"""Hàng đợi truyền tải hóa đơn — giao dịch tới hạn của RT-10.

Toàn bộ lát 7E-1 quy về một câu: **cấp số và xếp hàng phải xảy ra cùng lúc hoặc
không xảy ra**. Cấp số commit trước rồi mới xếp hàng thì một tiến trình chết
giữa hai bước để lại một số hóa đơn đã tiêu mà không ai đi gửi — dãy `gap_free`
thủng đúng chỗ nó sinh ra để bịt. Xếp hàng trước rồi mới cấp số thì worker giành
được một dòng trỏ tới tờ hóa đơn chưa có số.

`enqueue_issue` vì thế **không** tự mở transaction: nó chạy trong `unit_of_work`
của người gọi. Endpoint gọi nó bên trong `execute_once`, nên lượt gửi lại cùng
khóa idempotency không đốt thêm số **và** không xếp thêm dòng.

## Hai đường vào, và vì sao phải là hai

Câu hỏi an toàn của RT-10 là: *dòng này có thể đã tới tay provider chưa?* Nếu
"có thể" thì cấm gửi lại trước khi hỏi. Câu trả lời phải đọc được từ **trạng
thái đã commit**, vì thứ nó chống đỡ chính là một tiến trình chết giữa chừng —
mà thân job thì chạy trong **một** transaction do worker mở và không tự commit
được (xem `JobContext`). Không có cách nào ghi "sắp gửi đây" ngay trước lượt gọi
mạng rồi mới gọi.

Nên lượt "sắp gửi đây" được ghi ở chỗ **có** commit. Có đúng hai chỗ như thế,
và cả hai đều cần:

* **endpoint phát hành** đặt lease lúc xếp hàng — dòng sinh ra đã `in_flight`
  (§Pipeline của plan viết đúng như vậy), và job nhận thẳng `outbox_id` của nó
  qua `load_for_send`. Đó là đường vào thứ nhất, và là đường **duy nhất** một
  lượt gửi chưa-từng-gửi đi qua;
* **`jobs.attempt`** của hàng đợi job, tăng ngay lúc worker giành job trong
  transaction riêng của hàng đợi (`kernel/jobs/queue.py`). Đây là hàng rào cho
  đúng cái lỗ mà lease **không** bịt được: `_fence_before_commit` mất lease làm
  rollback trọn thân job, kéo theo cả lượt làm mới lease của `load_for_send` —
  nên dòng quay về với lease T0 do endpoint đặt, trông y như "chưa ai gửi", dù
  lượt gọi provider đã thật sự đi. Số lượt giành job thì không rollback theo, và
  `attempt > 1` nghĩa là "thân job này đã chạy ít nhất một lần rồi". Người gọi
  truyền nó vào `force_reconcile`.

Đường thứ hai là bộ bơm, `claim_due`. Mọi dòng nó giành đều đã có thể tới tay
provider rồi:

* `in_flight` mà lease **hết hạn** — người cầm nó đã chết, và cái chết ấy có thể
  xảy ra ngay sau khi provider nhận;
* `needs_reconcile` — lượt trước mất tín hiệu.

Không có nguồn thứ ba, nên `claim_due` **không bao giờ** sinh ra một lượt gửi
mù. Đó là lý do bảng bỏ hẳn chặng `pending`: một dòng "chờ tới lượt" không phân
biệt được với một dòng đã gửi rồi mà lượt ghi kết luận bị rollback.

Hệ quả đẹp: một lượt chạy hỏng giữa lô, kéo cả transaction rollback, vẫn an
toàn. Mọi dòng đã gửi trong lô ấy quay về `in_flight` với lease **cũ**; tới lúc
lease hết hạn chúng đi đúng nhánh "phải hỏi".

`attempt_count` vì thế chỉ để lùi lịch và để người vận hành đọc — nó **không**
mang bất biến nào.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ket.kernel.identifiers import uuid7
from ket.modules.einvoice.models import (
    EInvoice,
    EInvoiceOutbox,
    OutboxOperation,
    OutboxStatus,
)
from ket.modules.einvoice.service import EInvoiceService

LEASE_DURATION: Final[timedelta] = timedelta(minutes=5)
"""Bao lâu thì coi như worker giữ dòng này đã chết.

