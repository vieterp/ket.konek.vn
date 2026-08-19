"""Gói cấu hình pháp lý và hệ thống tài khoản — phần **schema** (LD-06, RT-07).

Hai bảng này thuộc phạm vi phase 5 (gói TT200/TT133 đầy đủ: nhập từ CSV, kích
hoạt, ký số, layout BCTC), nhưng **hình dạng bảng** phải có từ phase 4: mọi dòng
`gl_postings.account_id` trỏ vào `chart_of_accounts`, và một khóa ngoại không
thêm sau được lên bảng phát sinh triệu dòng mà không khóa bảng. Hình dạng lấy
nguyên văn từ phase-05 §Gói cấu hình — phase 5 chỉ **đổ dữ liệu và dựng máy
móc quanh nó**, không đổi cột.

Tài khoản thuộc **gói** chứ không đứng một mình: TT200 và TT133 là hai hệ thống
tài khoản khác nhau cùng tồn tại (LD-06), và một dòng phát sinh năm 2026 phải
trỏ mãi vào tài khoản của gói đã dùng năm 2026 — kể cả khi doanh nghiệp đổi chế
độ vào 2027. Đó là lý do khóa duy nhất là `(package_id, code)` chứ không `code`.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.periods.models import AccountingScheme
from ket.kernel.persistence.base import DatasetBase

ACCOUNT_CODE_MAX_LENGTH = 20
"""Số hiệu tài khoản (`111`, `33311`, tài khoản chi tiết người dùng thêm)."""

NAME_MAX_LENGTH = 255

_SCHEME_SQL_LIST = ", ".join(f"'{member.value}'" for member in AccountingScheme)


class BalanceNature:
    """Tính chất số dư của tài khoản (phase-05 §chart_of_accounts).

    Hằng số nguyên chứ không `IntEnum` đầy đủ: giá trị đi thẳng vào cột
    SMALLINT và vào SQL của gói cấu hình, nên thứ cần là **tên cho từng số**,
    không phải một kiểu bao ngoài.
    """

    DEBIT = 0
    """Dư Nợ (tài sản, chi phí)."""
    CREDIT = 1
    """Dư Có (nguồn vốn, doanh thu)."""
    DUAL = 2
    """Lưỡng tính (131, 331 — dư cả hai bên theo đối tượng)."""
    NONE = 3
    """Không có số dư (loại 5–9 kết chuyển hết cuối kỳ)."""


class DetailTracking:
    """Giá trị hợp lệ của `chart_of_accounts.detail_tracking` (FR-SYS-021).

    Mỗi giá trị trỏ vào một chiều trên dòng hạch toán; validator ghi sổ
    (`posting/engine/validators/dimension_required.py`) đọc mảng này và bắt
    buộc dòng điền đủ chiều đã bật.
    """

    CUSTOMER = "customer"
    VENDOR = "vendor"
    EMPLOYEE = "employee"
    COST_OBJECT = "cost_object"
    PROJECT = "project"
    ORDER = "order"
    CONTRACT = "contract"
    EXPENSE_ITEM = "expense_item"
    ITEM = "item"
    WAREHOUSE = "warehouse"

    ALL = frozenset(
        {
            CUSTOMER,
            VENDOR,
            EMPLOYEE,
            COST_OBJECT,
            PROJECT,
            ORDER,
            CONTRACT,
            EXPENSE_ITEM,
            ITEM,
            WAREHOUSE,
        }
    )


class ConfigPackage(DatasetBase, Audited):
    """Một gói cấu hình pháp lý có hiệu lực theo ngày (LD-06, FR-NFR-055).

    Phase 4 chỉ cần ba câu hỏi: gói nào, chế độ nào, hiệu lực khi nào — đủ để
    `resolve_package` chọn hệ thống tài khoản cho một ngày hạch toán. Ký số,
    verify checksum, kích hoạt có kiểm soát (RT-07) là việc của phase 5.
    """

    __tablename__ = "config_packages"
    __table_args__ = (
        CheckConstraint(f"scheme IN ({_SCHEME_SQL_LIST})", name="scheme_known"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="effective_range_ordered",
        ),
        Index("ix_config_packages_scheme_effective", "scheme", "effective_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    """`TT200-2014`, `TT133-2016` — mã mà gói ký số của phase 5 sẽ ghim."""

    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    scheme: Mapped[str] = mapped_column(String(10), nullable=False)
    legal_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """`user_id` trần như mọi tham chiếu tới `public.users` — xem `persistence/base.py`."""


class ChartOfAccount(DatasetBase, Audited):
    """Một tài khoản trong hệ thống tài khoản của một gói (FR-SYS-020).

    Không dùng `MasterDataMixin` dù là danh mục dạng cây: hệ thống tài khoản
    thuộc **gói cấu hình** (khóa duy nhất theo gói, không có bản riêng chi
    nhánh, không có `uid` đồng bộ — số hiệu tài khoản đã là định danh toàn quốc
    theo thông tư). Phần cây (path/level) dùng chung luật với danh mục.
    """

    __tablename__ = "chart_of_accounts"
    __table_args__ = (
        CheckConstraint("code <> ''", name="code_not_blank"),
        CheckConstraint(f"path ~ '{PATH_PATTERN}'", name="path_is_dotted_ids"),
        CheckConstraint(f"level >= {ROOT_LEVEL}", name="level_at_least_root"),
        CheckConstraint("balance_nature BETWEEN 0 AND 3", name="balance_nature_known"),
        UniqueConstraint("package_id", "code", name="uq_chart_of_accounts_package_code"),
        Index("ix_chart_of_accounts_parent_id", "parent_id"),
        Index("ix_chart_of_accounts_path", "package_id", "path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=False
    )

    code: Mapped[str] = mapped_column(String(ACCOUNT_CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)

    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=ROOT_LEVEL, server_default=str(ROOT_LEVEL)
    )

    balance_nature: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    is_summary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """TK tổng hợp — có tài khoản con, **không** hạch toán trực tiếp (BR-SYS-03)."""

    is_foreign_currency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    detail_tracking: Mapped[list[str] | None] = mapped_column(ARRAY(String(20)), nullable=True)
    """Chiều bắt buộc khi hạch toán vào TK này (FR-SYS-021) — giá trị thuộc
    `DetailTracking.ALL`, kiểm ở tầng dịch vụ vì `CHECK` trên mảng không nói
    được "mọi phần tử thuộc danh sách" một cách đọc nổi."""

    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """TK cấp 1 theo thông tư — người dùng không sửa/xóa được."""

    is_inactive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
