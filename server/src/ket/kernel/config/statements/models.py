"""Schema layout BCTC: `statement_layouts` + `statement_rows` (FR-GLE-043).

Layout thuộc **gói cấu hình** (khóa duy nhất `(package_id, code)`), cùng lý do
với `chart_of_accounts`: TT99 và TT133 là hai bộ mẫu BCTC khác nhau cùng tồn
tại, kích hoạt gói nào thì đọc bộ mẫu của gói đó (LD-06, FR-NFR-070). Đổi mẫu
khi thông tư sửa = phát hành gói mới, không sửa code (FR-NFR-055).

`formula` là văn bản theo grammar của `formula/parser.py` — loader gói cấu hình
parse **trước khi ghi** (fail-closed), nên một dòng nằm trong bảng này là một
công thức chắc chắn đọc được. Khác phác thảo phase-05: không có cột `sign` —
dấu âm nằm ngay trong công thức (`0 - CR(2294)`), hai cơ chế dấu song song chỉ
tạo chỗ cho chúng lệch nhau.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.config.accounts_models import NAME_MAX_LENGTH
from ket.kernel.persistence.base import DatasetBase

LAYOUT_CODE_MAX_LENGTH = 50
"""`B01-DN`, `B02-DN`, `B01A-DNN` — mã mẫu theo thông tư."""

ROW_CODE_MAX_LENGTH = 10
"""`110`, `411a`, `420b` — "Mã số" chỉ tiêu trên mẫu. Không thuần số."""

LABEL_MAX_LENGTH = 500
NOTE_REF_MAX_LENGTH = 20
FORMULA_MAX_LENGTH = 1000
ROW_NOTE_MAX_LENGTH = 1000


class StatementKind:
    """Loại báo cáo của một layout — quyết định ngữ nghĩa hai cột số.

    `BALANCE_SHEET` đọc **số dư tại thời điểm** (cột chính = cuối kỳ, cột so
    sánh = đầu năm); `INCOME`/`CASHFLOW` đọc **phát sinh trong khoảng** (cột
    chính = lũy kế từ đầu năm tới cuối kỳ, cột so sánh = cả năm trước). Hằng số
    chuỗi chứ không Enum — giá trị đi thẳng vào JSON gói cấu hình và CHECK.
    """

    BALANCE_SHEET = "balance_sheet"
    INCOME = "income"
    CASHFLOW = "cashflow"
    NOTES = "notes"

    ALL = frozenset({BALANCE_SHEET, INCOME, CASHFLOW, NOTES})


_KIND_SQL_LIST = ", ".join(f"'{kind}'" for kind in sorted(StatementKind.ALL))


class StatementLayout(DatasetBase, Audited):
    """Một mẫu BCTC của một gói cấu hình (`B01-DN` của TT99, `B01A-DNN` của TT133)."""

    __tablename__ = "statement_layouts"
    __table_args__ = (
        CheckConstraint("code <> ''", name="code_not_blank"),
        CheckConstraint(f"statement_kind IN ({_KIND_SQL_LIST})", name="statement_kind_known"),
        UniqueConstraint("package_id", "code", name="uq_statement_layouts_package_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(LAYOUT_CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    statement_kind: Mapped[str] = mapped_column(String(20), nullable=False)


class StatementRow(DatasetBase, Audited):
    """Một chỉ tiêu trên mẫu: "Mã số" + nhãn + công thức (FR-GLE-043).

    `display_order` là thứ tự trình bày trên mẫu, tách khỏi `row_code` vì mã số
    thông tư không đơn điệu (`411a` chen giữa `411` và `412`). Evaluator chạy
    theo thứ tự tô-pô của rowref, không theo cột này.

    `note` là chỗ ghi quyết định nhập liệu của từng chỉ tiêu (ví dụ "chi tiết
    ngắn/dài hạn theo đối tượng — gói builtin mặc định đặt toàn bộ vào chỉ tiêu
    ngắn hạn") — dữ liệu cần kế toán trưởng duyệt phải nói được nó đã chọn gì.
    """

    __tablename__ = "statement_rows"
    __table_args__ = (
        CheckConstraint("row_code <> ''", name="row_code_not_blank"),
        CheckConstraint("formula <> ''", name="formula_not_blank"),
        UniqueConstraint("layout_id", "row_code", name="uq_statement_rows_layout_row_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    layout_id: Mapped[int] = mapped_column(
        ForeignKey("statement_layouts.id", ondelete="RESTRICT"), nullable=False
    )
    row_code: Mapped[str] = mapped_column(String(ROW_CODE_MAX_LENGTH), nullable=False)
    label: Mapped[str] = mapped_column(String(LABEL_MAX_LENGTH), nullable=False)
    label_en: Mapped[str | None] = mapped_column(String(LABEL_MAX_LENGTH), nullable=True)
    note_ref: Mapped[str | None] = mapped_column(String(NOTE_REF_MAX_LENGTH), nullable=True)
    """Cột "Thuyết minh" trên mẫu — trỏ sang chỉ tiêu thuyết minh B09 (phase 10a)."""

    formula: Mapped[str] = mapped_column(String(FORMULA_MAX_LENGTH), nullable=False)
    indent_level: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_bold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    hide_when_zero: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    note: Mapped[str | None] = mapped_column(String(ROW_NOTE_MAX_LENGTH), nullable=True)
