"""Loại chứng từ (`docs/srs/01` §7, FR-SYS-063).

Danh mục phân loại chứng từ: phiếu thu, phiếu chi, hóa đơn bán hàng, phiếu nhập
kho. Nó là thứ mà `number_sequences.document_type` trỏ tới — theo **mã**, không
phải khóa ngoại, vì dịch vụ đánh số nhận `document_type` là một chuỗi phạm vi và
phải cấp được số cho cả loại chứng từ do gói cấu hình khai (phase 5).

Lớp tên `DocumentTypeCatalog` chứ không `DocumentType`: cái tên thứ hai đã thuộc
về `kernel/security/permissions.py`, nơi nó là **mục đăng ký sinh mã quyền** cho
mọi loại chứng từ *và* mọi danh mục — kể cả cho chính bảng này. Hai khái niệm
khác nhau mang cùng một tên trong cùng một tệp registry là chỗ nhầm lẫn không
tránh khỏi.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

DOCUMENT_TYPE_TABLE_NAME = "document_types"


class DocumentTypeCatalog(MasterDataRow):
    """Một loại chứng từ: quyết định quy tắc đánh số và mẫu in."""

    __tablename__ = DOCUMENT_TYPE_TABLE_NAME
    __table_args__ = master_data_table_args(DOCUMENT_TYPE_TABLE_NAME)