Rộng hơn nhiều so với một lượt gọi provider bình thường (vài giây) vì cái giá
hai phía không cân nhau: đòi lại **quá sớm** cho hai worker cùng gửi một tờ hóa
đơn, còn đòi lại muộn chỉ làm lượt phát hành chậm thêm ít phút. Cùng lập luận
với lease của `kernel.jobs`, khác con số vì phía kia là mạng Internet chứ không
phải CPU."""

RETRY_BACKOFF: Final[tuple[timedelta, ...]] = (
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
)
"""Giãn cách giữa các lượt thử; hết bảng thì lấy phần tử cuối.

Không nhân đôi không trần: một sự cố mạng kéo dài nửa buổi sáng không được đẩy
lượt thử kế tiếp ra sau giờ làm việc, vì người dùng đang đứng chờ tờ hóa đơn."""


def _now() -> datetime:
    return datetime.now(UTC)


def next_attempt_after(attempt_count: int, *, now: datetime | None = None) -> datetime:
    """Mốc sớm nhất cho lượt thử kế tiếp sau `attempt_count` lượt đã thử."""
    index = min(max(attempt_count, 1), len(RETRY_BACKOFF)) - 1
    return (now or _now()) + RETRY_BACKOFF[index]


@dataclass(frozen=True)
class ClaimedRow:
    """Một dòng đã giành, kèm câu trả lời an toàn tính **trước** khi lease mới đè lên.

    Hai trường chứ không một, vì `claim_due` làm mới `in_flight_since` — thao
    tác ấy xóa mất chính dấu vết ("lease đã hết hạn") mà `must_reconcile` đọc.
    Tính trước rồi mang theo là cách duy nhất giữ cả hai.
    """

    row: EInvoiceOutbox
    must_reconcile: bool


def enqueue_issue(
    session: Session,
    einvoice_id: UUID,
    *,
    invoice_date: date,
    provider_code: str,
) -> tuple[EInvoice, EInvoiceOutbox]:
    """Cấp số và xếp dòng truyền tải **trong cùng một transaction** (RT-10).

    Dòng sinh ra đã ở `in_flight` với lease còn hạn và `attempt_count = 1`,
    đúng như §Pipeline của plan viết. Lease ấy **là** lượt giành cho lần gửi đầu:
    nó được ghi ở transaction có commit (endpoint), nên nó sống sót qua mọi kiểu
    chết của worker — xem §"Hai đường vào" ở đầu tệp.

    Trả về cả hóa đơn lẫn dòng hàng đợi vì người gọi cần cả hai: một để trả về
    client, một để xếp job gửi đúng dòng ấy.

    Phát hành **lại** một tờ đã bị từ chối đi qua đúng hàm này. `service.issue`
    giữ nguyên số cũ (ADR-013); dòng outbox cũ đang ở `failed` nên chỉ mục
    một-dòng-mở cho phép xếp dòng mới — với `client_ref` mới, đúng như phải thế:
    đây là một lần chào hàng khác với provider, không phải lượt gửi lại của lần
    trước.
    """
    invoice = EInvoiceService(session).issue(einvoice_id, invoice_date=invoice_date)
    now = _now()
    row = EInvoiceOutbox(
        einvoice_id=invoice.id,
        branch_id=invoice.branch_id,
        provider_code=provider_code,
        operation=OutboxOperation.ISSUE,
        client_ref=uuid7(),
        status=OutboxStatus.IN_FLIGHT,
        attempt_count=1,
        in_flight_since=now,
        next_attempt_at=now,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return invoice, row


def claim_due(session: Session, *, limit: int, now: datetime | None = None) -> list[ClaimedRow]:
    """Giành các dòng tới hạn và làm mới lease của chúng.

    `FOR UPDATE SKIP LOCKED` chứ không `FOR UPDATE`: hai worker song song phải
    chia nhau hàng đợi, không xếp hàng chờ nhau. Bỏ qua dòng đang bị khóa là
    đúng — worker kia đang lo dòng ấy.

    **Mọi dòng nó giành đều là dòng phải hỏi trước.** Hai nguồn — lease hết hạn
    và `needs_reconcile` — đều có nghĩa "có thể đã tới tay provider rồi". Lượt
    gửi chưa-từng-gửi đi đường khác (`load_for_send`), nên hàm này không bao giờ
    sinh ra một lượt gửi mù. Xem §"Hai đường vào" ở đầu tệp.
    """
    moment = now or _now()
    lease_deadline = moment - LEASE_DURATION
    due = (
        select(EInvoiceOutbox)
        .where(
            or_(
                and_(
                    EInvoiceOutbox.status == OutboxStatus.NEEDS_RECONCILE,
                    EInvoiceOutbox.next_attempt_at <= moment,
                ),
                and_(
                    EInvoiceOutbox.status == OutboxStatus.IN_FLIGHT,
                    EInvoiceOutbox.in_flight_since <= lease_deadline,
                ),
            )
        )
        .order_by(EInvoiceOutbox.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed: list[ClaimedRow] = []
    for row in session.scalars(due):
        row.status = OutboxStatus.IN_FLIGHT
        row.in_flight_since = moment
        row.attempt_count += 1
        claimed.append(ClaimedRow(row=row, must_reconcile=True))
    session.flush()
    return claimed


def load_for_send(
    session: Session,
    outbox_id: int,
    *,
    force_reconcile: bool,
    now: datetime | None = None,
) -> ClaimedRow | None:
    """Mở một dòng đã biết id để gửi — đường vào của lượt phát hành đầu tiên.

    Khác `claim_due` ở đúng một điểm, và điểm ấy là toàn bộ lý do có hai hàm:
    ở đây `must_reconcile` **được tính**, vì dòng có thể còn nguyên lease do
    endpoint đặt (chưa ai gửi — gửi thẳng) hoặc đã quá hạn vì một worker chết
    trước đó (phải hỏi).

    `force_reconcile` là vế thứ hai của phép tính ấy, và **không** phải một tùy
    chọn: người gọi truyền `jobs.attempt > 1` vào đây. Lease không tự đủ, vì
    lượt làm mới nó nằm trong chính transaction mà `_fence_before_commit` sẽ
    rollback khi worker mất lease job — dòng quay về với lease T0 của endpoint,
    trông y như chưa ai gửi, dù lượt gọi provider đã đi thật. Xem §"Hai đường
    vào" ở đầu tệp.

    `None` khi dòng đã biến mất hoặc đã đóng — người gọi bỏ qua thay vì đổ: cả
    hai đều là kết cục hợp lệ của một job xếp hàng từ trước.
    """
    moment = now or _now()
    lease_deadline = moment - LEASE_DURATION
    stmt = select(EInvoiceOutbox).where(EInvoiceOutbox.id == outbox_id).with_for_update()
    row = session.scalars(stmt).one_or_none()
    if row is None or row.status in (OutboxStatus.DONE, OutboxStatus.FAILED):
        return None
    must_reconcile = force_reconcile or _has_possibly_been_sent(row, lease_deadline=lease_deadline)
    row.status = OutboxStatus.IN_FLIGHT
    row.in_flight_since = moment
    if must_reconcile:
        # Lượt giành của endpoint đã tính là lượt thứ nhất; chỉ đếm thêm khi
        # đây thật sự là một lượt mới.
        row.attempt_count += 1
    session.flush()
    return ClaimedRow(row=row, must_reconcile=must_reconcile)


def mark_superseded(row: EInvoiceOutbox, *, message: str) -> None:
    """Đóng dòng vì tờ hóa đơn đã rời `DANG_PHAT_HANH` bằng đường khác.

    Người dùng tra cứu trên cổng cơ quan thuế rồi bấm "Xác nhận" tay là đường
    có thật, và nó để lại một dòng hàng đợi vẫn đang trỏ tới tờ hóa đơn ấy. Bộ
    bơm mà cứ thế gửi đi thì với nhà cung cấp thật, một `query_status` trả
    `known=False` sẽ dẫn thẳng tới tờ hóa đơn thứ hai.

    Đóng ở `done` vì việc của **hàng đợi** đã xong, không phải vì provider đã
    nhận; `last_error` giữ lại lý do để panel vận hành không đọc nhầm nó thành
    một lượt gửi thành công.
    """
    row.status = OutboxStatus.DONE
    row.in_flight_since = None
    row.next_attempt_at = None
    row.last_error = message


def due_now(session: Session, *, now: datetime | None = None) -> int:
    """Số dòng đang chờ tới lượt — cho panel vận hành và cho test."""
    moment = now or _now()
    lease_deadline = moment - LEASE_DURATION
    total = session.scalar(
        select(func.count())
        .select_from(EInvoiceOutbox)
        .where(
            or_(
                and_(
                    EInvoiceOutbox.status == OutboxStatus.NEEDS_RECONCILE,
                    EInvoiceOutbox.next_attempt_at <= moment,
                ),
                and_(
                    EInvoiceOutbox.status == OutboxStatus.IN_FLIGHT,
                    EInvoiceOutbox.in_flight_since <= lease_deadline,
                ),
            )
        )
    )
    return int(total or 0)


def _has_possibly_been_sent(row: EInvoiceOutbox, *, lease_deadline: datetime) -> bool:
    """Dòng này có thể đã tới tay provider chưa — câu hỏi an toàn của RT-10."""
    if row.status == OutboxStatus.NEEDS_RECONCILE:
        return True
    # Lease hết hạn = người cầm nó đã chết, và cái chết ấy có thể xảy ra ngay
    # sau khi provider nhận. Lease còn hạn ở đây chỉ có một nghĩa: chính endpoint
    # phát hành vừa đặt nó và chưa ai gửi gì.
    return row.in_flight_since is not None and row.in_flight_since <= lease_deadline


def mark_done(row: EInvoiceOutbox, *, provider_ref: str | None = None) -> None:
    """Provider đã nhận. Chặng cuối — bộ bơm không nhìn tới dòng này nữa."""
    row.status = OutboxStatus.DONE
    row.in_flight_since = None
    row.next_attempt_at = None
    row.last_error = None
    if provider_ref is not None:
        row.provider_ref = provider_ref


def mark_failed(row: EInvoiceOutbox, *, message: str) -> None:
    """Provider **từ chối** — một câu trả lời rõ ràng, không phải mất tín hiệu.

    Không lùi lịch thử lại: gửi lại đúng nội dung ấy sẽ bị từ chối đúng như thế.
    Số hóa đơn giữ nguyên (ADR-013); đường ra nằm ở người dùng — sửa chứng từ
    gốc rồi phát hành lại, hoặc lập biên bản hủy số.
    """
    row.status = OutboxStatus.FAILED
    row.in_flight_since = None
    row.next_attempt_at = None
    row.last_error = message


def mark_needs_reconcile(row: EInvoiceOutbox, *, message: str, now: datetime | None = None) -> None:
    """Không rõ kết quả — trả dòng về hàng đợi, và **không** gửi lại mù.

    Chặng `NEEDS_RECONCILE` là thứ `_has_possibly_been_sent` đọc ở lượt sau, nên
    lượt ấy bắt buộc đi qua `query_status` trước.
    """
    moment = now or _now()
    row.status = OutboxStatus.NEEDS_RECONCILE
    row.in_flight_since = None
    row.next_attempt_at = next_attempt_after(row.attempt_count, now=moment)
    row.last_error = message


def open_rows_for(session: Session, einvoice_id: UUID) -> list[EInvoiceOutbox]:
    """Dòng còn mở của một hóa đơn — cho màn hình và cho test."""
    stmt = (
        select(EInvoiceOutbox)
        .where(
            EInvoiceOutbox.einvoice_id == einvoice_id,
            EInvoiceOutbox.status.notin_((OutboxStatus.DONE, OutboxStatus.FAILED)),
        )
        .order_by(EInvoiceOutbox.id)
    )
    return list(session.scalars(stmt))


def list_rows(
    session: Session, *, status: OutboxStatus | None = None, limit: int, offset: int
) -> tuple[list[EInvoiceOutbox], int]:
    """Một trang hàng đợi cho panel vận hành, kèm tổng số dòng khớp bộ lọc."""
    conditions = [] if status is None else [EInvoiceOutbox.status == status]
    total = session.scalar(select(func.count()).select_from(EInvoiceOutbox).where(*conditions))
    stmt = (
        select(EInvoiceOutbox)
        .where(*conditions)
        .order_by(EInvoiceOutbox.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt)), int(total or 0)
