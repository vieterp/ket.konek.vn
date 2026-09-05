"""Chứng từ nghiệp vụ khác sinh / giảm công nợ trên sổ phụ (lát 7C-3).

Quyết định user 2026-09-05: bút toán gõ thẳng vào TK công nợ vẫn cho phép,
nhưng từ nay `general_ledger.journal` là nguồn ghi `ar_ap_ledger` thứ ba. Tệp
này canh chính chỗ phân biệt mà quyết định ấy đòi: **dòng ghi tăng nợ sinh
khoản mới, dòng ghi giảm nợ là một lượt ĐỐI TRỪ** — bù trừ 131 ↔ 331 của cùng
một đối tác không được phình cả hai vế sổ phụ.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from importlib import resources
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import PROVIDERS, SettlementTargetKind
from ket.modules.general_ledger.journal.models import JournalSettlement
from ket.modules.general_ledger.journal.schemas import (
    JournalLineIn,
    JournalSettlementIn,
    JournalVoucherIn,
)
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.modules.general_ledger.journal.settlement_service import (
    SETTLEMENT_ON_INCREASE_CODE,
    SETTLEMENT_ON_NON_DEBT_CODE,
    SETTLEMENT_OVER_REMAINING_CODE,
)
from ket.modules.receivables.ledger_service import SUBLEDGER_SETTLED_CODE
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.contracts import PartnerKind
from ket.posting.integrity.checks.registry import check_of
from ket.posting.integrity.runner import run_check
from ket.posting.settlements import SETTLEMENT_KIND_MISMATCH_CODE
from posting_support import USD_RATE, PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_payment_term

pytestmark = pytest.mark.db

ACTOR_ID = 1
JAN_15 = date(2026, 1, 15)
_FINANCIAL_LEDGER = 0
_MANAGEMENT_LEDGER = 1
DUE_DAYS = 30

PARTNER_ID = 771_001
PAYMENT_TERM_ID = 771_002
PARTNER_WITHOUT_TERM_ID = 771_003

Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def fx_accounts(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    """Gieo 515/635 + purpose chênh lệch tỷ giá vào gói test của module này."""
    return seed_cash_book_package_data(session_factory, dataset_alpha, context)


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


def _ensure_partners(session: Session) -> None:
    """Một đối tác vừa là khách vừa là nhà cung cấp — chính hình dạng mà bù trừ
    131 ↔ 331 đòi (danh mục đối tác gộp, FR-SYS-031)."""
    from ket.kernel.master_data.models.partner import Partner

    ensure_payment_term(session, term_id=PAYMENT_TERM_ID, code="NET30", due_days=DUE_DAYS)
    for partner_id, code, term_id in (
        (PARTNER_ID, "DT7710", PAYMENT_TERM_ID),
        (PARTNER_WITHOUT_TERM_ID, "DT7711", None),
    ):
        existing = session.get(Partner, partner_id)
        if existing is None:
            session.add(
                Partner(
                    id=partner_id,
                    code=code,
                    name=f"Đối tác {code}",
                    path=f"{partner_id}.",
                    is_customer=True,
                    is_vendor=True,
                    payment_term_id=term_id,
                )
            )
        else:
            existing.payment_term_id = term_id
    session.flush()


def _line(
    account_id: int,
    *,
    debit: int = 0,
    credit: int = 0,
    partner_id: int | None = None,
    partner_kind: PartnerKind | None = None,
) -> JournalLineIn:
    return JournalLineIn(
        account_id=account_id,
        debit_fc=Decimal(debit),
        credit_fc=Decimal(credit),
        partner_id=partner_id,
        partner_kind=partner_kind,
    )


def _payload(
    context: PostingContext,
    *,
    lines: tuple[JournalLineIn, ...],
    settlements: tuple[JournalSettlementIn, ...] = (),
) -> JournalVoucherIn:
    return JournalVoucherIn(
        branch_id=context.branch_id,
        document_date=JAN_15,
        posting_date=JAN_15,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="bút toán công nợ ghi tay",
        lines=lines,
        settlements=settlements,
    )


def _receivable_lines(
    context: PostingContext, amount: int, partner_id: int
) -> tuple[JournalLineIn, ...]:
    """Nợ 131 / Có 642 — khoản phải thu ghi tay, bên THUẬN của tính chất."""
    return (
        _line(
            context.accounts["131"],
            debit=amount,
            partner_id=partner_id,
            partner_kind=PartnerKind.CUSTOMER,
        ),
        _line(context.accounts["642"], credit=amount),
    )


def _payable_lines(
    context: PostingContext, amount: int, partner_id: int
) -> tuple[JournalLineIn, ...]:
    """Nợ 642 / Có 331 — khoản phải trả ghi tay."""
    return (
        _line(context.accounts["642"], debit=amount),
        _line(
            context.accounts["331"],
            credit=amount,
            partner_id=partner_id,
            partner_kind=PartnerKind.VENDOR,
        ),
    )


def _entries_of(session: Session, voucher_id: UUID) -> list[ArApLedgerEntry]:
    return list(
        session.execute(select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == voucher_id))
        .scalars()
        .all()
    )


def _post(session: Session, payload: JournalVoucherIn) -> UUID:
    service = JournalVoucherService(session)
    voucher = service.create(payload, user_id=ACTOR_ID)
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


def test_a_line_increasing_debt_writes_one_subledger_row(
    run: Runner, context: PostingContext
) -> None:
    """Dòng Nợ 131 mang đối tác ⇒ một khoản phải thu, hạn rơi từ điều khoản."""

    def work(session: Session) -> object:
        _ensure_partners(session)
        service = JournalVoucherService(session)
        voucher = service.create(
            _payload(context, lines=_receivable_lines(context, 300_000, PARTNER_ID)),
            user_id=ACTOR_ID,
        )
        # Chưa ghi sổ thì chưa có dòng nào: bất biến "dòng sổ phụ chỉ tồn tại
        # khi chứng từ đã ghi sổ" là thứ giữ cho đường xóa an toàn.
        assert _entries_of(session, voucher.id) == []

        service.post(voucher.id, user_id=ACTOR_ID)
        entries = _entries_of(session, voucher.id)
        assert len(entries) == 1
        entry = entries[0]
        assert SettlementTargetKind(entry.target_kind) is SettlementTargetKind.JOURNAL_RECEIVABLE
        assert entry.partner_id == PARTNER_ID
        assert entry.account_id == context.accounts["131"]
        assert entry.amount == Decimal(300_000)
        assert entry.settled == Decimal(0)
        # Số chứng từ GLE đứng thay số hóa đơn: khoản này KHÔNG có hóa đơn gốc.
        assert entry.document_no == voucher.voucher_no
        assert entry.document_id == voucher.id
        assert entry.due_date == JAN_15 + timedelta(days=DUE_DAYS)
        return None

    run(work)


def test_a_partner_without_payment_terms_gets_no_due_date(
    run: Runner, context: PostingContext
) -> None:
    """Không đoán hạn: đối tác chưa khai điều khoản thì để trống."""

    def work(session: Session) -> object:
        _ensure_partners(session)
        voucher_id = _post(
            session,
            _payload(context, lines=_receivable_lines(context, 50_000, PARTNER_WITHOUT_TERM_ID)),
        )
        assert _entries_of(session, voucher_id)[0].due_date is None
        return None

    run(work)


def test_unposting_removes_the_subledger_row(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        _ensure_partners(session)
        service = JournalVoucherService(session)
        voucher = service.create(
            _payload(context, lines=_receivable_lines(context, 120_000, PARTNER_ID)),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        assert len(_entries_of(session, voucher.id)) == 1

        service.unpost(voucher.id, user_id=ACTOR_ID)
        assert _entries_of(session, voucher.id) == []
        return None

    run(work)


def test_a_mutual_offset_settles_both_sides_instead_of_creating_debt(
    run: Runner, context: PostingContext
) -> None:
    """Bù trừ 131 ↔ 331 cùng đối tác = HAI lượt đối trừ, không phải hai khoản mới.

    Đây là ca mà điều kiện #1 của `arap_matches_control.sql` nói tới: không
    phân biệt được nó với "ghi tăng nợ" thì mỗi lượt bù trừ phình cả hai vế sổ
    phụ trong khi sổ cái đứng yên.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        receivable_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 300_000, PARTNER_ID))
        )
        payable_voucher = _post(
            session, _payload(context, lines=_payable_lines(context, 200_000, PARTNER_ID))
        )
        receivable = _entries_of(session, receivable_voucher)[0]
        payable = _entries_of(session, payable_voucher)[0]
        assert SettlementTargetKind(payable.target_kind) is SettlementTargetKind.JOURNAL_PAYABLE

        offset_id = _post(
            session,
            _payload(
                context,
                lines=(
                    _line(
                        context.accounts["331"],
                        debit=200_000,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.VENDOR,
                    ),
                    _line(
                        context.accounts["131"],
                        credit=200_000,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_PAYABLE,
                        target_id=payable.id,
                        amount_fc=Decimal(200_000),
                    ),
                    JournalSettlementIn(
                        line_no=2,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=receivable.id,
                        amount_fc=Decimal(200_000),
                    ),
                ),
            ),
        )

        # Không một khoản nợ mới nào: cả hai dòng đều ở bên NGƯỢC tính chất.
        assert _entries_of(session, offset_id) == []
        session.refresh(receivable)
        session.refresh(payable)
        assert receivable.settled == Decimal(200_000)
        assert receivable.is_closed is False
        assert payable.settled == Decimal(200_000)
        assert payable.is_closed is True
        assert (
            len(
                list(
                    session.execute(
                        select(JournalSettlement).where(JournalSettlement.voucher_id == offset_id)
                    ).scalars()
                )
            )
            == 2
        )
        return None

    run(work)


