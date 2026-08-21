"""`posting/engine/guards.py` — pipeline cảnh báo ba mức (FR-SYS-062, lát 6A).

Phần không cần DB: `run_guards` chỉ duyệt registry và gom kết quả — session
đi xuyên qua tới guard, nên một stub bỏ qua nó là đủ. Đường tích hợp thật
(guard chạy trong `PostingService.post`, lượt xác nhận qua HTTP) kiểm ở
`test_posting_engine_flow.py::test_post_requires_acknowledgement_for_guard_warnings`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from sqlalchemy.orm import Session

from ket.kernel.errors import PostingViolation
from ket.posting.documents.models import Voucher
from ket.posting.engine.guards import (
    GUARD_WARNING_DETAIL,
    GuardFinding,
    PostingGuardRegistry,
    run_guards,
)
from ket.posting.engine.prepared import PreparedLine

_NO_SESSION = cast(Session, None)
_NO_VOUCHER = cast(Voucher, None)


class _FixedGuard:
    def __init__(self, findings: Sequence[GuardFinding]) -> None:
        self._findings = findings

    def check(
        self, session: Session, *, voucher: Voucher, lines: Sequence[PreparedLine]
    ) -> Sequence[GuardFinding]:
        return self._findings


def _finding(code: str, *, blocking: bool) -> GuardFinding:
    return GuardFinding(violation=PostingViolation(code, "vi phạm thử"), blocking=blocking)


def test_run_guards_splits_blocking_from_warnings_and_runs_all_guards() -> None:
    """Chạy **hết** mọi guard rồi mới trả — người dùng sửa một lượt, không bị
    nhỏ giọt (cùng triết lý trả-toàn-bộ của bộ kiểm phase 4)."""
    registry = PostingGuardRegistry()
    registry.register(_FixedGuard([_finding("guard.block", blocking=True)]))
    registry.register(_FixedGuard([_finding("guard.warn", blocking=False)]))

    blocking, warnings = run_guards(_NO_SESSION, voucher=_NO_VOUCHER, lines=(), registry=registry)

    assert [violation.code for violation in blocking] == ["guard.block"]
    assert [violation.code for violation in warnings] == ["guard.warn"]


def test_warnings_are_stamped_at_the_pipeline_not_by_each_guard() -> None:
    """Dấu `warning=1` đóng ở một chỗ cho mọi guard: client phân biệt "xác nhận
    được" bằng khóa này, và một guard quên tự đóng dấu không được phép làm hộp
    xác nhận biến mất."""
    registry = PostingGuardRegistry()
    registry.register(_FixedGuard([_finding("guard.warn", blocking=False)]))

    _blocking, warnings = run_guards(_NO_SESSION, voucher=_NO_VOUCHER, lines=(), registry=registry)

    assert warnings[0].details[GUARD_WARNING_DETAIL] == 1
    assert warnings[0].as_json()["details"][GUARD_WARNING_DETAIL] == 1


def test_blocking_findings_carry_no_warning_stamp() -> None:
    """Vi phạm chặn không mang dấu xác nhận — lẫn dấu vào đó là mời client
    hiện nút "Vẫn ghi sổ?" cho một thứ gửi lại kiểu gì cũng bị từ chối."""
    registry = PostingGuardRegistry()
    registry.register(_FixedGuard([_finding("guard.block", blocking=True)]))

    blocking, _warnings = run_guards(_NO_SESSION, voucher=_NO_VOUCHER, lines=(), registry=registry)

    assert GUARD_WARNING_DETAIL not in blocking[0].details


def test_an_empty_registry_finds_nothing() -> None:
    blocking, warnings = run_guards(
        _NO_SESSION, voucher=_NO_VOUCHER, lines=(), registry=PostingGuardRegistry()
    )
    assert blocking == [] and warnings == []
