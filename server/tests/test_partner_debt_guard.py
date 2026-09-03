"""Guard ngưỡng nợ đối tác (FR-SYS-032) trên PostgreSQL thật.

Guard sống ở `receivables` (chủ sổ phụ) nhưng canh **mọi** loại chứng từ; ở đây
kích nó bằng hóa đơn mua — chứng từ đầu tiên của hệ làm tăng nợ phải trả.

Ba mức đọc từ `warning.partner_debt`, cùng hợp đồng với guard số dư quỹ:
`none` bỏ qua, `warn` trả vi phạm mang `GUARD_WARNING_DETAIL` và cho đi tiếp
khi người dùng xác nhận, `block` chặn bất kể xác nhận. Hai luật con:

* Vượt hạn mức — nợ mở hiện tại + phần tăng của chứng từ > `credit_limit`.
* Nợ quá hạn — còn khoản mở có hạn trả (hoặc ngày chứng từ + `due_days` của
  điều khoản) trước ngày ghi sổ.

Nợ mang sang từ hệ thống cũ (chứng từ công nợ đầu kỳ còn treo) là nợ thật: cả
hai luật đều đếm nó, không riêng dòng `ar_ap_ledger` do hệ này sinh ra.

Hạn mức là của đối tác trước TOÀN công ty: nợ ở chi nhánh khác — nơi người ghi
sổ không đọc được `ar_ap_ledger` vì RLS — vẫn cộng vào số so với hạn mức.

Chứng từ làm GIẢM nợ (trả lại hàng) không bao giờ kích guard: chặn đường trả
nợ vì "đang nợ nhiều" là chặn đúng thứ giúp thoát ngưỡng.

Mỗi bài kết bằng `_RollbackError` để tùy chọn hệ thống và khoản nợ dựng trong
bài không dính sang bài khác trên dataset dùng chung.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_open_invoice
from ket.kernel.config.catalog import PARTNER_DEBT_WARNING_KEY
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.models import Branch, Setting
from ket.modules.purchase.models import (
    LandedCostAllocation,
    PurchaseInvoiceKind,
    VendorInvoiceStatus,
)
from ket.modules.purchase.schemas import (
    PurchaseInvoiceIn,
    PurchaseInvoiceLineIn,
    PurchaseSettlementIn,
)
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.receivables.guards import CREDIT_LIMIT_EXCEEDED_CODE, OVERDUE_DEBT_CODE
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.contracts import GUARD_WARNING_DETAIL, VoucherStatus
from ket.posting.opening_balances.models import OpeningDetailKind
from posting_support import (
    PostingContext,
    ensure_second_branch,
    posting_scope,
    seed_posting_context,
)
from purchase_support import ensure_payment_term, ensure_vendor, seed_purchase_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_10 = date(2026, 1, 10)
FEB_20 = date(2026, 2, 20)

LIMITED_VENDOR_ID = 9201
TERM_VENDOR_ID = 9202
NET_15_TERM_ID = 9203
MULTI_BRANCH_VENDOR_ID = 9204
"""NCC riêng cho bài hai chi nhánh — nợ nền của nó được COMMIT (guard ở chi
nhánh kia phải thấy qua transaction khác), nên không dùng chung với bài nào."""
OPENING_VENDOR_ID = 9205
"""NCC riêng cho bài công nợ đầu kỳ — số dư đầu kỳ gieo qua transaction riêng
(COMMIT) và ở lại dataset, nên nó không được lẫn vào nợ của bài khác."""


class _RollbackError(Exception):
    """Chặn commit — tùy chọn hệ thống và nợ dựng trong test không dính sang test khác."""


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    accounts = seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_vendor(
            session,
            partner_id=LIMITED_VENDOR_ID,
            code="NCC-7B-HM",
            credit_limit=Decimal(1_500_000),
        )
        ensure_payment_term(session, term_id=NET_15_TERM_ID, code="NET15-7B", due_days=15)
        ensure_vendor(
            session,
            partner_id=TERM_VENDOR_ID,
            code="NCC-7B-DK",
            payment_term_id=NET_15_TERM_ID,
        )
        ensure_vendor(
            session,
            partner_id=MULTI_BRANCH_VENDOR_ID,
            code="NCC-7B-2CN",
            credit_limit=Decimal(1_500_000),
        )
        ensure_vendor(
            session,
            partner_id=OPENING_VENDOR_ID,
            code="NCC-7B-DK-SDDK",
            credit_limit=Decimal(1_000_000),
        )
    return accounts


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _set_system_setting(session: Session, key: str, value: str, value_type: str) -> None:
    """Upsert thẳng dòng `settings` — cùng lối `test_cash_balance_guard`."""
    row = session.scalar(select(Setting).where(Setting.key == key, Setting.scope == "system"))
    if row is None:
        session.add(Setting(scope="system", key=key, value=value, value_type=value_type))
    else:
        row.value = value
    session.flush()


def _invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    vendor_id: int,
    amount: int,
    posting_date: date = JAN_10,
    due_date: date | None = None,
    kind: int = PurchaseInvoiceKind.GOODS,
    settlements: tuple[PurchaseSettlementIn, ...] = (),
    branch_id: int | None = None,
) -> PurchaseInvoiceIn:
    return PurchaseInvoiceIn(
        kind=kind,
        operation_code="tra-lai-hang-mua" if kind == PurchaseInvoiceKind.RETURN else "mua-hang-hoa",
        vendor_id=vendor_id,
        payable_account_id=accounts["331"],
        branch_id=context.branch_id if branch_id is None else branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        vendor_invoice_status=VendorInvoiceStatus.NOT_YET,
        due_date=due_date,
        landed_cost_allocation=LandedCostAllocation.BY_VALUE,
        lines=(
            PurchaseInvoiceLineIn(
                description="Hàng",
                amount_fc=Decimal(amount),
                account_id=accounts["156"],
            ),
        ),
        settlements=settlements,
    )


def _post_debt(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    vendor_id: int,
    amount: int,
) -> ArApLedgerEntry:
    """Ghi sổ một hóa đơn dựng nợ nền (guard đang ở mức `none`) và trả dòng sổ phụ."""
    service = PurchaseInvoiceService(session)
    voucher = service.create(
        _invoice(context, accounts, vendor_id=vendor_id, amount=amount), user_id=ACTOR_ID
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return session.execute(
        select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == voucher.id)
    ).scalar_one()


def test_none_level_lets_debt_pass_the_limit(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        _post_debt(session, context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=1_000_000)
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _invoice(context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=1_000_000),
            user_id=ACTOR_ID,
        )
        posted = service.post(voucher.id, user_id=ACTOR_ID)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_warn_level_flags_credit_limit_then_posts_on_acknowledgement(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        _post_debt(session, context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=1_000_000)
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "warn", "string")

        service = PurchaseInvoiceService(session)
        # 1 000 000 đang nợ + 400 000 = 1 400 000 ≤ hạn mức 1 500 000: qua.
        within = service.create(
            _invoice(context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=400_000),
            user_id=ACTOR_ID,
        )
        service.post(within.id, user_id=ACTOR_ID)

        # 1 400 000 + 200 000 = 1 600 000 > hạn mức: cảnh báo, xác nhận thì qua.
        over = service.create(
            _invoice(context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=200_000),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(over.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == CREDIT_LIMIT_EXCEEDED_CODE
        assert violation.details[GUARD_WARNING_DETAIL] == 1
        assert Decimal(violation.details["projected"]) == Decimal(1_600_000)
        assert Decimal(violation.details["credit_limit"]) == Decimal(1_500_000)

        posted = service.post(over.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_block_level_ignores_acknowledgement(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "block", "string")
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _invoice(context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=2_000_000),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID, acknowledged_warnings=True)
        (violation,) = caught.value.violations
        assert violation.code == CREDIT_LIMIT_EXCEEDED_CODE
        assert GUARD_WARNING_DETAIL not in violation.details
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_overdue_debt_uses_due_date_or_payment_term(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> object:
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        # Không có `due_date` trên hóa đơn: hạn = 10/01 + 15 ngày = 25/01.
        _post_debt(session, context, accounts, vendor_id=TERM_VENDOR_ID, amount=500_000)
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "warn", "string")

        service = PurchaseInvoiceService(session)
        # Ghi sổ ngày 20/02: khoản trên đã quá hạn.
        late = service.create(
            _invoice(
                context, accounts, vendor_id=TERM_VENDOR_ID, amount=100_000, posting_date=FEB_20
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(late.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == OVERDUE_DEBT_CODE
        assert violation.details["overdue_count"] == 1
        assert violation.details["oldest_due_date"] == "2026-01-25"
        assert violation.details["due_days"] == 15

        # NCC KHÔNG có điều khoản nhưng khoản nợ ghi hạn tường minh đã qua: vẫn
        # quá hạn — hạn trên khoản là sự thật, điều khoản chỉ là cách suy hạn
        # cho khoản không ghi.
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        dated = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=LIMITED_VENDOR_ID,
                amount=100_000,
                due_date=date(2026, 1, 31),
            ),
            user_id=ACTOR_ID,
        )
        service.post(dated.id, user_id=ACTOR_ID)
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "warn", "string")
        after = service.create(
            _invoice(
                context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=100_000, posting_date=FEB_20
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(after.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == OVERDUE_DEBT_CODE
        assert violation.details["oldest_due_date"] == "2026-01-31"
        assert violation.details["due_days"] is None
        service.post(after.id, user_id=ACTOR_ID, acknowledged_warnings=True)

        # Hóa đơn có `due_date` tường minh xa hơn hạn của điều khoản thì không quá hạn.
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        far = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=LIMITED_VENDOR_ID,
                amount=100_000,
                due_date=date(2026, 12, 31),
            ),
            user_id=ACTOR_ID,
        )
        service.post(far.id, user_id=ACTOR_ID)
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "warn", "string")
        # Khoản 31/01 đã có chứng từ `after` cộng thêm rồi vẫn còn mở — để bài
        # này im lặng, đối trừ nó bằng chứng từ trả lại trước.
        (dated_debt,) = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == dated.id)
        ).scalars()
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        cleared = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=LIMITED_VENDOR_ID,
                amount=100_000,
                posting_date=FEB_20,
                kind=PurchaseInvoiceKind.RETURN,
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=dated_debt.id,
                        amount_fc=Decimal(100_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(cleared.id, user_id=ACTOR_ID)
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "warn", "string")
        # NCC này không có điều khoản, các khoản còn mở đều chưa tới hạn hoặc
        # không có hạn, tổng nợ < hạn mức 1 500 000 → im lặng.
        quiet = service.create(
            _invoice(
                context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=100_000, posting_date=FEB_20
            ),
            user_id=ACTOR_ID,
        )
        posted = service.post(quiet.id, user_id=ACTOR_ID)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_debt_decrease_never_triggers_the_guard(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Trả lại hàng cho NCC đang vượt hạn mức phải ghi sổ được — nó GIẢM nợ."""

    def work(session: Session) -> object:
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "none", "string")
        debt = _post_debt(session, context, accounts, vendor_id=LIMITED_VENDOR_ID, amount=2_000_000)
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "block", "string")

        service = PurchaseInvoiceService(session)
        returned = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=LIMITED_VENDOR_ID,
                amount=300_000,
                kind=PurchaseInvoiceKind.RETURN,
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=debt.id,
                        amount_fc=Decimal(300_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        posted = service.post(returned.id, user_id=ACTOR_ID)
        assert VoucherStatus(posted.status) is VoucherStatus.DA_GHI_SO
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)


