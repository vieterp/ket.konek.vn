"""Mẫu số hóa đơn (`docs/srs/01` §7, `docs/srs/08`).

Danh mục mẫu số / ký hiệu hóa đơn mà doanh nghiệp đã đăng ký với cơ quan thuế.

Chỉ có bộ cột chung ở lát này. Mẫu số, ký hiệu, dải số được phép và trạng thái
thông báo phát hành thuộc phase 7 (Mua – Bán – Công nợ & HĐĐT): chúng chỉ có
nghĩa cùng với luồng phát hành hóa đơn, và đoán trước hình dạng của chúng ở đây
là đoán trước hợp đồng với nhà cung cấp hóa đơn điện tử mà LD-10 còn chưa chốt
(adapter 1–2 nhà cung cấp).
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

INVOICE_FORM_TABLE_NAME = "invoice_forms"


class InvoiceForm(MasterDataRow):
    """Một mẫu số hóa đơn đã đăng ký."""

    __tablename__ = INVOICE_FORM_TABLE_NAME
    __table_args__ = master_data_table_args(INVOICE_FORM_TABLE_NAME)
