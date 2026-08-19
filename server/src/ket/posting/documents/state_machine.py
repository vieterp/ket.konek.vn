"""Máy trạng thái chứng từ — bảng chuyển tường minh (SRS 00 §3.3, phase-04 bước 2).

Một `dict` bất biến chứ không phải chuỗi `if` rải trong service: thêm loại
chuyển mới (phase 7 có Hủy-giữ-số) là thêm một dòng dữ liệu mà người review
nhìn thấy, và test duyệt được **mọi** cặp (trạng thái, thao tác) — kể cả những
cặp không ai nghĩ tới — để chắc rằng chúng bị từ chối chứ không rơi vào nhánh
mặc định nào đó.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from ket.kernel.errors import VoucherTransitionError
from ket.posting.documents.models import VoucherStatus


class VoucherAction(StrEnum):
    """Thao tác đổi trạng thái của một chứng từ đã cất.

    Không có thành viên "Cất": Cất là phép **tạo** dòng (chưa có trạng thái
    nguồn), nên nó không phải một cạnh của máy trạng thái — nó là điểm vào.
    """

    POST = "post"
    UNPOST = "unpost"
    DELETE = "delete"
    CANCEL = "cancel"


TRANSITIONS: Final[dict[tuple[VoucherStatus, VoucherAction], VoucherStatus | None]] = {
    (VoucherStatus.DA_CAT, VoucherAction.POST): VoucherStatus.DA_GHI_SO,
    (VoucherStatus.DA_GHI_SO, VoucherAction.UNPOST): VoucherStatus.DA_CAT,
    # `None` = dòng biến mất: Xóa chỉ hợp lệ ở Đã cất, và số chứng từ **không**
    # tái sử dụng (bảng chuyển của phase-04) — dãy có thể thủng với chứng từ
    # nội bộ (`allow_gaps=True`), còn dãy liên tục thì đi đường Hủy giữ số.
    (VoucherStatus.DA_CAT, VoucherAction.DELETE): None,
    (VoucherStatus.DA_CAT, VoucherAction.CANCEL): VoucherStatus.DA_HUY,
}
"""Mọi cạnh hợp lệ. Vắng mặt = bị từ chối — không có nhánh mặc định.

Khóa/mở kỳ **không phải** một cạnh ở đây (quyết định 4D): `DA_KHOA_SO` là
trạng thái suy ra từ `accounting_periods.locked_at` lúc đọc, không ai ghi nó
xuống hàng (xem docstring `VoucherStatus.DA_KHOA_SO`). Chứng từ trong kỳ đã
khóa vẫn bất động — nhưng bởi `PostingService` kiểm kỳ (FOR SHARE, RT-09) và
trigger `gl_postings_period_guard`, không phải bởi một trạng thái được chép
xuống từng dòng."""


def transition(status: VoucherStatus, action: VoucherAction) -> VoucherStatus | None:
    """Trạng thái sau thao tác, hoặc `None` nếu thao tác xóa dòng.

    Cặp không có trong bảng → `VoucherTransitionError` mang cả hai vế, để
    client dựng câu chỉ đúng bước còn thiếu ("đã ghi sổ nên không xóa được —
    bỏ ghi sổ trước").
    """
    key = (status, action)
    if key not in TRANSITIONS:
        raise VoucherTransitionError(
            "Thao tác không hợp lệ với trạng thái hiện tại của chứng từ",
            status=status.value,
            action=action.value,
        )
    return TRANSITIONS[key]


def transition_to(status: VoucherStatus, action: VoucherAction) -> VoucherStatus:
    """Như `transition` nhưng cho thao tác **giữ dòng** — trả trạng thái mới.

    Gọi với `DELETE` là lỗi lập trình (thao tác xóa không có "trạng thái sau"),
    nên `ValueError` chứ không `DomainError`: nó phải nổ ở CI, không phải thành
    thông điệp dịu cho người dùng.
    """
    result = transition(status, action)
    if result is None:
        raise ValueError(f"Thao tác {action} xóa dòng — không có trạng thái sau")
    return result
