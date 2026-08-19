"""Báo cáo của một lượt nhập số dư ban đầu — đi vào `jobs.result` (khuôn H78).

Model riêng chứ không tái dùng `excel.report.ImportReport`: báo cáo kia nói về
một *danh mục* (`catalog`, `mode`, `missing_reference`), còn ở đây một lượt nói
về **một năm tài chính, một sổ, bốn sheet** — và điều người dùng cần đọc trước
khi bấm ghi là hai con số tổng dư Nợ/dư Có sẽ hình thành (FR-OPB-006), thứ
`ImportReport` không có chỗ đặt.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pydantic import BaseModel, Field

MAX_REPORTED_ERRORS: Final[int] = 500
"""Cùng trần và cùng lý do với `excel.report.MAX_REPORTED_ERRORS`: quá mức này
thì vấn đề là cả tệp sai chứ không phải từng dòng."""


class OpeningRowError(BaseModel):
    """Một lỗi gắn với một ô — như `excel.report.RowError`, thêm chiều `sheet`.

    Thêm `sheet` vì workbook có bốn sheet dữ liệu: "dòng 12" không đủ nghĩa khi
    có bốn dòng 12 khác nhau.
    """

    sheet: str
    row: int
    column: str | None
    code: str
    message: str
    value: str | None = None


class OpeningImportWarning(BaseModel):
    """Một cảnh báo cho cả lượt — không chặn ghi (FR-OPB-006 nói *cảnh báo* khi lệch)."""

    code: str
    message: str


class OpeningImportReport(BaseModel):
    """Kết quả một lượt kiểm/ghi số dư ban đầu.

    Một model cho cả hai bước, cùng lý do với `ImportReport` (H85): lượt ghi
    chạy lại đúng phép kiểm ấy nên có cùng thứ để kể.
    """

    fiscal_year_id: int
    ledger: int
    file_name: str
    content_hash: str

    total_rows: int = 0
    error_rows: int = 0
    errors: list[OpeningRowError] = Field(default_factory=list)
    warnings: list[OpeningImportWarning] = Field(default_factory=list)

    rows_by_kind: dict[int, int] = Field(default_factory=dict)
    """`{detail_kind: số dòng opening_balances sẽ ghi}` — sau khi gộp hóa đơn
    về dòng cha, nên nó khác `total_rows` của tệp."""

    invoice_rows: int = 0
    """Số dòng chi tiết hóa đơn/tạm ứng sẽ ghi (FR-OPB-003)."""

    replaced_kinds: list[int] = Field(default_factory=list)
    """Nhóm sẽ bị **thay trọn** trong phạm vi (năm, sổ, chi nhánh). Sheet vắng
    mặt hoặc không có dòng nào thì nhóm đó không đụng tới — con số này là câu
    trả lời cho "lượt ghi này xóa gì"."""

    total_debit: Decimal = Decimal(0)
    total_credit: Decimal = Decimal(0)
    """Tổng dư Nợ/Có **sau** lượt ghi trong phạm vi (năm, sổ, chi nhánh): dòng
    mới cộng dòng của các nhóm không bị thay. Lệch nhau = cảnh báo BR-OPB-01."""

    committed: bool = False

    def add(self, error: OpeningRowError) -> None:
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(error)

    @property
    def is_valid(self) -> bool:
        return self.error_rows == 0

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit
