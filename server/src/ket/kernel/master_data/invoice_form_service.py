"""Luật hợp nhất của danh mục mẫu số hóa đơn (FR-SYS-016, lát 7D).

Một hook duy nhất, và nó **luôn từ chối**. Đó không phải một chỗ chưa làm xong.

Cổng `test_master_data_merge` đòi hook vì `einvoices` mang khóa ngoại tới
`invoice_forms` **và** một ràng buộc duy nhất chứa cột ấy (`uq_einvoices_form_number`
— mỗi ký hiệu, mỗi số, một hóa đơn). Với mọi danh mục khác, câu trả lời cho ràng
buộc ấy là một luật hợp nhất: giữ dòng của bản đích, bỏ dòng của bản nguồn. Ở đây
thì không có dòng nào đáng bỏ, vì hai ký hiệu hóa đơn **không bao giờ là một bản
ghi bị khai hai lần**:

* mỗi ký hiệu là một dãy số riêng đã đăng ký với cơ quan thuế (BR-EIV-02), và
  gộp chúng là đổ hai dãy vào một — số `00000042` của ký hiệu nguồn và số
  `00000042` của ký hiệu đích cùng trở thành số `00000042` của một ký hiệu, tức
  hai tờ hóa đơn trùng số;
* những tờ hóa đơn đã phát hành thì **đã in ra và đã gửi cơ quan thuế** mang ký
  hiệu cũ. Đổi ký hiệu của chúng trong sổ là làm sổ nói khác tờ giấy.

Nói cách khác: cái mà một lần gộp phải làm được — biến hai bản ghi thành một mà
không mất thông tin — ở đây không tồn tại. Từ chối là câu trả lời đúng, không
phải một hạn chế kỹ thuật; cùng lối `ItemUnitOfItemMergeHook` từ chối hai mã
hàng khác đơn vị chính (H71).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ket.kernel.errors import MasterDataMergeRefusedError
from ket.kernel.master_data.models.invoice_form import INVOICE_FORM_TABLE_NAME


class InvoiceFormMergeHook:
    """Từ chối mọi lần gộp hai ký hiệu hóa đơn — xem docstring đầu tệp."""

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        raise MasterDataMergeRefusedError(
            "Hai ký hiệu hóa đơn không gộp được: mỗi ký hiệu là một dãy số riêng "
            "đã đăng ký với cơ quan thuế, và những hóa đơn đã phát hành mang ký "
            "hiệu cũ. Ngừng theo dõi ký hiệu không dùng nữa thay vì gộp",
            entity_type=INVOICE_FORM_TABLE_NAME,
            entity_id=source_id,
            reason="invoice_serials_are_never_duplicates",
        )

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không bao giờ chạy tới — `before_move` đã dừng mọi lần gộp."""