def test_unposting_the_offset_gives_the_debt_back(run: Runner, context: PostingContext) -> None:
    def work(session: Session) -> object:
        _ensure_partners(session)
        receivable_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 80_000, PARTNER_ID))
        )
        receivable = _entries_of(session, receivable_voucher)[0]
        service = JournalVoucherService(session)
        offset = service.create(
            _payload(
                context,
                lines=(
                    _line(
                        context.accounts["131"],
                        credit=80_000,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], debit=80_000),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=receivable.id,
                        amount_fc=Decimal(80_000),
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(offset.id, user_id=ACTOR_ID)
        session.refresh(receivable)
        assert receivable.is_closed is True

        service.unpost(offset.id, user_id=ACTOR_ID)
        session.refresh(receivable)
        assert receivable.settled == Decimal(0)
        assert receivable.is_closed is False
        return None

    run(work)


def test_a_receipt_screen_sees_a_debt_recorded_by_a_journal_voucher(
    run: Runner, context: PostingContext
) -> None:
    """Khoản nợ ghi tay phải hiện ở màn chọn đối trừ, đúng CHIỀU của nó.

    Hai view provider khóa chiều bằng `target_kind`; để hai loại mới ngoài bộ
    ấy thì phiếu thu/chi không bao giờ nhìn thấy chúng — một khoản nợ không đối
    trừ được là một khoản treo vĩnh viễn trên báo cáo tuổi nợ.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        receivable_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 45_000, PARTNER_ID))
        )
        payable_voucher = _post(
            session, _payload(context, lines=_payable_lines(context, 65_000, PARTNER_ID))
        )
        expected_receivable = _entries_of(session, receivable_voucher)[0].id
        expected_payable = _entries_of(session, payable_voucher)[0].id

        def ids_from(providers: tuple[object, ...], partner_kind: PartnerKind) -> set[UUID]:
            found: set[UUID] = set()
            for provider in providers:
                found.update(
                    invoice.target_id
                    for invoice in provider.open_invoices(  # type: ignore[attr-defined]
                        session,
                        partner_kind=partner_kind,
                        partner_id=PARTNER_ID,
                        branch_id=context.branch_id,
                        as_of=JAN_15,
                    )
                )
            return found

        receivables = ids_from(PROVIDERS.receivable_providers(), PartnerKind.CUSTOMER)
        payables = ids_from(PROVIDERS.payable_providers(), PartnerKind.VENDOR)
        assert expected_receivable in receivables
        assert expected_payable in payables
        # Chiều không rò sang nhau.
        assert expected_payable not in receivables
        assert expected_receivable not in payables
        return None

    run(work)


def test_a_settlement_on_a_line_that_increases_debt_is_refused(
    run: Runner, context: PostingContext
) -> None:
    """Vừa ghi tăng nợ vừa tất toán là vô nghĩa — chặn từ lúc cất."""

    def work(session: Session) -> object:
        _ensure_partners(session)
        receivable_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 10_000, PARTNER_ID))
        )
        target = _entries_of(session, receivable_voucher)[0]
        with pytest.raises(PostingValidationError) as error:
            JournalVoucherService(session).create(
                _payload(
                    context,
                    lines=_receivable_lines(context, 10_000, PARTNER_ID),
                    settlements=(
                        JournalSettlementIn(
                            line_no=1,
                            target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                            target_id=target.id,
                            amount_fc=Decimal(10_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert {violation.code for violation in error.value.violations} == {
            SETTLEMENT_ON_INCREASE_CODE
        }
        return None

    run(work)


def test_a_settlement_on_a_line_that_touches_no_debt_account_is_refused(
    run: Runner, context: PostingContext
) -> None:
    def work(session: Session) -> object:
        _ensure_partners(session)
        receivable_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 10_000, PARTNER_ID))
        )
        target = _entries_of(session, receivable_voucher)[0]
        with pytest.raises(PostingValidationError) as error:
            JournalVoucherService(session).create(
                _payload(
                    context,
                    lines=(
                        _line(context.accounts["642"], debit=10_000),
                        _line(context.accounts["111"], credit=10_000),
                    ),
                    settlements=(
                        JournalSettlementIn(
                            line_no=1,
                            target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                            target_id=target.id,
                            amount_fc=Decimal(10_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert {violation.code for violation in error.value.violations} == {
            SETTLEMENT_ON_NON_DEBT_CODE
        }
        return None

    run(work)


def test_unposting_is_refused_when_the_journal_debt_was_partly_settled(
    run: Runner, context: PostingContext
) -> None:
    """Guard dùng chung của `receivables` canh cả chứng từ GLE, không riêng
    hóa đơn mua/bán — nó đăng ký từ phía CHỦ sổ phụ nên mọi cửa bỏ ghi sổ
    được soi, kể cả cửa của một phân hệ sinh công nợ mà 7A chưa nghĩ tới."""

    def work(session: Session) -> object:
        _ensure_partners(session)
        service = JournalVoucherService(session)
        debt_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 90_000, PARTNER_ID))
        )
        target = _entries_of(session, debt_voucher)[0]
        _post(
            session,
            _payload(
                context,
                lines=(
                    _line(
                        context.accounts["131"],
                        credit=30_000,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], debit=30_000),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=target.id,
                        amount_fc=Decimal(30_000),
                    ),
                ),
            ),
        )
        with pytest.raises(PostingValidationError) as error:
            service.unpost(debt_voucher, user_id=ACTOR_ID)
        assert {violation.code for violation in error.value.violations} == {SUBLEDGER_SETTLED_CODE}
        return None

    run(work)


def test_the_integrity_checks_stay_green_after_a_journal_offset(
    run: Runner, context: PostingContext
) -> None:
    """Nộp nhánh `gl_journal_settlements` cho `settlement_matches_subledger`.

    Check ấy cộng từ một `UNION` các bảng đối trừ, nên một phân hệ mới quên nộp
    nhánh của mình là một dòng đỏ trên dữ liệu ĐÚNG mà không cổng nào khác
    thấy. Kiểm chứng bằng cách **gỡ lại nhánh**: câu không có nó phải đỏ đúng ở
    khoản vừa được đối trừ.

    KHÔNG kiểm `usage_counter_accurate` ở đây, khác lát 7C-2: chứng từ nghiệp
    vụ khác đứng hoàn toàn ngoài hệ bộ đếm tham chiếu — nó không tăng/giảm
    counter nào và `gl_journal_lines` không có mặt trong `UNION` nguồn đếm của
    câu ấy. Một assert ở đó vì thế không mang tín hiệu nào của lát này, mà lại
    khẳng định một tính chất TOÀN dataset (check ấy cố ý không lọc chi nhánh)
    — tệp test khác để lại một bộ đếm lệch là đủ làm nó đỏ.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        debt_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 70_000, PARTNER_ID))
        )
        target = _entries_of(session, debt_voucher)[0]
        _post(
            session,
            _payload(
                context,
                lines=(
                    _line(
                        context.accounts["131"],
                        credit=70_000,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], debit=70_000),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=target.id,
                        amount_fc=Decimal(70_000),
                    ),
                ),
            ),
        )

        check = check_of("settlement_matches_subledger")
        assert run_check(session, check, branch_id=context.branch_id).total == 0

        # Gỡ nhánh ⇒ phải đỏ đúng ở khoản vừa đối trừ.
        without_branch = _drop_journal_branch(check.sql())
        rows = session.execute(text(without_branch), {"branch_id": context.branch_id}).mappings()
        assert any(row["target_id"] == target.id for row in rows)
        return None

    run(work)


def _drop_journal_branch(sql: str) -> str:
    """Cắt nhánh `gl_journal_settlements` ra khỏi câu check — dựng lại đúng
    tình trạng "phân hệ mới quên nộp nhánh của mình"."""
    marker = "    UNION ALL\n    -- Chứng từ nghiệp vụ khác (lát 7C-3)"
    head, _, tail = sql.partition(marker)
    assert tail, "không tìm thấy nhánh gl_journal_settlements trong câu check"
    return head + "\n)," + tail.split("\n),", 1)[1]


def test_the_control_equation_balances_with_journal_debt(
    run: Runner, context: PostingContext
) -> None:
    """Bản thảo `arap_matches_control` không báo lệch nào trên dữ liệu của lát này.

    Câu ấy CHƯA nằm trong `CHECKS` (điều kiện #2 — khoản ứng trước — còn mở,
    xem đầu tệp `.sql`), nên nó được nạp thẳng từ gói thay vì qua `check_of`.
    Bài này là bằng chứng cho điều kiện #1 đã đóng: trước 7C-3, một bút toán
    gõ thẳng vào 131 làm nhích vế sổ cái mà không nhích vế sổ phụ, và chính
    dòng đó sẽ hiện ra ở đây.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        debt_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 500_000, PARTNER_ID))
        )
        target = _entries_of(session, debt_voucher)[0]
        _post(
            session,
            _payload(
                context,
                lines=(
                    _line(
                        context.accounts["131"],
                        credit=120_000,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], debit=120_000),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=target.id,
                        amount_fc=Decimal(120_000),
                    ),
                ),
            ),
        )
        _post(session, _payload(context, lines=_payable_lines(context, 250_000, PARTNER_ID)))

        sql = (
            resources.files("ket.posting.integrity.checks")
            .joinpath("arap_matches_control.sql")
            .read_text("utf-8")
        )
        rows = list(session.execute(text(sql), {"branch_id": context.branch_id}).mappings())

        # Sổ TÀI CHÍNH khớp từng đồng — đó là thứ lát này đóng.
        assert [row for row in rows if row["ledger"] == _FINANCIAL_LEDGER] == []

        # Sổ QUẢN TRỊ thì không, và cố ý: `gl_postings` nhân đôi bút toán sang
        # cả hai sổ (LD-07), còn sổ phụ công nợ chỉ ghi sổ tài chính — cùng
        # luật ở `purchase`, `sales` và lát này. Đẳng thức per-ledger của bản
        # thảo vì thế KHÔNG BAO GIỜ đúng ở sổ 1, với mọi nguồn công nợ chứ
        # không riêng chứng từ GLE. Đây là điều kiện #6 ghi ở đầu tệp `.sql`,
        # và bài test này là chỗ neo nó: ai đăng ký check ở 7C-4 sẽ thấy ngay
        # con số phải xử, thay vì gặp một cổng đỏ không rõ nguồn.
        assert {row["ledger"] for row in rows} == {_MANAGEMENT_LEDGER}
        assert all(row["subledger_net"] == Decimal(0) for row in rows)
        return None

    run(work)


# --------------------------------------------------- chênh lệch tỷ giá (C-1)


def _usd_payload(
    context: PostingContext,
    *,
    lines: tuple[JournalLineIn, ...],
    rate: Decimal,
    settlements: tuple[JournalSettlementIn, ...] = (),
) -> JournalVoucherIn:
    return JournalVoucherIn(
        branch_id=context.branch_id,
        document_date=JAN_15,
        posting_date=JAN_15,
        currency_code="USD",
        exchange_rate=rate,
        description="bút toán công nợ ngoại tệ",
        lines=lines,
        settlements=settlements,
    )


def _postings_of(session: Session, voucher_id: UUID) -> list[tuple[int, Decimal, Decimal]]:
    from ket.posting.engine.models import GlPosting

    rows = session.execute(
        select(GlPosting.account_id, GlPosting.debit, GlPosting.credit)
        .where(GlPosting.voucher_id == voucher_id, GlPosting.ledger == _FINANCIAL_LEDGER)
        .order_by(GlPosting.line_no)
    ).all()
    return [(row.account_id, row.debit, row.credit) for row in rows]


def test_a_foreign_currency_offset_books_the_fx_difference(
    run: Runner, context: PostingContext, fx_accounts: dict[str, int]
) -> None:
    """Chênh lệch tỷ giá của đối trừ GLE phải vào 515/635, không được rơi mất.

    Đây là bản cài thứ năm có đối trừ; bốn bản kia (`cash_book`, `bank`,
    `purchase`, `sales`) đều sinh cặp bù. Thiếu nó thì `settled` cộng theo tỷ
    giá GHI NHẬN còn sổ cái 131 giảm theo tỷ giá CHỨNG TỪ — hai vế lệch đúng
    `Σ fx_diff`, im lặng, và không có đường gõ tay nào vá được (dòng bù gõ tay
    trên 131 bị `classify` đọc thành một khoản phải thu mới).
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        # Khoản phải thu 100 USD ghi ở tỷ giá 25.000 ⇒ 2.500.000 VND.
        debt_voucher = _post(
            session,
            _usd_payload(
                context,
                rate=USD_RATE,
                lines=(
                    _line(
                        context.accounts["131"],
                        debit=100,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], credit=100),
                ),
            ),
        )
        target = _entries_of(session, debt_voucher)[0]
        assert target.amount == Decimal(100) * USD_RATE

        # Bù trừ trọn 100 USD ở tỷ giá 26.000 ⇒ sổ cái giảm 2.600.000, giá trị
        # ghi nhận giải phóng chỉ 2.500.000 ⇒ lãi 100.000 vào 515.
        higher = Decimal(26_000)
        offset_id = _post(
            session,
            _usd_payload(
                context,
                rate=higher,
                lines=(
                    _line(
                        context.accounts["131"],
                        credit=100,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], debit=100),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=target.id,
                        amount_fc=Decimal(100),
                    ),
                ),
            ),
        )

        session.refresh(target)
        assert target.is_closed is True
        # Giá trị ghi nhận đã giải phóng = số ghi ở tỷ giá GHI NHẬN, không phải
        # tỷ giá chứng từ.
        assert target.settled == Decimal(100) * USD_RATE

        postings = _postings_of(session, offset_id)
        gain_account = fx_accounts["515"]
        gain_lines = [row for row in postings if row[0] == gain_account]
        assert len(gain_lines) == 1
        assert gain_lines[0][2] == Decimal(100) * (higher - USD_RATE)

        # Vế 131 trên sổ cái phải RÒNG đúng bằng giá trị đã giải phóng: dòng Có
        # người dùng gõ (2.600.000) cộng dòng Nợ bù (100.000).
        receivable = context.accounts["131"]
        net = sum(
            (debit - credit for account_id, debit, credit in postings if account_id == receivable),
            Decimal(0),
        )
        assert net == -(Decimal(100) * USD_RATE)
        return None

    run(work)


def test_the_control_equation_survives_a_foreign_currency_offset(
    run: Runner, context: PostingContext, fx_accounts: dict[str, int]
) -> None:
    """Bằng chứng chống hồi quy cho C-1: sổ cái và sổ phụ vẫn khớp sau bù trừ
    ngoại tệ. Gỡ cặp bù chênh lệch ra là bài này đỏ."""

    def work(session: Session) -> object:
        _ensure_partners(session)
        debt_voucher = _post(
            session,
            _usd_payload(
                context,
                rate=USD_RATE,
                lines=(
                    _line(
                        context.accounts["131"],
                        debit=200,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], credit=200),
                ),
            ),
        )
        target = _entries_of(session, debt_voucher)[0]
        _post(
            session,
            _usd_payload(
                context,
                rate=Decimal(24_000),
                lines=(
                    _line(
                        context.accounts["131"],
                        credit=200,
                        partner_id=PARTNER_ID,
                        partner_kind=PartnerKind.CUSTOMER,
                    ),
                    _line(context.accounts["642"], debit=200),
                ),
                settlements=(
                    JournalSettlementIn(
                        line_no=1,
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=target.id,
                        amount_fc=Decimal(200),
                    ),
                ),
            ),
        )

        sql = (
            resources.files("ket.posting.integrity.checks")
            .joinpath("arap_matches_control.sql")
            .read_text("utf-8")
        )
        rows = list(session.execute(text(sql), {"branch_id": context.branch_id}).mappings())
        assert [row for row in rows if row["ledger"] == _FINANCIAL_LEDGER] == []
        return None

    run(work)


# ------------------------------------------------------ luật biên đối trừ


def test_two_lines_cannot_together_overpay_one_invoice(
    run: Runner, context: PostingContext
) -> None:
    """Phép kiểm vượt số còn nợ phải GỘP theo đích qua nhiều dòng.

    Kiểm từng dòng là đủ ở ba phân hệ trước (mỗi chứng từ một khối đối trừ);
    chứng từ GLE có nhiều dòng, nên hai dòng 60 vào một hóa đơn 100 đều lọt
    lượt kiểm từng-dòng rồi mới nổ lúc GHI SỔ. Lỗi phải nói ngay lúc cất.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        debt_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 100_000, PARTNER_ID))
        )
        target = _entries_of(session, debt_voucher)[0]
        with pytest.raises(PostingValidationError) as error:
            JournalVoucherService(session).create(
                _payload(
                    context,
                    lines=(
                        _line(
                            context.accounts["131"],
                            credit=60_000,
                            partner_id=PARTNER_ID,
                            partner_kind=PartnerKind.CUSTOMER,
                        ),
                        _line(
                            context.accounts["131"],
                            credit=60_000,
                            partner_id=PARTNER_ID,
                            partner_kind=PartnerKind.CUSTOMER,
                        ),
                        _line(context.accounts["642"], debit=120_000),
                    ),
                    settlements=(
                        JournalSettlementIn(
                            line_no=1,
                            target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                            target_id=target.id,
                            amount_fc=Decimal(60_000),
                        ),
                        JournalSettlementIn(
                            line_no=2,
                            target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                            target_id=target.id,
                            amount_fc=Decimal(60_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert SETTLEMENT_OVER_REMAINING_CODE in {
            violation.code for violation in error.value.violations
        }
        return None

    run(work)


def test_a_target_kind_that_does_not_match_the_target_is_refused(
    run: Runner, context: PostingContext
) -> None:
    """Loại đích client gửi phải khớp loại THẬT của dòng được chọn.

    Một source phục vụ bốn loại trên cùng bảng, nên `find` tra theo id và
    không tự kiểm được. Bỏ qua thì dòng đối trừ lưu sai `target_kind` và cặp
    `(target_kind, target_id)` không nối được với sổ phụ —
    `settlement_matches_subledger` báo hai dòng đỏ trên dữ liệu ĐÚNG.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        debt_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 40_000, PARTNER_ID))
        )
        target = _entries_of(session, debt_voucher)[0]
        with pytest.raises(PostingValidationError) as error:
            JournalVoucherService(session).create(
                _payload(
                    context,
                    lines=(
                        _line(
                            context.accounts["131"],
                            credit=40_000,
                            partner_id=PARTNER_ID,
                            partner_kind=PartnerKind.CUSTOMER,
                        ),
                        _line(context.accounts["642"], debit=40_000),
                    ),
                    settlements=(
                        JournalSettlementIn(
                            line_no=1,
                            # Dòng đích thật là JOURNAL_RECEIVABLE (3).
                            target_kind=SettlementTargetKind.SALES_INVOICE,
                            target_id=target.id,
                            amount_fc=Decimal(40_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert SETTLEMENT_KIND_MISMATCH_CODE in {
            violation.code for violation in error.value.violations
        }
        return None

    run(work)


# ------------------------------------------------- hai dataset báo cáo (H-1)


def _dataset_sql(name: str) -> str:
    return (
        resources.files("ket.kernel.config.reports.data.datasets").joinpath(name).read_text("utf-8")
    )


def test_journal_debt_shows_up_in_aging_and_forecast_on_the_right_side(
    run: Runner, context: PostingContext
) -> None:
    """Khoản nợ ghi tay phải có mặt ở báo cáo tuổi nợ và dự báo dòng tiền, ĐÚNG chiều.

    Hai dataset ấy lọc `target_kind` và ánh xạ chiều bằng `CASE ... WHEN 0
    THEN 'thu' ELSE 'chi'`, nên loại đích mới phải có mặt ở CẢ HAI chỗ: nới
    mỗi `WHERE` thì khoản phải thu ghi tay rơi vào nhánh `ELSE` và hiện ở phía
    phải trả — hỏng nặng hơn bỏ sót nó.
    """

    def work(session: Session) -> object:
        _ensure_partners(session)
        receivable_voucher = _post(
            session, _payload(context, lines=_receivable_lines(context, 310_000, PARTNER_ID))
        )
        payable_voucher = _post(
            session, _payload(context, lines=_payable_lines(context, 220_000, PARTNER_ID))
        )
        receivable_no = _entries_of(session, receivable_voucher)[0].document_no
        payable_no = _entries_of(session, payable_voucher)[0].document_no

        aging_params = {
            "from_date": JAN_15,
            "to_date": JAN_15,
            "ledger": _FINANCIAL_LEDGER,
            "branch_ids": [context.branch_id],
        }
        aging_sql = _dataset_sql("ar_ap_aging.sql")
        receivable_rows = session.execute(
            text(aging_sql), {**aging_params, "direction": "thu"}
        ).mappings()
        payable_rows = session.execute(
            text(aging_sql), {**aging_params, "direction": "chi"}
        ).mappings()
        receivable_nos = {row["invoice_no"] for row in receivable_rows}
        payable_nos = {row["invoice_no"] for row in payable_rows}
        assert receivable_no in receivable_nos
        assert payable_no in payable_nos
        # Không rò chiều — đúng cái bẫy của nhánh `ELSE`.
        assert receivable_no not in payable_nos
        assert payable_no not in receivable_nos

        forecast_rows = list(
            session.execute(
                text(_dataset_sql("cash_forecast.sql")),
                {
                    "from_date": JAN_15,
                    "to_date": date(2026, 12, 31),
                    "ledger": _FINANCIAL_LEDGER,
                    "branch_ids": [context.branch_id],
                },
            ).mappings()
        )
        forecast_nos = {row.get("invoice_no") for row in forecast_rows}
        assert receivable_no in forecast_nos
        assert payable_no in forecast_nos
        return None

    run(work)
