"""Bảng chuyển trạng thái hóa đơn điện tử (SRS 07 §3, lát 7D).

Hàm thuần trên một `dict` bất biến — không cần PostgreSQL. Bài quan trọng nhất
ở đây là bài **duyệt hết**: mọi cặp (trạng thái, thao tác) không có trong bảng
phải bị từ chối, kể cả những cặp không ai nghĩ tới. Một nhánh mặc định lọt vào
`transition` sẽ làm bài ấy đỏ ngay, và đó là lý do bảng chuyển là dữ liệu chứ
không phải một chuỗi `if`.
"""

from __future__ import annotations

import pytest

from ket.kernel.errors import EInvoiceTransitionError
from ket.modules.einvoice.models import EInvoiceStatus
from ket.modules.einvoice.state_machine import (
    TRANSITIONS,
    EInvoiceAction,
    transition,
    transition_to,
)

_TERMINAL = (EInvoiceStatus.DA_THAY_THE, EInvoiceStatus.DA_DIEU_CHINH, EInvoiceStatus.DA_HUY)


def test_every_pair_outside_the_table_is_refused() -> None:
    """Vắng mặt trong bảng = bị từ chối, không rơi vào nhánh mặc định nào."""
    for status in EInvoiceStatus:
        for action in EInvoiceAction:
            if (status, action) in TRANSITIONS:
                continue
            with pytest.raises(EInvoiceTransitionError) as raised:
                transition(status, action)
            # Thông điệp phải mang **cả hai vế** để client dựng được câu chỉ
            # đúng bước còn thiếu, thay vì một câu "không làm được" chung chung.
            assert raised.value.details["status"] == status.value
            assert raised.value.details["action"] == action.value


def test_only_a_draft_can_be_deleted() -> None:
    """Xóa hợp lệ ở đúng một trạng thái — trạng thái duy nhất chưa tiêu số."""
    deletable = {
        status for status in EInvoiceStatus if (status, EInvoiceAction.DELETE) in TRANSITIONS
    }
    assert deletable == {EInvoiceStatus.CHUA_PHAT_HANH}
    assert transition(EInvoiceStatus.CHUA_PHAT_HANH, EInvoiceAction.DELETE) is None


def test_reissuing_after_a_failure_goes_back_through_the_queue() -> None:
    """`PHAT_HANH_LOI --ISSUE-->` phải quay lại `DANG_PHAT_HANH`, không nhảy thẳng.

    Lượt gửi thứ hai cũng qua hàng đợi và cũng có thể hỏng; một cạnh tắt tới
    `DA_PHAT_HANH` là gán trạng thái "đã phát hành" trước khi cơ quan thuế nói
    gì.
    """
    assert (
        transition_to(EInvoiceStatus.PHAT_HANH_LOI, EInvoiceAction.ISSUE)
        is EInvoiceStatus.DANG_PHAT_HANH
    )


def test_the_three_end_states_have_no_way_out() -> None:
    """Đã thay thế / đã điều chỉnh / đã hủy là ngõ cụt, có chủ đích.

    Cách sửa sai sót của một hóa đơn thay thế là lập thêm một hóa đơn nữa,
    không phải hồi sinh hóa đơn cũ — hồi sinh được nghĩa là một tờ đã báo hủy
    với cơ quan thuế lại sống lại trong sổ.
    """
    for status in _TERMINAL:
        assert not [action for action in EInvoiceAction if (status, action) in TRANSITIONS]

    # Ghim đúng ba con số: chỉ mục riêng phần `uq_einvoices_live_source_voucher`
    # gõ thẳng `status NOT IN (5, 6, 7)` (bộ quét SQL cấm nội suy vào `text()`),
    # nên đổi giá trị enum mà quên sửa vị từ ấy sẽ để một hóa đơn đã hủy tiếp
    # tục chiếm chỗ chứng từ gốc của nó.
    assert [int(status) for status in _TERMINAL] == [5, 6, 7]


def test_transition_to_refuses_the_delete_edge() -> None:
    """Gọi `transition_to` với `DELETE` là lỗi lập trình, phải nổ ở CI."""
    with pytest.raises(ValueError, match="không có trạng thái sau"):
        transition_to(EInvoiceStatus.CHUA_PHAT_HANH, EInvoiceAction.DELETE)


def test_no_edge_leaves_an_issued_invoice_below_the_immutable_floor() -> None:
    """Không cạnh nào đưa hóa đơn đã phát hành về lại vùng sửa được.

    Ngưỡng bất biến (`status >= DA_PHAT_HANH`) chỉ có nghĩa nếu nó **một
    chiều**: một cạnh đưa `DA_GUI` về `CHUA_PHAT_HANH` sẽ mở lại toàn bộ nội
    dung hóa đơn cho trigger DB đi qua hợp lệ, và BR-EIV-01 mất hiệu lực mà
    không dòng nào của trigger sai.
    """
    floor = EInvoiceStatus.DA_PHAT_HANH
    for (status, action), target in TRANSITIONS.items():
        if status < floor or target is None:
            continue
        assert target >= floor, f"cạnh {status.name} --{action}--> {target.name} phá ngưỡng"
