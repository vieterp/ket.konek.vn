"""Máy trạng thái hóa đơn điện tử (`docs/srs/07` §3, phase-07 bước 8).

Cùng khuôn `posting/documents/state_machine.py`: một `dict` bất biến chứ không
chuỗi `if` rải trong service, và test duyệt **mọi** cặp (trạng thái, thao tác)
để chắc rằng cặp vắng mặt bị từ chối chứ không rơi vào nhánh mặc định nào.

Khác chứng từ ở một điểm đáng nói: bảng này có những cạnh mà **lát 7D chưa có
đường đi tới** — gửi email (7E), thay thế và điều chỉnh (7F). Chúng vẫn khai ở
đây, vì bảng chuyển là *đặc tả vòng đời* chứ không phải danh sách những gì đã
cài: `DA_THAY_THE` phải tồn tại từ bây giờ để `DA_PHAT_HANH → DA_THAY_THE`
không phải là thứ ai đó nghĩ ra lại từ đầu ở 7F, và `service` của lát này chỉ
gọi tới những cạnh nó thật sự cài (xem `EInvoiceService`).

**Ba cạnh mang bất biến riêng, không nằm ở bảng này:**

* `ISSUE` từ `PHAT_HANH_LOI` **không cấp số mới** — số đã tiêu, ADR-013 giữ
  nguyên nó. Bảng chuyển không nói được điều đó; `service.issue` nói, và
  trigger DB chặn nếu ai đó quên.
* `CANCEL` đòi đủ thông báo hủy **và** biên bản hủy (BR-EIV-04).
* `DELETE` chỉ hợp lệ ở `CHUA_PHAT_HANH`, đúng vì đó là trạng thái duy nhất
  chưa tiêu số.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from ket.kernel.errors import EInvoiceTransitionError
from ket.modules.einvoice.models import EInvoiceStatus


class EInvoiceAction(StrEnum):
    """Thao tác đổi trạng thái của một hóa đơn đã lập.

    Không có thành viên "Lập": lập hóa đơn là phép **tạo** dòng (chưa có trạng
    thái nguồn) nên nó là điểm vào, không phải một cạnh — cùng lý do
    `VoucherAction` không có "Cất".
    """

    ISSUE = "issue"
    """Cấp số + đưa vào hàng đợi truyền tải. Từ `PHAT_HANH_LOI` là **phát hành
    lại**, dùng lại số cũ."""
    CONFIRM = "confirm"
    """Nhà cung cấp / cơ quan thuế đã nhận."""
    REJECT = "reject"
    """Bị từ chối. Số vẫn giữ (ADR-013)."""
    SEND = "send"
    """Gửi bản thể hiện cho người mua (FR-EIV-020) — đường ghi ở 7E."""
    REPLACE = "replace"
    """Bị một hóa đơn khác thay thế (FR-EIV-030) — đường ghi ở 7F."""
    ADJUST = "adjust"
    """Bị một hóa đơn khác điều chỉnh (FR-EIV-033) — đường ghi ở 7F."""
    CANCEL = "cancel"
    """Hủy (FR-EIV-031/032, BR-EIV-04)."""
    DELETE = "delete"
    """Xóa hóa đơn chưa phát hành."""


TRANSITIONS: Final[dict[tuple[EInvoiceStatus, EInvoiceAction], EInvoiceStatus | None]] = {
    (EInvoiceStatus.CHUA_PHAT_HANH, EInvoiceAction.ISSUE): EInvoiceStatus.DANG_PHAT_HANH,
    (EInvoiceStatus.CHUA_PHAT_HANH, EInvoiceAction.DELETE): None,
    (EInvoiceStatus.DANG_PHAT_HANH, EInvoiceAction.CONFIRM): EInvoiceStatus.DA_PHAT_HANH,
    (EInvoiceStatus.DANG_PHAT_HANH, EInvoiceAction.REJECT): EInvoiceStatus.PHAT_HANH_LOI,
    # Phát hành lại sau khi bị từ chối (SRS 07 §3: PhatHanhLoi --> DaPhatHanh).
    # Đi lại qua `DANG_PHAT_HANH` chứ không nhảy thẳng: lượt gửi thứ hai cũng
    # phải qua hàng đợi và cũng có thể hỏng, nên một cạnh tắt sẽ là trạng thái
    # "đã phát hành" gán trước khi cơ quan thuế nói gì.
    (EInvoiceStatus.PHAT_HANH_LOI, EInvoiceAction.ISSUE): EInvoiceStatus.DANG_PHAT_HANH,
    # Bỏ hẳn một số đã cấp: không xóa dòng, mà hủy nó — số không bao giờ quay
    # lại dãy (ADR-013), và biên bản hủy số là thứ giải trình chỗ trống ấy.
    (EInvoiceStatus.PHAT_HANH_LOI, EInvoiceAction.CANCEL): EInvoiceStatus.DA_HUY,
    (EInvoiceStatus.DA_PHAT_HANH, EInvoiceAction.SEND): EInvoiceStatus.DA_GUI,
    (EInvoiceStatus.DA_PHAT_HANH, EInvoiceAction.REPLACE): EInvoiceStatus.DA_THAY_THE,
    (EInvoiceStatus.DA_PHAT_HANH, EInvoiceAction.ADJUST): EInvoiceStatus.DA_DIEU_CHINH,
    (EInvoiceStatus.DA_PHAT_HANH, EInvoiceAction.CANCEL): EInvoiceStatus.DA_HUY,
    (EInvoiceStatus.DA_GUI, EInvoiceAction.REPLACE): EInvoiceStatus.DA_THAY_THE,
    (EInvoiceStatus.DA_GUI, EInvoiceAction.ADJUST): EInvoiceStatus.DA_DIEU_CHINH,
    (EInvoiceStatus.DA_GUI, EInvoiceAction.CANCEL): EInvoiceStatus.DA_HUY,
}
"""Mọi cạnh hợp lệ. Vắng mặt = bị từ chối — không có nhánh mặc định.

