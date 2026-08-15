"""Phân giải tùy chọn hai cấp: riêng người dùng đè lên chung hệ thống (FR-SYS-060).

Ba tầng, xét từ hẹp tới rộng: **giá trị của tôi** → **giá trị của hệ thống** →
**mặc định trong catalog**. Tầng thứ ba luôn có, nên mọi lần đọc đều trả về một
giá trị — không có nhánh "chưa cấu hình" mà nơi gọi phải tự nghĩ ra cách xử lý.

Tùy chọn nằm **trong schema dataset**, không phải schema điều khiển: hai doanh
nghiệp trên cùng bản cài có thể ghi sổ theo hai thông tư khác nhau, nên "số chữ
số thập phân" của họ cũng khác nhau. Hệ quả: cùng một người mở hai doanh nghiệp
sẽ có hai bộ tùy chọn riêng — đúng ý muốn, vì thói quen bàn phím thì giống nhau
nhưng quy tắc ghi sổ thì không.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ket.kernel.config.catalog import (
    CATALOG,
    SettingDefinition,
    SettingScope,
    SettingValue,
    definition_of,
    parse_value,
)
from ket.kernel.errors import SettingScopeNotAllowedError, SettingUnknownError
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.security.models import Setting

NO_ROW_VERSION: Final[int] = 0
"""Phiên bản quy ước của một dòng **chưa tồn tại**.

