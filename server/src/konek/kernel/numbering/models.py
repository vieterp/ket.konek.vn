"""Bộ đếm số chứng từ — bảng ở đây, thuật toán cấp số ở phase 3 (RT-12).

Kế toán VN có hai loại số với hai yêu cầu khác hẳn nhau:

* **Liên tục, không đứt quãng** (hóa đơn): mất số phải giải trình. Cấp bằng
  `SELECT … FOR UPDATE` trên đúng dòng bộ đếm, **trong** transaction ghi chứng
  từ — nối tiếp nhau, chậm hơn, nhưng không thủng dãy.
* **Nội bộ** (phiếu thu, phiếu nhập): đứt quãng chấp nhận được, ưu tiên không
  chặn nhau khi nhiều người cùng nhập.

`allow_gaps` khai loại nào là loại nào ngay trên dữ liệu, để phase 3 không phải
đoán và để người triển khai đổi được mà không sửa code (FR-NFR-055).

Bộ đếm nằm **trong schema dataset**, nên hai doanh nghiệp đánh số độc lập —
một hệ quả trực tiếp của ADR-017.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from konek.kernel.persistence.base import DatasetBase


class NumberSequence(DatasetBase):
    """Một dãy số: khóa phạm vi → giá trị kế tiếp.

    Cố ý **không** `Audited`: mỗi lần cấp số là một `UPDATE` trên đường nóng
    nhất của hệ (trong chính transaction ghi chứng từ), nên ghi vết nó vừa làm
    chậm vừa nhấn chìm những dòng nhật ký mà kiểm toán viên thật sự cần đọc.
    Dấu vết của số đã cấp nằm ở **chứng từ** mang số đó; việc con người đổi
    *cấu hình* dãy số sẽ được ghi ở tầng dịch vụ (phase 3) bằng
    `record_action`, chứ không phải mỗi lần bộ đếm nhích lên.
    """

    __tablename__ = "number_sequences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    scope_key: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    """Phạm vi dãy số, ví dụ `cash_receipt/2026/HN`. Chi nhánh và năm nằm
    **trong** khóa chứ không thành cột riêng: quy tắc "đánh số theo gì" là cấu
    hình của người dùng, và mỗi lần nó đổi mà phải `ALTER TABLE` thì đã vi phạm
    "cấu hình thay vì sửa code"."""

    prefix: Mapped[str] = mapped_column(String(50), nullable=False, default="", server_default="")
    padding: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    next_value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )

    allow_gaps: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    """`False` = số phải liên tục (hóa đơn) → cấp số nối tiếp nhau trong
    transaction. `True` = cho phép đứt quãng (chứng từ nội bộ)."""