def test_credit_limit_counts_debt_of_every_branch(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Nợ 1 000 000 ở chi nhánh A + hóa đơn 600 000 ghi ở chi nhánh B — người
    của B không đọc được dòng sổ phụ của A (RLS), guard vẫn phải cộng nó vào.
    Nợ nền được COMMIT vì guard ở B đọc trong transaction khác."""
    run(
        lambda session: _post_debt(
            session, context, accounts, vendor_id=MULTI_BRANCH_VENDOR_ID, amount=1_000_000
        )
    )

    ensure_second_branch(session_factory, dataset_alpha)
    other_branch_id = run(
        lambda session: session.execute(
            select(Branch.id).where(Branch.id != context.branch_id).order_by(Branch.id).limit(1)
        ).scalar_one()
    )
    assert isinstance(other_branch_id, int)
    other_branch_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(other_branch_id,),
        acting_branch_id=other_branch_id,
    )

    with (
        pytest.raises(_RollbackError),
        unit_of_work(session_factory, other_branch_scope) as session,
    ):
        visible = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.partner_id == MULTI_BRANCH_VENDOR_ID)
        ).all()
        assert visible == []

        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "warn", "string")
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=MULTI_BRANCH_VENDOR_ID,
                amount=600_000,
                branch_id=other_branch_id,
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == CREDIT_LIMIT_EXCEEDED_CODE
        assert Decimal(violation.details["projected"]) == Decimal(1_600_000)
        raise _RollbackError


def test_opening_balance_debt_counts_like_any_other_debt(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Công nợ đầu kỳ còn treo cộng vào cả hạn mức lẫn nợ quá hạn.

    Nợ mang sang từ hệ thống cũ nằm ở `opening_balance_invoices`, không ở
    `ar_ap_ledger`: chỉ đếm sổ phụ thì một khách vừa cấp dữ liệu luôn còn
    nguyên hạn mức và khoản quá hạn từ đầu kỳ không bao giờ kêu.
    """
    seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=OPENING_VENDOR_ID,
        amount_fc=Decimal(900_000),
        invoice_no="HD-DK-7B",
        invoice_date=date(2025, 12, 15),
        due_date=date(2025, 12, 31),
    )

    def work(session: Session) -> object:
        _set_system_setting(session, PARTNER_DEBT_WARNING_KEY, "block", "string")
        service = PurchaseInvoiceService(session)
        voucher = service.create(
            _invoice(context, accounts, vendor_id=OPENING_VENDOR_ID, amount=200_000),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(voucher.id, user_id=ACTOR_ID)
        by_code = {violation.code: violation for violation in caught.value.violations}
        assert set(by_code) == {CREDIT_LIMIT_EXCEEDED_CODE, OVERDUE_DEBT_CODE}
        # 900 000 đầu kỳ + 200 000 của chứng từ > hạn mức 1 000 000.
        assert Decimal(by_code[CREDIT_LIMIT_EXCEEDED_CODE].details["projected"]) == Decimal(
            1_100_000
        )
        overdue = by_code[OVERDUE_DEBT_CODE].details
        assert overdue["oldest_document_no"] == "HD-DK-7B"
        assert overdue["oldest_due_date"] == "2025-12-31"
        raise _RollbackError

    with pytest.raises(_RollbackError):
        run(work)