`row_version` thật bắt đầu từ 1, nên 0 không đụng giá trị nào. Nhờ vậy "chưa
có ai ghi" và "đã có người ghi" so sánh được bằng cùng một phép kiểm, thay vì
hai nhánh mà nhánh thứ hai dễ bị quên."""


def _expected_or_absent(expected_row_version: int | None) -> int:
    """`None` từ client = "tôi tin rằng chưa có dòng nào"."""
    return NO_ROW_VERSION if expected_row_version is None else expected_row_version


@dataclass(frozen=True)
class EffectiveSetting:
    """Giá trị đang có hiệu lực cho một người dùng, kèm **nguồn** của nó.

    `source` để màn hình thiết lập nói được "bạn đang dùng giá trị chung" thay
    vì hiển thị một ô trống hay một giá trị không rõ từ đâu ra — và để nút
    "khôi phục mặc định" biết có gì để khôi phục hay không.
    """

    key: str
    value: SettingValue
    raw_value: str
    value_type: str
    source: str
    """`default` | `system` | `user`."""

    system_row_version: int | None
    user_row_version: int | None
    """Phiên bản của **từng cấp**, `None` khi cấp đó chưa có dòng nào.

    Hai trường chứ **không** phải một "phiên bản của giá trị đang hiệu lực".
    Bản đầu tiên của tệp này trả một trường duy nhất và bảo client gửi lại nó
    khi lưu — mà lúc ghi thì lại so với dòng của **cấp được yêu cầu**. Hai dòng
    khác nhau nên phép so vô nghĩa theo cả hai chiều, đo được cả hai:

    * người dùng có giá trị chung (system v1) mà chưa có giá trị riêng thì
      **không đặt được** giá trị riêng — gửi đúng theo hợp đồng vẫn nhận `409`
      với `latest: null`, tức là FR-SYS-060 hỏng ở đúng tính năng của nó;
    * quản trị viên có giá trị riêng ở v2 **ghi đè được** dòng chung cũng ở v2
      mà chưa từng nhìn thấy nó — đúng thứ FR-NFR-005 sinh ra để chặn.

    Client sửa cấp nào thì gửi phiên bản của cấp đó."""


def _stored_rows(session: Session, *, user_id: int) -> dict[tuple[str, str], Setting]:
    """Mọi dòng liên quan tới người này: cấp hệ thống + cấp của chính họ.

    Một lượt truy vấn cho cả hai cấp: màn hình thiết lập đọc toàn bộ danh mục
    cùng lúc, và hai lượt sẽ mở ra khe hở giữa chúng.
    """
    rows = session.scalars(
        select(Setting).where(
            (Setting.scope == SettingScope.SYSTEM.value)
            | ((Setting.scope == SettingScope.USER.value) & (Setting.user_id == user_id))
        )
    ).all()
    return {(row.scope, row.key): row for row in rows}


def _resolve(
    definition: SettingDefinition, stored: dict[tuple[str, str], Setting]
) -> EffectiveSetting:
    user_row = stored.get((SettingScope.USER.value, definition.key))
    system_row = stored.get((SettingScope.SYSTEM.value, definition.key))
    row = user_row if user_row is not None else system_row

    raw = row.value if row is not None else definition.default
    source = (
        SettingScope.USER.value
        if user_row is not None
        else SettingScope.SYSTEM.value
        if system_row is not None
        else "default"
    )
    return EffectiveSetting(
        key=definition.key,
        # Giá trị đã lưu vẫn đi qua `parse_value`: cột là `varchar`, nên một
        # bản khôi phục cũ hay một lần sửa tay trong DB có thể để lại chuỗi
        # không còn hợp lệ với ràng buộc hiện tại của khóa đó.
        value=parse_value(definition, raw),
        raw_value=raw,
        value_type=definition.value_type.value,
        source=source,
        system_row_version=system_row.row_version if system_row is not None else None,
        user_row_version=user_row.row_version if user_row is not None else None,
    )


def effective_settings(session: Session, *, user_id: int) -> tuple[EffectiveSetting, ...]:
    """Toàn bộ catalog với giá trị đang có hiệu lực cho `user_id`."""
    stored = _stored_rows(session, user_id=user_id)
    return tuple(_resolve(definition, stored) for definition in CATALOG.values())


def value_of(session: Session, *, key: str, user_id: int) -> SettingValue:
    """Giá trị hiệu lực của một khóa. Khóa lạ → `404` (lỗi lập trình phía gọi).

    **Một lượt truy vấn cho mỗi lời gọi.** Nơi gọi phải đọc **một lần cho cả
    transaction** rồi truyền giá trị xuống, đừng gọi trong vòng lặp dòng chứng
    từ — `money.scale` là ứng viên số một cho lỗi đó ở phase sau, và nó sẽ biến
    một lần ghi sổ 500 dòng thành 500 lượt truy vấn tùy chọn."""
    definition = definition_of(key)
    if definition is None:
        raise SettingUnknownError("Không có tùy chọn với khóa này", key=key)
    return _resolve(definition, _stored_rows(session, user_id=user_id)).value


def set_setting(
    session: Session,
    *,
    key: str,
    scope: SettingScope,
    user_id: int,
    raw_value: str,
    expected_row_version: int | None,
) -> EffectiveSetting:
    """Ghi một tùy chọn ở đúng một cấp, có kiểm phiên bản (FR-NFR-005).

    `expected_row_version=None` nghĩa là "tôi tin rằng chưa có ai ghi khóa này ở
    cấp này". Nếu thực ra đã có dòng thì đó vẫn là xung đột: người gửi đang dựa
    trên một màn hình cũ hơn hiện trạng, y như trường hợp phiên bản lệch.

    Trả về giá trị **hiệu lực sau khi ghi**, không phải dòng vừa ghi: ghi tùy
    chọn cấp hệ thống trong khi bản thân mình có giá trị riêng thì thứ mình
    thấy tiếp theo vẫn là giá trị riêng, và phản hồi phải nói đúng điều đó.
    """
    definition = definition_of(key)
    if definition is None:
        raise SettingUnknownError("Không có tùy chọn với khóa này", key=key)
    if scope not in definition.scopes:
        raise SettingScopeNotAllowedError(
            "Tùy chọn này không cho phép cấu hình ở cấp đang yêu cầu",
            key=key,
            scope=scope.value,
            allowed=", ".join(sorted(item.value for item in definition.scopes)),
        )

    # Ném sớm nếu giá trị không hợp lệ — trước khi chạm bảng, để transaction
    # không mang theo một lệnh ghi nửa vời.
    parse_value(definition, raw_value)

    owner_id = user_id if scope is SettingScope.USER else None

    # Khóa cố vấn theo (cấp, khóa, chủ sở hữu) trước khi đọc — cùng khuôn với
    # `role_service._lock_schema`. `SELECT … FOR UPDATE` **không** dùng được ở
    # đây: lần ghi đầu tiên cho một khóa chưa có dòng nào để khóa, nên hai
    # request song song sẽ cùng thấy trống rồi cùng `INSERT`, và người thua nhận
    # `500` từ index duy nhất thay vì `409` có nghĩa. Khóa cố vấn khóa được
    # **ô còn trống**, và tự nhả khi transaction kết thúc.
    # Khóa cố vấn là phạm vi **database**, không phải schema — nên chuỗi khóa
    # phải mang theo dataset, nếu không hai doanh nghiệp ghi cùng một khóa tùy
    # chọn sẽ nối đuôi nhau mà không có lý do gì.
    schema = session.scalar(text("SELECT current_schema()"))
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"setting/{schema}/{scope.value}/{owner_id}/{key}"},
    )

    row = session.scalar(
        select(Setting).where(
            Setting.key == key,
            Setting.scope == scope.value,
            Setting.user_id.is_(None) if owner_id is None else Setting.user_id == owner_id,
        )
    )

    if row is None:
        require_row_version(
            current=NO_ROW_VERSION,
            expected=_expected_or_absent(expected_row_version),
            entity=Setting.__tablename__,
            # Vẫn trả `latest` dù chưa có dòng nào: client cần biết **phải gửi
            # gì** ở lần thử tiếp theo, và `null` là câu trả lời hợp lệ cho câu
            # hỏi đó. `latest: null` trơ trọi thì họ chỉ biết là hỏng.
            latest={"key": key, "scope": scope.value, "value": None, "row_version": None},
        )
        session.add(
            Setting(
                scope=scope.value,
                user_id=owner_id,
                key=key,
                value=raw_value,
                value_type=definition.value_type.value,
            )
        )
    else:
        require_row_version(
            current=row.row_version,
            expected=_expected_or_absent(expected_row_version),
            entity=Setting.__tablename__,
            latest={
                "key": key,
                "scope": scope.value,
                "value": row.value,
                "row_version": row.row_version,
            },
        )
        row.value = raw_value
        row.value_type = definition.value_type.value

    session.flush()
    return _resolve(definition, _stored_rows(session, user_id=user_id))