Ba trạng thái cuối (`DA_THAY_THE`, `DA_DIEU_CHINH`, `DA_HUY`) **không có cạnh
ra**, có chủ đích: một hóa đơn đã bị thay thế không quay lại được, và cách sửa
sai sót của chính hóa đơn thay thế là lập thêm một hóa đơn nữa, không phải hồi
sinh hóa đơn cũ. `DA_GUI` không có cạnh `SEND` thứ hai vì gửi lại email không
đổi trạng thái hóa đơn — nó đổi trạng thái **gửi** (FR-EIV-023), một trục khác.
"""


def transition(status: EInvoiceStatus, action: EInvoiceAction) -> EInvoiceStatus | None:
    """Trạng thái sau thao tác, hoặc `None` nếu thao tác xóa dòng.

    Cặp không có trong bảng → `EInvoiceTransitionError` mang cả hai vế, để
    client dựng được câu chỉ đúng bước còn thiếu.
    """
    key = (status, action)
    if key not in TRANSITIONS:
        raise EInvoiceTransitionError(
            "Thao tác không hợp lệ với trạng thái hiện tại của hóa đơn",
            status=status.value,
            action=action.value,
        )
    return TRANSITIONS[key]


def transition_to(status: EInvoiceStatus, action: EInvoiceAction) -> EInvoiceStatus:
    """Như `transition` nhưng cho thao tác **giữ dòng** — trả trạng thái mới.

    Gọi với `DELETE` là lỗi lập trình (thao tác xóa không có "trạng thái sau"),
    nên `ValueError` chứ không `DomainError`: nó phải nổ ở CI, không phải thành
    thông điệp dịu cho người dùng.
    """
    result = transition(status, action)
    if result is None:
        raise ValueError(f"Thao tác {action} xóa dòng — không có trạng thái sau")
    return result
