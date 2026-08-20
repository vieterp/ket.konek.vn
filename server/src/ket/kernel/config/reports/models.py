"""Bốn bảng metadata của report engine (FR-RPT-001..003).

`report_datasets`/`report_layouts`/`report_param_sets` là ba mảnh ghép độc
lập; `report_definitions` ráp chúng thành một báo cáo có mã. Trần ≤30 dataset
phủ ~155 báo cáo (phase-05 §Chiến lược quy mô) sống được là nhờ tách này:
một dataset × nhiều layout = nhiều báo cáo, không dòng SQL nào lặp lại.

Cấu hình dùng chung toàn dataset, không RLS — cùng nhóm `chart_of_accounts`/
`statement_layouts` (0009 §RLS): cô lập chi nhánh nằm ở RLS của các bảng gốc
mà `sql_text` đọc, không nằm ở bảng metadata.

`spec` của layout/param set là JSONB nhưng **không ai đọc nó dạng dict thô**
(LD-13): mọi đường đọc đi qua `spec.parse_layout_spec`/`parse_param_set_spec`
— hình dạng sai nổ thành lỗi cấu hình ngay lúc gieo/nhập, không thành KeyError
giữa một lượt render.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.config.accounts_models import NAME_MAX_LENGTH
from ket.kernel.persistence.base import DatasetBase

REPORT_CODE_MAX_LENGTH = 50
"""Mã báo cáo/dataset/layout/bộ tham số — nằm trên URL path nên có ràng ký tự
(cùng bài học M-3 review 5B với mã layout BCTC)."""

CATEGORY_MAX_LENGTH = 50
DESCRIPTION_MAX_LENGTH = 500

type SpecDocument = dict[str, object]
"""JSONB thô của cột `spec` — KHÔNG đọc trực tiếp (LD-13); đi qua `spec.py`."""


class LayoutKind:
    """Cách renderer đọc `spec`. Hằng chuỗi, không Enum — giá trị nằm trong
    JSON builtin và CHECK constraint, cùng khuôn `StatementKind`."""

    TABLE = "table"
    GROUPED = "grouped"
    STATEMENT = "statement"
    FORM = "form"

    ALL = frozenset({TABLE, GROUPED, STATEMENT, FORM})


class LedgerScope:
    """Báo cáo thuộc sổ nào (BR-RPT-04). `BOTH` = người xem chọn bằng tham số."""

    BOTH = "both"
    FINANCIAL = "financial"
    MANAGEMENT = "management"

    ALL = frozenset({BOTH, FINANCIAL, MANAGEMENT})


_KIND_SQL_LIST = ", ".join(f"'{kind}'" for kind in sorted(LayoutKind.ALL))
_SCOPE_SQL_LIST = ", ".join(f"'{scope}'" for scope in sorted(LedgerScope.ALL))


class ReportDataset(DatasetBase, Audited):
    """Một câu SQL tham số hóa — nguồn dữ liệu cho một họ báo cáo.

    `sql_text` chỉ chứa placeholder có tên (`:from_date`, …); engine bind qua
    SQLAlchemy `text()`, không nối chuỗi. `allowed_params` là danh sách đóng
    các placeholder được phép — seed/importer đối chiếu fail-closed trước khi
    ghi, engine đối chiếu lần nữa trước khi chạy.

    `is_builtin = false` (gói `.zip` nhập ngoài, đường nhập đến ở 5D) phải chạy
    bằng role read-only RLS-bound (RT-07) — engine hiện **từ chối** dataset
    không-builtin cho tới khi có role đó, fail-closed thay vì chạy nhầm quyền.
    """

    __tablename__ = "report_datasets"
    __table_args__ = (CheckConstraint("sql_text <> ''", name="sql_text_not_blank"),)

    code: Mapped[str] = mapped_column(String(REPORT_CODE_MAX_LENGTH), primary_key=True)
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_params: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    supports_branch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    supports_ledger: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_builtin: Mapped[bool] = mapped_column(
        # KHÔNG-tin-cậy là mặc định (review 5C, M1): quên khai `is_builtin` khi
        # tạo dataset = dataset không chạy được (RT-07), không phải dataset
        # chạy bằng quyền runtime đầy đủ. Chỉ seed builtin đặt True tường minh.
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)


class ReportLayout(DatasetBase, Audited):
    """Cách trình bày một dataset: cột, nhóm, tổng, khổ giấy (`spec.LayoutSpec`)."""

    __tablename__ = "report_layouts"
    __table_args__ = (CheckConstraint(f"kind IN ({_KIND_SQL_LIST})", name="kind_known"),)

    code: Mapped[str] = mapped_column(String(REPORT_CODE_MAX_LENGTH), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    spec: Mapped[SpecDocument] = mapped_column(JSONB, nullable=False)
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=True
    )
    """Layout theo mẫu thông tư thuộc một gói cấu hình; NULL = không phụ thuộc
    chế độ kế toán (sổ quản trị, bảng kê nội bộ)."""


class ReportParamSet(DatasetBase, Audited):
    """Tham số NGOÀI bộ chuẩn của một báo cáo (`spec.ParamSetSpec`).

    Bộ chuẩn FR-RPT-002 (`from_date`, `to_date`, `branch_ids`, `ledger`) áp cho
    mọi báo cáo, không khai lại ở đây — spec chỉ khai phần thêm (mã TK, ngưỡng
    tuổi nợ, chiều phân tích FR-RPT-003…).
    """

    __tablename__ = "report_param_sets"

    code: Mapped[str] = mapped_column(String(REPORT_CODE_MAX_LENGTH), primary_key=True)
    spec: Mapped[SpecDocument] = mapped_column(JSONB, nullable=False)


class ReportDefinition(DatasetBase, Audited):
    """Một báo cáo người dùng gọi được: mã + tên + ba mảnh ghép.

    `code` nằm trên URL (`POST /reports/{code}/render`) — ràng ký tự ở CHECK.
    """

    __tablename__ = "report_definitions"
    __table_args__ = (
        CheckConstraint("code ~ '^[A-Za-z0-9][A-Za-z0-9_-]*$'", name="code_url_safe"),
        CheckConstraint(f"ledger_scope IN ({_SCOPE_SQL_LIST})", name="ledger_scope_known"),
        UniqueConstraint("code", name="uq_report_definitions_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(REPORT_CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    category: Mapped[str] = mapped_column(String(CATEGORY_MAX_LENGTH), nullable=False)
    module: Mapped[str] = mapped_column(String(CATEGORY_MAX_LENGTH), nullable=False)
    dataset_code: Mapped[str] = mapped_column(
        ForeignKey("report_datasets.code", ondelete="RESTRICT"), nullable=False
    )
    layout_code: Mapped[str] = mapped_column(
        ForeignKey("report_layouts.code", ondelete="RESTRICT"), nullable=False
    )
    param_set_code: Mapped[str] = mapped_column(
        ForeignKey("report_param_sets.code", ondelete="RESTRICT"), nullable=False
    )
    ledger_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LedgerScope.BOTH, server_default=LedgerScope.BOTH
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=True
    )
