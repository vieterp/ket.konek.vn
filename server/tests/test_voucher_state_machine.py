"""Máy trạng thái chứng từ — duyệt **mọi** cặp (trạng thái, thao tác).

Giá trị của test không nằm ở các cạnh hợp lệ (chúng hiển nhiên) mà ở phần bù:
mọi cặp vắng mặt trong bảng chuyển phải bị từ chối bằng `VoucherTransitionError`
— không có nhánh mặc định nào cho chứng từ đi lối tắt.
"""

from __future__ import annotations

from itertools import product

import pytest

from ket.kernel.errors import VoucherTransitionError
from ket.posting.documents.models import VoucherStatus
from ket.posting.documents.state_machine import (
    TRANSITIONS,
    VoucherAction,
    transition,
    transition_to,
)


def test_the_declared_edges_are_exactly_the_business_flow() -> None:
    """SRS 00 §3.3: Cất → Ghi sổ, với đường lui tương ứng.

    Khóa sổ KHÔNG phải một cạnh (quyết định 4D): `DA_KHOA_SO` là trạng thái
    suy ra từ `accounting_periods.locked_at` lúc đọc, không ai ghi nó vào cột
    `status` — xem docstring `VoucherStatus.DA_KHOA_SO` và
    `posting/periods/lock_service.py`.
    """
    assert TRANSITIONS == {
        (VoucherStatus.DA_CAT, VoucherAction.POST): VoucherStatus.DA_GHI_SO,
        (VoucherStatus.DA_GHI_SO, VoucherAction.UNPOST): VoucherStatus.DA_CAT,
        (VoucherStatus.DA_CAT, VoucherAction.DELETE): None,
        (VoucherStatus.DA_CAT, VoucherAction.CANCEL): VoucherStatus.DA_HUY,
    }


@pytest.mark.parametrize(
    ("status", "action"),
    [pair for pair in product(VoucherStatus, VoucherAction) if pair not in TRANSITIONS],
)
def test_every_undeclared_pair_is_refused(status: VoucherStatus, action: VoucherAction) -> None:
    with pytest.raises(VoucherTransitionError) as caught:
        transition(status, action)
    assert caught.value.details["status"] == status.value
    assert caught.value.details["action"] == action.value


def test_locked_status_has_no_edges_because_it_is_derived() -> None:
    """`DA_KHOA_SO` không có cạnh vào/ra: nó không bao giờ nằm trong cột
    `status`, nên máy trạng thái từ chối mọi thao tác nhận nó làm trạng thái
    nguồn — hàng rào thật của kỳ khóa là RT-09 + trigger, không phải cạnh máy."""
    for action in VoucherAction:
        with pytest.raises(VoucherTransitionError):
            transition(VoucherStatus.DA_KHOA_SO, action)


def test_transition_to_returns_the_new_status() -> None:
    assert transition_to(VoucherStatus.DA_CAT, VoucherAction.POST) is VoucherStatus.DA_GHI_SO


def test_transition_to_refuses_the_delete_edge() -> None:
    """DELETE không có "trạng thái sau" — gọi qua `transition_to` là lỗi lập trình."""
    with pytest.raises(ValueError, match="không có trạng thái sau"):
        transition_to(VoucherStatus.DA_CAT, VoucherAction.DELETE)


def test_deleted_is_not_a_status_and_cancel_keeps_the_number() -> None:
    """Đã hủy là trạng thái cuối: không cạnh nào đi ra khỏi nó."""
    outgoing = [pair for pair in TRANSITIONS if pair[0] is VoucherStatus.DA_HUY]
    assert outgoing == []
