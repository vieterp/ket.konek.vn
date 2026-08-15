"""Thực hiện **đúng một lần** một thao tác đổi trạng thái (FR-NFR-004, RT-12).

Hình dạng bảng và bốn quyết định của RT-12 nằm ở `models.py`. Tệp này là phần
khó: ghép khóa idempotency vào **cùng** transaction với lệnh ghi nghiệp vụ mà
vẫn trả lời đúng cho lần thử lại.

Trình tự: **giành khóa → làm việc → điền kết quả**, cả ba trong *một*
transaction. Hai chữ "một transaction" mới là phần khó, và hai thứ tự khác đều
sai theo cách riêng của chúng:

* **Ghi khóa ở transaction riêng, commit trước** (cách quen thuộc nhất): một
  lệnh ghi nghiệp vụ hỏng sau đó sẽ để lại khóa cho một chứng từ **chưa từng
  tồn tại**, và lần thử lại bị từ chối vĩnh viễn — người dùng mất hẳn phiếu chi
  cho tới khi có người xóa khóa bằng tay. Đây chính là điều RT-12 cấm.
* **Làm việc trước, ghi khóa sau (cùng transaction)**: đúng về mặt bất biến,
  nhưng hỏng ở tình huống thật nhất. Lần gửi lại mang **đúng** nội dung cũ, nên
  nó đụng ràng buộc duy nhất của *bảng nghiệp vụ* (số chứng từ, mã danh mục)
  **trước** khi chạm tới bảng khóa — và người gọi nhận một lỗi ràng buộc thô
  thay vì kết quả của lần thực hiện trước. Đo được bằng test song song.

Giành khóa trước thì ràng buộc duy nhất của bảng khóa trở thành **cửa vào**:
request thứ hai dừng ngay ở câu lệnh đầu tiên, chưa ghi gì cả. Và vì PostgreSQL
cho nó **chờ** thay vì lỗi ngay — chờ tới khi bên kia commit (→ báo trùng khóa)
hoặc rollback (→ đi tiếp bình thường) — nên không có nhánh "cả hai cùng thấy
trống rồi cùng ghi".

Đánh đổi đã biết: `result_type`/`result_id` phải cho phép `NULL` ở tầng cột, vì
lúc giành khóa thì chưa biết kết quả. Không có dòng `NULL` nào **quan sát được**
(chúng chỉ tồn tại giữa hai câu lệnh của cùng một transaction chưa commit), và
`_lookup` coi dòng như vậy là hỏng chứ không phải là kết quả rỗng.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.errors import (
    IdempotencyKeyExpiredError,
    IdempotencyKeyReusedError,
    IdempotencyRaceLostError,
)
from ket.kernel.idempotency.models import IdempotencyKey
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work


class _KeyTakenError(Exception):
    """Nội bộ: ràng buộc duy nhất vừa loại mình ra khỏi cuộc đua ghi khóa.

    Một lớp riêng chứ không bắt thẳng `IntegrityError` quanh cả transaction:
    lệnh ghi **nghiệp vụ** cũng ném `IntegrityError` (trùng mã chi nhánh, sai
    khóa ngoại), và nuốt nó thành "có ai đó cùng khóa" sẽ trả `409` tranh chấp
    cho một lỗi dữ liệu mà người dùng phải tự sửa.
    """


# `ttl` **không có mặc định**, có chủ đích: giá trị thật nằm ở
# `Settings.idempotency_ttl_hours` (đọc được từ biến môi trường của bản cài), và
# một mặc định ở đây sẽ là nguồn sự thật thứ hai — endpoint nào quên truyền sẽ
# âm thầm dùng 24 giờ bất kể người vận hành cấu hình gì. Không có mặc định thì
# "quên" là lỗi biên dịch.


@dataclass(frozen=True)
class IdempotentRef:
    """Tham chiếu tới kết quả — **không** phải bản sao của phản hồi.

    Lưu tham chiếu chứ không lưu thân phản hồi: thân phản hồi phình bảng, lỗi
    thời ngay khi chứng từ được sửa, và có thể chứa dữ liệu mà người gọi lại lần
    hai không còn quyền xem (họ vừa bị gỡ khỏi một chi nhánh chẳng hạn). Đọc lại
    từ bản ghi thật thì mọi lớp kiểm quyền vẫn chạy.
    """

    result_type: str
    result_id: str


def fingerprint_of(payload: str) -> str:
    """Vân tay của thân request — SHA-256 hex.

    Dùng để phân biệt "gửi lại đúng thứ đó" với "dùng lại khóa cho nội dung
    khác". Vế thứ hai là **lỗi của client** và phải nổ ra (`409`), vì trả kết
    quả cũ cho một nội dung mới là im lặng bỏ rơi lệnh ghi thứ hai.
    """
    return sha256(payload.encode("utf-8")).hexdigest()


def _lookup(
    session: Session,
    *,
    route_key: str,
    key: str,
    user_id: int,
    fingerprint: str,
    now: datetime,
) -> IdempotentRef | None:
    """Khóa đã dùng chưa? Trả tham chiếu cũ, hoặc ném nếu dùng sai cách."""
    row = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.route_key == route_key,
            IdempotencyKey.idempotency_key == key,
        )
    )
    if row is None:
        return None

    if row.user_id != user_id or row.request_fingerprint != fingerprint:
        # Gộp hai nhánh vào một mã lỗi có chủ đích: nói rõ "khóa này của người
        # khác" là xác nhận cho người dò biết họ đoán trúng một khóa đang tồn
        # tại. Với client thật thì cách sửa giống hệt nhau — sinh khóa mới.
        raise IdempotencyKeyReusedError(
            "Khóa idempotency này đã dùng cho một yêu cầu khác",
            route=route_key,
        )

    if row.expires_at <= now:
        # Fail-closed: chạy lại một lệnh ghi sổ sau khi khóa đã hết hạn là ghi
        # trùng, không phải khôi phục. Người dùng bắt đầu lại bằng khóa mới và
        # tự thấy chứng từ cũ nếu nó đã tồn tại.
        raise IdempotencyKeyExpiredError(
            "Khóa idempotency đã hết hạn — gửi lại bằng khóa mới",
            route=route_key,
        )

    if row.result_type is None or row.result_id is None:
        # Không quan sát được qua đường bình thường: dòng chỉ hiện ra sau khi
        # transaction đã điền kết quả và commit. Tới được đây nghĩa là có ai đó
        # ghi thẳng vào bảng, hoặc một bản khôi phục dở dang. Coi là "chưa xong"
        # và bảo thử lại — an toàn hơn hẳn việc trả một tham chiếu rỗng.
        raise IdempotencyRaceLostError(
            "Khóa idempotency tồn tại nhưng chưa có kết quả — thử lại sau giây lát",
            route=route_key,
        )

    return IdempotentRef(result_type=row.result_type, result_id=row.result_id)


def execute_once[T](
    factory: sessionmaker[Session],
    scope: RequestScope,
    *,
    route_key: str,
    key: str,
    fingerprint: str,
    work: Callable[[Session], tuple[T, IdempotentRef]],
    replay: Callable[[Session, IdempotentRef], T],
    ttl: timedelta,
    now: datetime | None = None,
) -> tuple[T, bool]:
    """Chạy `work` nếu khóa còn mới; nếu không thì `replay` kết quả cũ.

    Trả `(kết quả, đã_thực_hiện)` — `False` nghĩa là đây là lần gửi lại và
    không có gì mới được ghi, để tầng API trả `200` thay vì `201`.

    `work` nhận `Session` **đang mở** và trả về (giá trị cho phản hồi, tham
    chiếu để lưu). Nó không được tự commit: transaction do hàm này sở hữu, và
    chính việc sở hữu đó là thứ giữ cho khóa với lệnh ghi cùng sống cùng chết.
    """
    moment = now if now is not None else datetime.now(UTC)

    # Hai vòng, không nhiều hơn: vòng đầu chạy việc, vòng sau chỉ để đọc kết quả
    # của bên thắng khi ràng buộc duy nhất vừa loại mình ra. Một vòng lặp mở sẽ
    # biến tranh chấp thành lặp vô hạn dưới tải, đúng lúc không nên.
    for final_attempt in (False, True):
        try:
            with unit_of_work(factory, scope) as session:
                existing = _lookup(
                    session,
                    route_key=route_key,
                    key=key,
                    user_id=scope.user_id,
                    fingerprint=fingerprint,
                    now=moment,
                )
                if existing is not None:
                    return replay(session, existing), False

                if final_attempt:
                    # Vòng hai mà khóa lại biến mất: bên thắng đã rollback sau
                    # khi làm mình thua, hoặc khóa vừa bị dọn. Dừng lại và để
                    # client quyết định, thay vì ghi một chứng từ mà lần gửi
                    # trước có thể cũng đã ghi.
                    raise IdempotencyRaceLostError(
                        "Một yêu cầu khác cùng khóa đang được xử lý — thử lại sau giây lát",
                        route=route_key,
                    )

                try:
                    claim = _claim(
                        session,
                        route_key=route_key,
                        key=key,
                        user_id=scope.user_id,
                        fingerprint=fingerprint,
                        expires_at=moment + ttl,
                    )
                except IntegrityError as conflict:
                    raise _KeyTakenError from conflict

                value, ref = work(session)
                session.flush()
                claim.result_type = ref.result_type
                claim.result_id = ref.result_id
                session.flush()
                return value, True
        except _KeyTakenError:
            # Bên kia đã commit cùng khóa. Transaction này đã bị `unit_of_work`
            # rollback trọn vẹn — và vì khóa được giành **trước** khi làm việc,
            # cái bị bỏ đi chỉ là một dòng khóa, không phải một chứng từ.
            continue

    raise AssertionError("vòng execute_once phải thoát bằng return hoặc raise")


def _claim(
    session: Session,
    *,
    route_key: str,
    key: str,
    user_id: int,
    fingerprint: str,
    expires_at: datetime,
) -> IdempotencyKey:
    """Giành khóa: `INSERT` rồi `flush` ngay, **trước** khi làm việc gì khác.

    `flush()` tường minh là toàn bộ điểm của hàm này. Nó biến ràng buộc duy nhất
    của bảng khóa thành **cửa vào** của thao tác: request thứ hai dừng ở đây, ở
    câu lệnh đầu tiên, thay vì dừng ở giữa lệnh ghi nghiệp vụ.

    Kết quả (`result_type`/`result_id`) điền sau, trong **cùng** transaction —
    xem docstring đầu tệp về lý do không tách ra transaction riêng.
    """
    claim = IdempotencyKey(
        route_key=route_key,
        idempotency_key=key,
        user_id=user_id,
        request_fingerprint=fingerprint,
        expires_at=expires_at,
    )
    session.add(claim)
    session.flush()
    return claim


def prune_expired_keys(session: Session, *, now: datetime | None = None) -> int:
    """Xóa khóa đã hết hạn của **dataset đang bind**. Trả số dòng đã xóa.

    Bảng chỉ tăng: mỗi thao tác ghi để lại một dòng, và khóa hết hạn thì không
    còn chống trùng cho gì nữa (`_lookup` từ chối chúng — fail-closed). Index
    `ix_idempotency_keys_expires_at` được khai từ `0001` đúng để phục vụ lượt
    dọn này; không có đường dọn thì index đó chỉ là chi phí ghi.

    Không ghi vết kiểm toán như `prune_expired_sessions`: khóa idempotency là
    **cơ chế giao thức**, không phải lịch sử ai làm gì — thứ nó bảo vệ (chứng
    từ) vẫn còn nguyên cùng nhật ký của nó.
    """
    moment = now if now is not None else datetime.now(UTC)
    result = session.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at < moment))
    return cast("CursorResult[Any]", result).rowcount
