"""Bảng `print_templates` — mẫu in chứng từ là DỮ LIỆU (FR-RPT-008, FR-NFR-055).

Một mẫu = Jinja2 HTML dùng class của design system + CSS phụ; render luôn qua
`SandboxedEnvironment` + `asset_url_fetcher` allowlist (RT-01) bất kể builtin
hay không — xem `reporting/rendering/environment.py`. Mẫu không-builtin (gói
nhập ngoài, người dùng sửa) chỉ nhận context DỮ LIỆU đã định dạng sẵn, không
hàm/không filter tùy biến.

Cấu hình dùng chung toàn dataset, không RLS — cùng nhóm `report_layouts`
(0013): cô lập chi nhánh nằm ở dữ liệu chứng từ được đổ vào mẫu, không ở mẫu.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.config.reports.models import REPORT_CODE_MAX_LENGTH
from ket.kernel.persistence.base import DatasetBase

DOCUMENT_TYPE_MAX_LENGTH = 20
"""Soi gương `vouchers.document_type` (phase 4) — hai cột nối nhau theo mã."""

TEMPLATE_NAME_MAX_LENGTH = 200


class PrintTemplate(DatasetBase, Audited):
    """Một mẫu in cho một loại chứng từ.

    `is_default`: mẫu dùng khi người in không chọn mã cụ thể — mỗi loại chứng
    từ nhiều nhất một mặc định (index một phần bên dưới; hai dòng cùng bật sẽ
    chết ở DB chứ không im lặng chọn dòng may rủi).
    """

    __tablename__ = "print_templates"
    __table_args__ = (
        CheckConstraint("code ~ '^[A-Za-z0-9][A-Za-z0-9_-]*$'", name="code_url_safe"),
        CheckConstraint("html_template <> ''", name="html_template_not_blank"),
        UniqueConstraint("document_type", "code", name="uq_print_templates_type_code"),
        Index(
            "uq_print_templates_default_per_type",
            "document_type",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_type: Mapped[str] = mapped_column(String(DOCUMENT_TYPE_MAX_LENGTH), nullable=False)
    code: Mapped[str] = mapped_column(String(REPORT_CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(TEMPLATE_NAME_MAX_LENGTH), nullable=False)
    html_template: Mapped[str] = mapped_column(Text, nullable=False)
    css_extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_builtin: Mapped[bool] = mapped_column(
        # Cùng chiều mặc định với `report_datasets.is_builtin` (review 5C, M1):
        # KHÔNG-tin-cậy là mặc định; chỉ seed builtin đặt True tường minh.
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=True
    )
    """Mẫu theo thông tư thuộc một gói cấu hình; NULL = không phụ thuộc chế độ
    kế toán (Phiếu kế toán chung là một ví dụ)."""
