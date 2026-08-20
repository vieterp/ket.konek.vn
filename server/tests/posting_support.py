"""Khung dựng bối cảnh cho test posting engine (phase 4).

Cùng vai với `catalog_api_support.py`: chỉ phần **dựng** — năm tài chính, gói
cấu hình + hệ thống TK tối thiểu, đồng tiền, chi nhánh. Luật nghiệp vụ nằm
trong từng tệp test.

Mỗi lượt gọi `seed_posting_context` dựng một **chi nhánh riêng** (mã duy nhất):
RLS cô lập theo chi nhánh nên hai tệp test dùng chung dataset không thấy chứng
từ của nhau — cách rẻ nhất để không phải dọn bảng phát sinh giữa các test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, ConfigPackage
from ket.kernel.config.accounts_provider import resolve_package
from ket.kernel.currency.models import Currency, ExchangeRate, RateType
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.periods.models import (
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

SEED_ACTOR_ID = 1

FISCAL_YEAR_CODE = "2026"
FISCAL_YEAR_START = date(2026, 1, 1)

PACKAGE_CODE = "TT99-TEST"
"""Mã gói test lịch sử — giữ tên cho scheme mặc định; xem `_package_code`."""


def _package_code(scheme: AccountingScheme) -> str:
    # Mỗi scheme một mã gói: `test_scheme_switch` (bước 20 phase-05) dựng dataset
    # TT133 bằng CÙNG khung seed này, và một gói mang scheme sai sẽ thắng
    # `resolve_package` của dataset đó.
    return PACKAGE_CODE if scheme is AccountingScheme.TT99 else f"{scheme.value}-TEST"


USD_RATE = Decimal("25000")


@dataclass(frozen=True)
class PostingContext:
    """Những `id` mà một test posting cần cầm theo."""

    branch_id: int
    branch_code: str
    fiscal_year_id: int
    package_id: int
    accounts: dict[str, int]
    """Số hiệu TK → id. Bộ tối thiểu: 111, 112, 131 (theo dõi khách), 331
    (theo dõi NCC), 511, 642, và 11 (TK tổng hợp để kiểm BR-SYS-03)."""


def _seed_scope(dataset: DatasetRef, branch_ids: tuple[int, ...] = ()) -> RequestScope:
    return RequestScope(
        dataset_schema=dataset.schema_name, user_id=SEED_ACTOR_ID, branch_ids=branch_ids
    )


def _ensure_currencies(session: Session) -> None:
    if session.get(Currency, "VND") is None:
        session.add(Currency(code="VND", name="Đồng Việt Nam", decimal_places=0, is_base=True))
    if session.get(Currency, "USD") is None:
        session.add(Currency(code="USD", name="Đô la Mỹ", decimal_places=2))
    session.flush()
    rate_key = ("USD", FISCAL_YEAR_START, RateType.ACCOUNTING.value)
    if session.get(ExchangeRate, rate_key) is None:
        session.add(
            ExchangeRate(
                currency_code="USD",
                rate_date=FISCAL_YEAR_START,
                rate_type=RateType.ACCOUNTING.value,
                rate=USD_RATE,
            )
        )
    session.flush()


def _ensure_fiscal_year(session: Session, scheme: AccountingScheme) -> int:
    existing = session.scalar(select(FiscalYear.id).where(FiscalYear.code == FISCAL_YEAR_CODE))
    if existing is not None:
        return existing
    year = PeriodService(session).create_fiscal_year(
        code=FISCAL_YEAR_CODE,
        start_date=FISCAL_YEAR_START,
        accounting_scheme=scheme,
        base_currency="VND",
        inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
        vat_method=VatMethod.DEDUCTION,
    )
    return year.id


_ACCOUNT_SPECS: tuple[tuple[str, str, int, bool, list[str] | None, str | None], ...] = (
    # (code, name, balance_nature, is_summary, detail_tracking, parent_code)
    ("11", "Tiền", BalanceNature.DEBIT, True, None, None),
    ("111", "Tiền mặt", BalanceNature.DEBIT, False, None, "11"),
    ("112", "Tiền gửi ngân hàng", BalanceNature.DEBIT, False, None, "11"),
    ("131", "Phải thu của khách hàng", BalanceNature.DUAL, False, ["customer"], None),
    ("331", "Phải trả cho người bán", BalanceNature.DUAL, False, ["vendor"], None),
    ("511", "Doanh thu bán hàng", BalanceNature.NONE, False, None, None),
    ("642", "Chi phí quản lý doanh nghiệp", BalanceNature.NONE, False, None, None),
)


def _ensure_package_and_accounts(
    session: Session, scheme: AccountingScheme
) -> tuple[int, dict[str, int]]:
    code = _package_code(scheme)
    package_id = session.scalar(select(ConfigPackage.id).where(ConfigPackage.code == code))
    if package_id is None:
        package = ConfigPackage(
            code=code,
            name=f"Gói {scheme.value} cho test",
            scheme=scheme.value,
            # CÙNG ngày hiệu lực với gói TT99 dựng sẵn thật (`TT99-2025`,
            # 2026-01-01 — gieo tự động lúc cấp dữ liệu kế toán): số dư ban đầu
            # ghi đúng ngày đầu năm tài chính, nên gói test phải phủ được ngày
            # đó. `resolve_package` chỉ đọc gói **đã kích hoạt**, xếp theo
            # `effective_from DESC, activated_at DESC, id DESC` — gói này phải
            # `activated_at` **và** kích hoạt sau gói thật (luôn đúng: gói thật
            # kích hoạt lúc `provision_dataset`, gói này tạo sau đó, lúc test
            # chạy) để thắng cả hai tiêu chí, tránh chứng từ test resolve nhầm
            # sang hệ TK thật (185 TK) trong khi `context.accounts` vẫn trỏ ID
            # của gói tối giản này.
            effective_from=FISCAL_YEAR_START,
            activated_at=datetime.now(UTC),
            activated_by=SEED_ACTOR_ID,
        )
        session.add(package)
        session.flush()
        package_id = package.id

    accounts: dict[str, int] = {}
    for code, name, nature, is_summary, tracking, parent_code in _ACCOUNT_SPECS:
        row = session.scalar(
            select(ChartOfAccount).where(
                ChartOfAccount.package_id == package_id, ChartOfAccount.code == code
            )
        )
        if row is None:
            parent_id = accounts.get(parent_code) if parent_code else None
            row = ChartOfAccount(
                package_id=package_id,
                code=code,
                name=name,
                parent_id=parent_id,
                # Cây một-hai cấp cho test: `path` chỉ cần đúng dạng chấm.
                path="0.",
                balance_nature=nature,
                is_summary=is_summary,
                detail_tracking=tracking,
            )
            session.add(row)
            session.flush()
            row.path = f"{row.id}." if parent_id is None else f"{parent_id}.{row.id}."
            session.flush()
        accounts[code] = row.id
    return package_id, accounts


def seed_posting_context(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    *,
    scheme: AccountingScheme = AccountingScheme.TT99,
) -> PostingContext:
    """Dựng đủ bối cảnh ghi sổ trong dataset đã cho, với một chi nhánh mới.

    `scheme` phải khớp scheme của dataset (bước 20 phase-05 dùng TT133): năm
    tài chính và gói test cùng mang scheme đó thì `resolve_package` mới trỏ về
    đúng bộ TK tối giản mà `context.accounts` cầm id.
    """
    branch_code = f"PB{uuid4().hex[:6].upper()}"
    with unit_of_work(session_factory, _seed_scope(dataset)) as session:
        branch = BranchService(session).create(code=branch_code, name=f"Chi nhánh {branch_code}")
        branch_id = branch.id
        _ensure_currencies(session)
        fiscal_year_id = _ensure_fiscal_year(session, scheme)
        package_id, accounts = _ensure_package_and_accounts(session, scheme)
    return PostingContext(
        branch_id=branch_id,
        branch_code=branch_code,
        fiscal_year_id=fiscal_year_id,
        package_id=package_id,
        accounts=accounts,
    )


def seed_posting_context_on_builtin(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    *,
    scheme: AccountingScheme,
    account_codes: tuple[str, ...],
) -> PostingContext:
    """Bối cảnh ghi sổ dùng **gói builtin** của scheme thay vì gói test tối giản.

    Cho `test_scheme_switch` (bước 20 phase-05): bài kiểm phải chứng minh hệ TK
    + layout BCTC + bộ sổ ĐÓNG GÓI của từng thông tư phục vụ đúng, nên không
    được che builtin bằng một gói test kích hoạt sau. Không tạo gói nào ở đây —
    `resolve_package` trỏ về chính gói gieo lúc provision.
    """
    branch_code = f"PB{uuid4().hex[:6].upper()}"
    with unit_of_work(session_factory, _seed_scope(dataset)) as session:
        branch = BranchService(session).create(code=branch_code, name=f"Chi nhánh {branch_code}")
        branch_id = branch.id
        _ensure_currencies(session)
        fiscal_year_id = _ensure_fiscal_year(session, scheme)
        package = resolve_package(session, scheme=scheme.value, on_date=FISCAL_YEAR_START)
        rows = session.execute(
            select(ChartOfAccount.code, ChartOfAccount.id).where(
                ChartOfAccount.package_id == package.id,
                ChartOfAccount.code.in_(account_codes),
            )
        ).all()
        accounts = dict(rows)
        missing = set(account_codes) - set(accounts)
        assert not missing, f"gói builtin {package.code} thiếu TK: {sorted(missing)}"
        package_id = package.id
    return PostingContext(
        branch_id=branch_id,
        branch_code=branch_code,
        fiscal_year_id=fiscal_year_id,
        package_id=package_id,
        accounts=accounts,
    )


def posting_scope(dataset: DatasetRef, context: PostingContext, *, user_id: int) -> RequestScope:
    """Scope của một người dùng thao tác trong đúng chi nhánh test."""
    return RequestScope(
        dataset_schema=dataset.schema_name,
        user_id=user_id,
        branch_ids=(context.branch_id,),
        acting_branch_id=context.branch_id,
    )
