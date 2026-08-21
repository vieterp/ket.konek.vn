"""Định khoản tự động theo nghiệp vụ (FR-SYS-025) — phần **schema**, lát 6A.

Chọn nghiệp vụ trên form phiếu thu/chi → hệ thống điền sẵn cặp Nợ/Có (vẫn sửa
được). Danh sách nghiệp vụ lấy từ `docs/srs/03` §3.1/§3.2 và `docs/srs/04` §3,
và là **dữ liệu trong gói cấu hình** chứ không `if/elif` trong code (phase-06
§Định khoản tự động): TT99 và TT133 gán tài khoản khác nhau cho cùng một
nghiệp vụ, và khác biệt đó phải nằm ở dữ liệu để đổi chế độ kế toán không sửa
code (LD-06, tiêu chí `test_scheme_switch`).

Hai cột `debit_purpose`/`credit_purpose` trỏ vào **`purpose` của
`default_accounts`** thay vì mang thẳng số hiệu TK: một gói đổi "TK phải thu
khách hàng" ở đúng một dòng `default_accounts` thay vì sửa mười mấy dòng nghiệp
vụ, và mọi logic cần TK cụ thể trong hệ thống tiếp tục đi qua đúng một cửa
(FR-SYS-024). Cột `NULL` = nghiệp vụ **cố ý** để ngỏ bên đó cho người dùng chọn
("Thu khác", "Chi khác" — SRS liệt kê cả chục TK khả dĩ, điền sẵn một cái là
đoán bừa); loader từ chối purpose không tồn tại trong gói, nên "để ngỏ" và "gõ
nhầm" không thể lẫn nhau.
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
from ket.kernel.config.accounts_models import (
    DOCUMENT_TYPE_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PURPOSE_MAX_LENGTH,
)
from ket.kernel.contracts import PartnerKind
from ket.kernel.persistence.base import DatasetBase

OPERATION_CODE_MAX_LENGTH = 50


class AutoPostingRule(DatasetBase, Audited):
    """Một nghiệp vụ thu/chi và cặp Nợ/Có ngầm định của nó, trong một gói."""

    __tablename__ = "auto_posting_rules"
    __table_args__ = (
        CheckConstraint("document_type <> ''", name="document_type_not_blank"),
        CheckConstraint("operation_code <> ''", name="operation_code_not_blank"),
        CheckConstraint("operation_name <> ''", name="operation_name_not_blank"),
        CheckConstraint(
            f"partner_kind IS NULL OR partner_kind BETWEEN {PartnerKind.CUSTOMER} "
            f"AND {PartnerKind.EMPLOYEE}",
            name="partner_kind_known",
        ),
        UniqueConstraint(
            "package_id",
            "document_type",
            "operation_code",
            name="uq_auto_posting_rules_package_operation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=False
    )

    document_type: Mapped[str] = mapped_column(String(DOCUMENT_TYPE_MAX_LENGTH), nullable=False)
    """Mã loại chứng từ (`PT`, `PC`, `BC`, `UNC`) — trùng `vouchers.document_type`.
    Không có wildcard như `default_accounts`: danh sách nghiệp vụ của phiếu thu
    và của báo có là hai danh sách nghiệp vụ thật sự khác nhau."""

    operation_code: Mapped[str] = mapped_column(String(OPERATION_CODE_MAX_LENGTH), nullable=False)
    operation_name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)

    debit_purpose: Mapped[str | None] = mapped_column(String(PURPOSE_MAX_LENGTH), nullable=True)
    credit_purpose: Mapped[str | None] = mapped_column(String(PURPOSE_MAX_LENGTH), nullable=True)

    requires_partner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Nghiệp vụ đòi chọn đối tác (thu nợ khách, trả nợ NCC) — form bật ô đối
    tác và picker đối trừ công nợ khi cờ này bật."""

    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """Loại đối tác mà form nên lọc (`kernel.contracts.PartnerKind`); `NULL` =
    không gợi ý loại (nghiệp vụ nhiều đối tượng, đối tác điền theo dòng)."""

    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
