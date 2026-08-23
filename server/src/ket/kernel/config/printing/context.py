"""Hợp đồng dữ liệu giữa module nghiệp vụ và mẫu in (lát 6E-2).

Mẫu 01-TT/02-TT (TT99 Phụ lục I) đòi những trường mà **chỉ module sở hữu
chứng từ** mới biết: "Họ và tên người nộp tiền", "Lý do nộp", "Kèm theo …
chứng từ gốc", tài khoản ngân hàng người thụ hưởng (FR-SYS-033)… Bản in lại
dựng ở tầng api (chỗ duy nhất được ráp `posting` với `reporting`, contract C5),
nên phải có một hình dạng chung để module trả về mà không tầng nào phải biết
tên loại chứng từ nào.

Vì sao ở **kernel**: `posting` khai chỗ cắm (`PostingDocumentType.print_details`),
`modules` điền, `reporting` đổ vào mẫu, `api` ráp — bốn tầng, và kernel là tầng
duy nhất cả bốn đều import được (C1..C5).

Mọi giá trị đã là **CHUỖI định dạng sẵn** (`kernel/formatting.py`) — cùng
nguyên tắc với `report_table`: mẫu chỉ đổ khuôn, không tính toán, không định
dạng. Sáu ô chứa tương ứng sáu vùng có thật trên biểu mẫu giấy, không phải sáu
chỗ trống cho tương lai.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrintField:
    """Một dòng "Nhãn: giá trị" trên bản in."""

    label: str
    value: str


@dataclass(frozen=True)
class PrintTableColumn:
    """Một cột của bảng chi tiết — `align` khớp lớp CSS của `print_base.css`."""

    label: str
    align: str = "text"
    """`text` (trái) · `center` · `money` (phải, canh theo hàng đơn vị)."""

    width: str | None = None
    """Bề rộng CSS (`"12%"`); `None` = cột co giãn theo phần còn lại."""


@dataclass(frozen=True)
class PrintTable:
    """Bảng chi tiết trên bản in — mọi ô đã là chuỗi."""

    columns: tuple[PrintTableColumn, ...]
    rows: tuple[tuple[str, ...], ...]
    caption: str | None = None
    total_row: tuple[str, ...] | None = None
    """Dòng cộng in đậm ở cuối bảng; `None` = bảng không có dòng cộng."""

    def __post_init__(self) -> None:
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(
                    f"Dòng {index} có {len(row)} ô, bảng khai {width} cột — "
                    "lệch số ô là bản in lệch cột, bắt ngay lúc dựng"
                )
        if self.total_row is not None and len(self.total_row) != width:
            raise ValueError(f"Dòng cộng có {len(self.total_row)} ô, bảng khai {width} cột")


@dataclass(frozen=True)
class DocumentPrintDetails:
    """Phần riêng của một loại chứng từ trên bản in.

    Sáu vùng, đọc theo thứ tự mắt chạy trên tờ 01-TT:

    * `header_fields` — khối góc phải dưới tên mẫu ("Nợ:", "Có:", "Quyển số:");
    * `fields` — thân phiếu ("Họ và tên người nộp tiền", "Địa chỉ", "Lý do nộp");
    * `amount` + `amount_in_words` — dòng "Số tiền … (Viết bằng chữ) …";
    * `tables` — bảng chi tiết khi phiếu có nhiều dòng định khoản, hoặc bảng
      mệnh giá của biên bản kiểm kê (08a-TT);
    * `notes` — khối chân trang ("+ Tỷ giá ngoại tệ", "+ Số tiền quy đổi").
    """

    header_fields: tuple[PrintField, ...] = ()
    fields: tuple[PrintField, ...] = ()
    amount: str | None = None
    amount_in_words: str | None = None
    tables: tuple[PrintTable, ...] = ()
    notes: tuple[PrintField, ...] = ()


EMPTY_DETAILS: DocumentPrintDetails = DocumentPrintDetails()
"""Loại chứng từ chưa khai phần riêng (Phiếu kế toán GLE) — mẫu chỉ dùng phần
chung, không phải viết hàm rỗng cho mỗi loại."""
