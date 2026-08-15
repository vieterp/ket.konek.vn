"""Ghi nhật ký tự động ở tầng lưu trữ (FR-NFR-012/013).

Vì sao đặt ở listener của `Session` chứ không gọi tay trong từng service: một
service quên gọi là một thao tác không có vết, và không có cách nào phát hiện
ra ngoài việc đọc lại toàn bộ mã. Listener bắt **mọi** thay đổi đi qua ORM —
bao gồm cả những đường mà phase 6..10 chưa viết.

Hai bất biến:

1. **Cùng transaction.** Nhật ký ghi bằng chính `Session` đang flush, nên
   rollback nghiệp vụ cũng rollback nhật ký. Không bao giờ có nhật ký mồ côi
   kể một thao tác chưa từng xảy ra.
2. **Fail-closed.** Thay đổi trên bảng `Audited` mà transaction không khai
   người thực hiện thì ném `AuditContextMissingError` — chặn thao tác, không
   ghi âm thầm với `user_id = 0`.

Cần **hai** móc chứ không phải một: `before_flush` là nơi duy nhất còn đọc được
lịch sử thay đổi của thuộc tính, còn khóa chính của bản ghi mới thì tới
`after_flush` mới có. Gộp vào một móc là mất một trong hai.

Giới hạn đã biết: `Query.update()`/`delete()` hàng loạt và SQL text đi thẳng
**không** qua đây (chúng không nạp đối tượng vào session). Đường ghi khối lượng
lớn của phase 4/8 là set-based SQL (LD-14) nên phải tự ghi vết — bắt buộc nêu
trong review, không thể tự động hóa ở tầng này.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import ClassVar, Final
from uuid import UUID

from sqlalchemy import event, insert
from sqlalchemy.orm import Session, object_mapper
from sqlalchemy.orm.attributes import instance_state

from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.errors import AuditContextMissingError
from ket.kernel.persistence.types import AuditValues, JsonPrimitive

AUDIT_CONTEXT_KEY: Final[str] = "ket_audit_context"
_PENDING_KEY: Final[str] = "ket_audit_pending"

MAX_VALUE_LENGTH: Final[int] = 4000
"""Cắt giá trị quá dài (ghi chú, JSON lồng nhau) — nhật ký để đối chiếu thay
đổi, không phải bản sao thứ hai của dữ liệu."""

REDACTED: Final[str] = "<đã ẩn>"


@dataclass(frozen=True)
class AuditContext:
    """Ai làm, ở đâu, trong request nào."""

    user_id: int
    branch_id: int | None = None
    correlation_id: UUID | None = None
    client_info: str | None = None


class Audited:
    """Mixin đánh dấu bảng cần ghi nhật ký.

    Không phải bảng nào cũng đáng ghi: `jobs`, `idempotency_keys`,
    `number_sequences` đổi liên tục theo cơ chế nội bộ và ghi vết chúng chỉ làm
    loãng thứ kiểm toán viên cần đọc.
    """

    __audit_exclude__: ClassVar[frozenset[str]] = frozenset()
    """Cột không được đưa vào nhật ký: hash mật khẩu, bí mật TOTP, token đã mã
    hóa (RT-05). Vẫn ghi nhận là "có đổi" nhưng giá trị bị ẩn — nhật ký không
    được trở thành chỗ rò bí mật thứ hai."""


@dataclass
class _PendingEntry:
    """Diff đã chụp ở `before_flush`, chờ khóa chính ở `after_flush`."""

    instance: object
    action: AuditAction
    old_values: AuditValues | None
    new_values: AuditValues | None
    branch_id: int | None


@dataclass
class _PendingBatch:
    entries: list[_PendingEntry] = field(default_factory=list)


def _to_primitive(value: object) -> JsonPrimitive:
    """Chuẩn hóa giá trị cột về nguyên thủy JSON (xem `persistence/types.py`)."""
    if value is None or isinstance(value, str | bool | int):
        return _truncate(value)
    if isinstance(value, Decimal):
        # Chuỗi, không phải số JSON: số JSON là dấu phẩy động và sẽ làm hỏng
        # đúng thứ nhật ký sinh ra để chứng minh.
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _to_primitive(value.value)
    if isinstance(value, bytes | bytearray | memoryview):
        # Nhị phân trong bảng của hệ này là bí mật đã mã hóa — không bao giờ đổ ra.
        return f"<{len(bytes(value))} byte>"
    return _truncate(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _truncate(value: JsonPrimitive) -> JsonPrimitive:
    if isinstance(value, str) and len(value) > MAX_VALUE_LENGTH:
        return value[:MAX_VALUE_LENGTH] + "…"
    return value


def _column_keys(instance: object) -> tuple[str, ...]:
    return tuple(attr.key for attr in object_mapper(instance).column_attrs)


def _table_name(instance: object) -> str:
    """Tên **bảng**, không phải tên lớp: nhật ký sống lâu hơn mọi lần đổi tên lớp."""
    name = getattr(type(instance), "__tablename__", None)
    if not isinstance(name, str):
        raise TypeError(f"{type(instance).__name__} không phải bảng ORM")
    return name


def _excluded(instance: object) -> frozenset[str]:
    exclude: object = getattr(instance, "__audit_exclude__", frozenset())
    return exclude if isinstance(exclude, frozenset) else frozenset()


def _snapshot(instance: object) -> AuditValues:
    """Toàn bộ giá trị hiện tại (dùng cho thêm mới và xóa)."""
    exclude = _excluded(instance)
    values: AuditValues = {}
    for key in _column_keys(instance):
        raw = getattr(instance, key)
        values[key] = REDACTED if key in exclude and raw is not None else _to_primitive(raw)
    return values


def _diff(instance: object) -> tuple[AuditValues, AuditValues]:
    """Chỉ các cột thực sự đổi — "trước" và "sau"."""
    exclude = _excluded(instance)
    state = instance_state(instance)
    old: AuditValues = {}
    new: AuditValues = {}
    for key in _column_keys(instance):
        history = state.attrs[key].history
        if not history.has_changes():
            continue
        redact = key in exclude
        old[key] = (
            REDACTED if redact else _to_primitive(history.deleted[0] if history.deleted else None)
        )
        new[key] = (
            REDACTED if redact else _to_primitive(history.added[0] if history.added else None)
        )
    return old, new


def _branch_of(instance: object, context: AuditContext) -> int | None:
    """Chi nhánh của bản ghi, nếu bảng có cột đó; nếu không thì của request.

    Phải là chi nhánh của **bản ghi**: policy RLS trên `audit_log` kiểm cả
    `WITH CHECK`, nên một dòng nhật ký gắn nhầm chi nhánh sẽ bị chính DB từ
    chối — đúng lúc ghi, không phải lúc đọc.
    """
    own = getattr(instance, "branch_id", None)
    if isinstance(own, int) and not isinstance(own, bool):
        return own
    return context.branch_id


def _entity_id(instance: object) -> str:
    identity = object_mapper(instance).primary_key_from_instance(instance)
    return "/".join("" if part is None else str(part) for part in identity)


def _discard_pending(session: Session, *_ignored: object) -> None:
    """Vứt bỏ diff chưa ghi khi transaction (hoặc savepoint) bị hủy.

    Không có bước này, một flush **hỏng** vẫn để lại diff trong `session.info`:
    `before_flush` đã chạy, `after_flush` thì không, và lần flush thành công kế
    tiếp trên cùng `Session` sẽ ghi chúng vào `audit_log`. Kết quả là nhật ký
    **bất biến** khẳng định một thao tác chưa từng xảy ra — tệ hơn hẳn việc
    thiếu một dòng, vì không ai (kể cả chủ sở hữu bảng) được phép sửa lại.

    Đường tái hiện thật: `session.begin_nested()` quanh một lệnh ghi có thể
    đụng ràng buộc — đúng khuôn mẫu mà idempotency (bước 9) và optimistic
    locking (bước 10) sẽ dùng.
    """
    session.info.pop(_PENDING_KEY, None)


def _capture(session: Session, _flush_context: object, _instances: object) -> None:
    """`before_flush` — chụp diff khi lịch sử thuộc tính còn nguyên."""
    # Diff chỉ có nghĩa cho đúng lần flush sinh ra nó. Còn sót lại nghĩa là lần
    # flush trước đã hỏng giữa chừng (xem `_discard_pending`) — vứt đi, không
    # mang sang.
    session.info.pop(_PENDING_KEY, None)

    entries: list[_PendingEntry] = []

    for instance in session.new:
        if isinstance(instance, Audited):
            # Ảnh "sau" của bản ghi mới chụp ở `after_flush`, KHÔNG phải ở đây:
            # lúc này khóa chính chưa được cấp và mọi default (Python lẫn DB)
            # chưa áp, nên ảnh chụp bây giờ sẽ ghi `id: null` và `is_active:
            # null` cho một dòng thực tế là `(7, true)`.
            entries.append(_PendingEntry(instance, AuditAction.CREATED, None, None, None))
    for instance in session.dirty:
        if isinstance(instance, Audited) and session.is_modified(instance):
            old, new = _diff(instance)
            if not new:
                continue
            entries.append(_PendingEntry(instance, AuditAction.UPDATED, old, new, None))
    for instance in session.deleted:
        if isinstance(instance, Audited):
            entries.append(
                _PendingEntry(instance, AuditAction.DELETED, _snapshot(instance), None, None)
            )

    if not entries:
        return

    context = session.info.get(AUDIT_CONTEXT_KEY)
    if not isinstance(context, AuditContext):
        raise AuditContextMissingError(
            "Transaction thay đổi dữ liệu cần ghi nhật ký nhưng chưa khai người thực hiện. "
            "Mở transaction bằng ket.kernel.persistence.unit_of_work.unit_of_work.",
            entities=", ".join(sorted({type(e.instance).__name__ for e in entries})),
        )

    for entry in entries:
        entry.branch_id = _branch_of(entry.instance, context)

    batch = session.info.setdefault(_PENDING_KEY, _PendingBatch())
    if isinstance(batch, _PendingBatch):
        batch.entries.extend(entries)


def _write(session: Session, _flush_context: object) -> None:
    """`after_flush` — khóa chính đã có, ghi nhật ký trong cùng transaction."""
    batch = session.info.get(_PENDING_KEY)
    if not isinstance(batch, _PendingBatch) or not batch.entries:
        return

    context = session.info[AUDIT_CONTEXT_KEY]
    if not isinstance(context, AuditContext):  # pragma: no cover - _capture đã chặn
        raise AuditContextMissingError("Thiếu ngữ cảnh nhật ký lúc ghi")

    rows = [
        {
            "user_id": context.user_id,
            "branch_id": entry.branch_id,
            "entity_type": _table_name(entry.instance),
            "entity_id": _entity_id(entry.instance),
            "action": entry.action.value,
            "old_values": entry.old_values,
            # Bản ghi mới: chụp ảnh **ở đây**, khi khóa chính đã cấp và default
            # đã áp — trước `flush` thì chúng còn là `None` (xem `_capture`).
            "new_values": (
                _snapshot(entry.instance)
                if entry.action is AuditAction.CREATED
                else entry.new_values
            ),
            "correlation_id": context.correlation_id,
            "client_info": context.client_info,
        }
        for entry in batch.entries
    ]
    session.info.pop(_PENDING_KEY, None)
    session.execute(insert(AuditLog), rows)


def register_audit_listener() -> None:
    """Gắn listener vào lớp `Session` (chạy được nhiều lần, không cộng dồn).

    Bốn móc, không phải hai: hai móc ghi (`before_flush` chụp diff khi lịch sử
    còn nguyên, `after_flush` ghi khi khóa chính đã có) và hai móc **dọn** —
    thiếu chúng thì diff của một flush hỏng sẽ trôi sang flush kế tiếp.
    """
    hooks = (
        ("before_flush", _capture),
        ("after_flush", _write),
        ("after_rollback", _discard_pending),
        ("after_soft_rollback", _discard_pending),
    )
    for name, handler in hooks:
        if not event.contains(Session, name, handler):
            event.listen(Session, name, handler)


def record_action(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: AuditAction,
    branch_id: int | None = None,
    old_values: AuditValues | None = None,
    new_values: AuditValues | None = None,
) -> None:
    """Ghi vết một hành vi không phải thêm/sửa/xóa hàng.

    Ghi sổ, bỏ ghi sổ, khóa sổ, in — chúng đổi **trạng thái nghiệp vụ** chứ
    không nhất thiết đổi cột nào, nên listener không thấy. Phase 4 và 10a gọi
    hàm này; ở phase 2 nó là hợp đồng đã có sẵn để hai phase đó không phải tự
    nghĩ ra cách riêng.
    """
    context = session.info.get(AUDIT_CONTEXT_KEY)
    if not isinstance(context, AuditContext):
        raise AuditContextMissingError(
            "Ghi vết hành vi nhưng transaction chưa khai người thực hiện",
            entity_type=entity_type,
        )
    session.add(
        AuditLog(
            user_id=context.user_id,
            branch_id=branch_id if branch_id is not None else context.branch_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action.value,
            old_values=old_values,
            new_values=new_values,
            correlation_id=context.correlation_id,
            client_info=context.client_info,
        )
    )
