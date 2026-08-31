"""Gói cấu hình pháp lý và hệ thống tài khoản — phần **schema** (LD-06, RT-07).

Hai bảng này thuộc phạm vi phase 5 (gói TT99/TT133 đầy đủ: nhập từ CSV, kích
hoạt, ký số, layout BCTC), nhưng **hình dạng bảng** phải có từ phase 4: mọi dòng
`gl_postings.account_id` trỏ vào `chart_of_accounts`, và một khóa ngoại không
thêm sau được lên bảng phát sinh triệu dòng mà không khóa bảng. Hình dạng lấy
nguyên văn từ phase-05 §Gói cấu hình — phase 5 chỉ **đổ dữ liệu và dựng máy
móc quanh nó**, không đổi cột.

Tài khoản thuộc **gói** chứ không đứng một mình: TT99 và TT133 là hai hệ thống
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


CASH_ON_HAND_CODE_PREFIX = "111"
"""Nhóm TK tiền mặt tại quỹ — cặp đôi của hằng ngay dưới."""

DEPOSIT_ACCOUNT_CODE_PREFIX = "112"
"""Nhóm TK tiền gửi ngân hàng.

Literal số hiệu CÓ CHỦ ĐÍCH: nhóm 112 do chính SRS định nghĩa và là bất biến
chung của TT99 lẫn TT133, không phải đích cấu hình. Đặt ở kernel vì cả ba tầng
cần nó — loader gói, mapper của hai module, và đường chuyển số dư của `posting`
— mà chúng không import được lẫn nhau (luật phụ thuộc C3/C4).
"""


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
    BANK_ACCOUNT = "bank_account"
    """Tài khoản ngân hàng doanh nghiệp của dòng 112x.

    Bật trên 112x thì mọi đường ghi sổ — chứng từ tiền gửi, phiếu thu/chi nộp
    rút tiền mặt, bút toán tổng hợp gõ thẳng — đều phải nói dòng này thuộc tài
    khoản ngân hàng nào. Trước đó chủ sở hữu được **suy** từ thân
    `bank_vouchers`, nên hai đường sau không suy được và sổ chi tiết tiền gửi
    thiếu đúng bằng chúng.
    """

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
            BANK_ACCOUNT,
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
    """`TT99-2025`, `TT133-2016` — mã mà gói ký số của phase 5 sẽ ghim."""

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


DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE = "*"
"""`document_type` đại diện cho "mọi loại chứng từ" trong `default_accounts`.

Một gói cấu hình khai `purpose='vat_output'` một lần cho `'*'` thay vì lặp lại
cho từng loại chứng từ (phiếu bán hàng, hóa đơn dịch vụ…) — logic nghiệp vụ tra
theo `(document_type cụ thể, purpose)` trước, rồi lùi về `('*', purpose)` khi
không có dòng riêng (`accounts_provider.default_account`)."""

DOCUMENT_TYPE_MAX_LENGTH = 20
PURPOSE_MAX_LENGTH = 50


class DefaultAccount(DatasetBase, Audited):
    """Tài khoản ngầm định theo mục đích nghiệp vụ (FR-SYS-024, ngoại lệ có kiểm soát).

    Đây là lối thoát duy nhất cho logic nghiệp vụ cần một TK **cụ thể** (khấu
    trừ thuế: Nợ 33311 / Có 133 — FR-TAX-004) mà không hard-code số hiệu: khai
    `purpose` mang nghĩa (`vat_output`, `vat_input`), gói cấu hình gán số hiệu.
    `account_code` là **số hiệu**, không phải `id`: gói cấu hình ghi tệp CSV
    trước khi biết `id` mà `ChartOfAccount` sẽ được cấp lúc nhập.
    """

    __tablename__ = "default_accounts"
    __table_args__ = (
        CheckConstraint("document_type <> ''", name="document_type_not_blank"),
        CheckConstraint("purpose <> ''", name="purpose_not_blank"),
        CheckConstraint("account_code <> ''", name="account_code_not_blank"),
    )

    package_id: Mapped[int] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), primary_key=True
    )
    document_type: Mapped[str] = mapped_column(String(DOCUMENT_TYPE_MAX_LENGTH), primary_key=True)
    """Mã loại chứng từ cụ thể, hoặc `DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE`."""
    purpose: Mapped[str] = mapped_column(String(PURPOSE_MAX_LENGTH), primary_key=True)
    account_code: Mapped[str] = mapped_column(String(ACCOUNT_CODE_MAX_LENGTH), nullable=False)


class ClosingAccountPair(DatasetBase, Audited):
    """Một cặp tài khoản kết chuyển cuối kỳ, theo thứ tự (FR-SYS-023, FR-GLE-022).

    `sequence` quyết định thứ tự chạy: kết chuyển giá vốn/doanh thu phải xong
    trước khi kết chuyển 911 sang 421, nếu không bút toán kết chuyển sau đọc
    một số dư 911 chưa đầy đủ. Không suy thứ tự từ số hiệu TK — dãy kết chuyển
    của mỗi chế độ kế toán là một quyết định nghiệp vụ, không phải một quy luật
    số học.
    """

    __tablename__ = "closing_account_pairs"
    __table_args__ = (
        CheckConstraint("source_account <> ''", name="source_account_not_blank"),
        CheckConstraint("target_account <> ''", name="target_account_not_blank"),
        UniqueConstraint(
            "package_id",
            "source_account",
            "target_account",
            name="uq_closing_account_pairs_package_pair",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("config_packages.id", ondelete="RESTRICT"), nullable=False
    )
    source_account: Mapped[str] = mapped_column(String(ACCOUNT_CODE_MAX_LENGTH), nullable=False)
    target_account: Mapped[str] = mapped_column(String(ACCOUNT_CODE_MAX_LENGTH), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
