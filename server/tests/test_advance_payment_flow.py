"""Khoản ứng trước thành dòng sổ phụ chiều ngược, và check 131/331 (lát 7C-4).

Quyết định user 2026-09-06: đóng hai điều kiện cuối của
`arap_matches_control.sql` rồi **đăng ký** nó vào `CHECKS`.

* **#2 — khoản ứng trước.** Dòng ở bên NGƯỢC tính chất công nợ mà không trỏ
  đích đối trừ là khoản khách ứng trước / ta trả trước người bán. Trước lát này
  nó nhích vế sổ cái mà không nhích vế sổ phụ, ở **cả ba** phân hệ sinh chuyển
  động công nợ bằng tiền hoặc bằng tay. Nay nó là một dòng `ar_ap_ledger` mang
  `ADVANCE_FROM_CUSTOMER`/`ADVANCE_TO_VENDOR` — chiều NGƯỢC với chiều nợ của
  chính đối tác ấy.
* **Đường bù** khoản ứng trước với hóa đơn phát sinh sau là một chứng từ nghiệp
  vụ khác: dòng bên thuận trỏ vào khoản ứng trước, dòng bên ngược trỏ vào hóa
  đơn. Sổ cái không đổi (cả hai bút toán đã ghi từ trước), sổ phụ giảm hai đầu.

Tệp này cũng canh một lỗ đã shipping từ 7C-3: `receivables` cấp
`SettlementTargetSource` cho hai loại đích ghi tay, nhưng trần `target_kind`
của bốn bảng đối trừ phía chứng từ còn dừng ở `OPENING_BALANCE` — nên thu tiền
một khoản phải thu ghi tay nổ CHECK ở DB thay vì chạy.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from importlib import resources
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.bank.models import BankVoucherKind
from ket.modules.bank.schemas import BankVoucherIn, BankVoucherLineIn
from ket.modules.bank.service import BankVoucherService
from ket.modules.cash_book.models import CashSettlement, CashVoucherKind
from ket.modules.cash_book.schemas import CashSettlementIn, CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.general_ledger.journal.schemas import (
    JournalLineIn,
    JournalSettlementIn,
    JournalVoucherIn,
)
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.contracts import PartnerKind
from ket.posting.settlements import SETTLEMENT_DIRECTION_MISMATCH_CODE
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
FEB_10 = date(2026, 2, 10)
FINANCIAL_LEDGER = 0

PARTNER_ID = 774_001
"""Riêng của tệp này. Mọi khẳng định dưới đây lọc theo đúng đối tác này: câu
check đo theo CHI NHÁNH, và dataset dùng chung cả phiên — khẳng định trên toàn
chi nhánh sẽ đỏ vì dư lượng của tệp khác, đúng cái bẫy đã bắt bốn lát liền."""

REFUND_OPERATION = "chi-tra-lai-khach-hang"
"""Phiếu chi cho KHÁCH HÀNG: bên Nợ 131 là bên THUẬN của phải thu, nên nó tất
toán khoản ứng trước chứ không tất toán nợ. Hai nghiệp vụ quỹ có sẵn ở
`cash_book_support` đều khóa đúng một loại đối tác nên không diễn được ca này."""

Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    result = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        _ensure_refund_operation(session, context)
        _ensure_partner(session)
    return result


@pytest.fixture(scope="module")
def bank_account_id(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> int:
    return ensure_company_bank_account(session_factory, dataset_alpha, context, code="ADV-7C4")


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _ensure_refund_operation(session: Session, context: PostingContext) -> None:
    existing = session.scalar(
        select(AutoPostingRule.id).where(
            AutoPostingRule.package_id == context.package_id,
            AutoPostingRule.document_type == "PC",
            AutoPostingRule.operation_code == REFUND_OPERATION,
        )
    )
    if existing is None:
        session.add(
            AutoPostingRule(
                package_id=context.package_id,
                document_type="PC",
                operation_code=REFUND_OPERATION,
                operation_name="Trả lại tiền khách hàng",
                debit_purpose=None,
                credit_purpose=None,
                requires_partner=True,
                partner_kind=PartnerKind.CUSTOMER,
                display_order=9,
            )
        )
    session.flush()


def _ensure_partner(session: Session) -> None:
    """Một đối tác vừa là khách vừa là nhà cung cấp, KHÔNG khai điều khoản:
    khoản ứng trước phải để hạn trống bất kể danh mục nói gì."""
    if session.get(Partner, PARTNER_ID) is None:
        session.add(
            Partner(
                id=PARTNER_ID,
                code="DT7740",
                name="Đối tác ứng trước",
                path=f"{PARTNER_ID}.",
                is_customer=True,
                is_vendor=True,
            )
        )
    session.flush()


# ------------------------------------------------------------------ tiện ích


def _cash(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    kind: int,
    operation: str,
    debit_code: str,
    credit_code: str,
    amount_fc: Decimal,
    partner_kind: PartnerKind,
    settlements: tuple[CashSettlementIn, ...] = (),
) -> CashVoucherIn:
    return CashVoucherIn(
        kind=kind,
        operation_code=operation,
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=FEB_10,
        posting_date=FEB_10,
        currency_code="VND",
        exchange_rate=Decimal(1),
        partner_kind=partner_kind,
        partner_id=PARTNER_ID,
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts[debit_code],
                credit_account_id=accounts[credit_code],
                amount_fc=amount_fc,
                partner_kind=partner_kind,
                partner_id=PARTNER_ID,
            ),
        ),
        settlements=settlements,
    )


def _post_cash(session: Session, payload: CashVoucherIn) -> UUID:
    service = CashVoucherService(session)
    voucher = service.create(payload, user_id=ACTOR_ID)
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


def _journal_line(
    account_id: int,
    *,
    debit: int = 0,
    credit: int = 0,
    partner_kind: PartnerKind | None = None,
) -> JournalLineIn:
    return JournalLineIn(
        account_id=account_id,
        debit_fc=Decimal(debit),
        credit_fc=Decimal(credit),
        partner_id=None if partner_kind is None else PARTNER_ID,
        partner_kind=partner_kind,
    )


def _post_journal(
    session: Session,
    context: PostingContext,
    *,
    lines: tuple[JournalLineIn, ...],
    settlements: tuple[JournalSettlementIn, ...] = (),
) -> UUID:
    service = JournalVoucherService(session)
    voucher = service.create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=FEB_10,
            posting_date=FEB_10,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="chứng từ công nợ lát 7C-4",
            lines=lines,
            settlements=settlements,
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


def _entries(session: Session, voucher_id: UUID) -> list[ArApLedgerEntry]:
    return list(
        session.execute(select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == voucher_id))
        .scalars()
        .all()
    )


def _control_rows(session: Session, context: PostingContext) -> list[dict[str, object]]:
    """Dòng lệch của bản thảo check 131/331 — CHỈ của đối tác thuộc tệp này.

    Nạp thẳng từ gói: câu ấy chưa nằm trong `CHECKS` (ba điều kiện còn mở, xem
    đầu tệp `.sql`). Lọc theo `PARTNER_ID` vì câu đo theo CHI NHÁNH còn dataset
    dùng chung cả phiên — khẳng định trên toàn chi nhánh sẽ đỏ vì dư lượng của
    tệp khác, đúng cái bẫy đã bắt bốn lát liền.
    """
    sql = (
        resources.files("ket.posting.integrity.checks")
        .joinpath("arap_matches_control.sql")
        .read_text("utf-8")
    )
    rows = session.execute(text(sql), {"branch_id": context.branch_id}).mappings()
    return [dict(row) for row in rows if row["partner_id"] == PARTNER_ID]


# --------------------------------------------------------- khoản ứng trước


def test_a_receipt_without_settlements_becomes_a_reverse_direction_row(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Phiếu thu Nợ 111 / Có 131 không chọn đối trừ = khách ứng trước.

    Đây là chính hình dạng của điều kiện #2: hợp lệ, thường ngày, và trước lát
    này nó làm nhích 131 trên sổ cái mà không để lại dấu vết nào ở sổ phụ.
    """

    def work(session: Session) -> object:
        voucher_id = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(300_000),
                partner_kind=PartnerKind.CUSTOMER,
            ),
        )
        entries = _entries(session, voucher_id)
        assert len(entries) == 1
        advance = entries[0]
        assert advance.target_kind == SettlementTargetKind.ADVANCE_FROM_CUSTOMER
        assert advance.partner_kind == PartnerKind.CUSTOMER
        assert advance.account_id == accounts["131"]
        assert advance.amount == Decimal(300_000)
        assert advance.settled == Decimal(0)
        assert advance.ledger == FINANCIAL_LEDGER
        # Không có gì "đến hạn" ở một khoản ta đang giữ tiền của người khác.
        assert advance.due_date is None

        assert _control_rows(session, context) == []
        return None

    run(work)


def test_unposting_takes_the_advance_row_back(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Bất biến 7A: dòng sổ phụ chỉ tồn tại khi chứng từ đang ở trạng thái đã
    ghi sổ — đường bỏ ghi sổ của chứng từ tiền phải gỡ nó như ba phân hệ kia."""

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        voucher = service.create(
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(120_000),
                partner_kind=PartnerKind.CUSTOMER,
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        assert len(_entries(session, voucher.id)) == 1

        service.unpost(voucher.id, user_id=ACTOR_ID)
        assert _entries(session, voucher.id) == []
        assert _control_rows(session, context) == []
        return None

    run(work)


def test_a_bank_credit_advice_without_settlements_becomes_an_advance_row(
    run: Runner, context: PostingContext, accounts: dict[str, int], bank_account_id: int
) -> None:
    """Chứng từ tiền gửi đi cùng một luật — hai phân hệ dùng chung
    `posting.debt_lines.record_pair_voucher_debt`, không hai bản sao."""

    def work(session: Session) -> object:
        service = BankVoucherService(session)
        voucher = service.create(
            BankVoucherIn(
                kind=BankVoucherKind.CREDIT_ADVICE,
                operation_code="thu-no-khach-hang",
                bank_account_id=bank_account_id,
                branch_id=context.branch_id,
                document_date=FEB_10,
                posting_date=FEB_10,
                currency_code="VND",
                exchange_rate=Decimal(1),
                partner_kind=PartnerKind.CUSTOMER,
                partner_id=PARTNER_ID,
                lines=(
                    BankVoucherLineIn(
                        debit_account_id=accounts["112"],
                        credit_account_id=accounts["131"],
                        amount_fc=Decimal(80_000),
                        partner_kind=PartnerKind.CUSTOMER,
                        partner_id=PARTNER_ID,
                    ),
                ),
                settlements=(),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        entries = _entries(session, voucher.id)
        assert [entry.target_kind for entry in entries] == [
            SettlementTargetKind.ADVANCE_FROM_CUSTOMER
        ]
        assert _control_rows(session, context) == []
        return None

    run(work)


def test_a_prepayment_to_a_vendor_takes_the_receivable_direction(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Trả trước người bán là quyền của ta ⇒ chiều PHẢI THU, dù đối tác là NCC.

    Chiều của khoản ứng trước đi ngược loại đối tác của chính nó; suy chiều
    theo `partner_kind` sẽ xếp nó nhầm nhóm ở cả báo cáo lẫn câu check.
    """

    def work(session: Session) -> object:
        voucher_id = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.PAYMENT,
                operation="tra-no-ncc",
                debit_code="331",
                credit_code="111",
                amount_fc=Decimal(200_000),
                partner_kind=PartnerKind.VENDOR,
            ),
        )
        entries = _entries(session, voucher_id)
        assert [entry.target_kind for entry in entries] == [SettlementTargetKind.ADVANCE_TO_VENDOR]
        assert _control_rows(session, context) == []
        return None

    run(work)


# ------------------------------------------------- bù ứng trước với hóa đơn


def test_an_advance_is_offset_against_a_later_invoice(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Đường bù (quyết định user 2026-09-06): một chứng từ nghiệp vụ khác.

    Dòng bên THUẬN (Nợ 131) trỏ vào khoản ứng trước, dòng bên NGƯỢC (Có 131)
    trỏ vào khoản phải thu. Sổ cái không đổi — hai bút toán đã ghi từ trước —
    nên đẳng thức chỉ đúng khi CẢ HAI dòng sổ phụ cùng giảm.
    """

    def work(session: Session) -> object:
        advance_voucher = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(500_000),
                partner_kind=PartnerKind.CUSTOMER,
            ),
        )
        advance = _entries(session, advance_voucher)[0]

        debt_voucher = _post_journal(
            session,
            context,
            lines=(
                _journal_line(accounts["131"], debit=500_000, partner_kind=PartnerKind.CUSTOMER),
                _journal_line(accounts["642"], credit=500_000),
            ),
        )
        debt = _entries(session, debt_voucher)[0]
        assert debt.target_kind == SettlementTargetKind.JOURNAL_RECEIVABLE

        _post_journal(
            session,
            context,
            lines=(
                _journal_line(accounts["131"], debit=500_000, partner_kind=PartnerKind.CUSTOMER),
                _journal_line(accounts["131"], credit=500_000, partner_kind=PartnerKind.CUSTOMER),
            ),
            settlements=(
                JournalSettlementIn(
                    line_no=1,
                    target_kind=SettlementTargetKind.ADVANCE_FROM_CUSTOMER,
                    target_id=advance.id,
                    amount_fc=Decimal(500_000),
                ),
                JournalSettlementIn(
                    line_no=2,
                    target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                    target_id=debt.id,
                    amount_fc=Decimal(500_000),
                ),
            ),
        )

        session.refresh(advance)
        session.refresh(debt)
        assert advance.settled == Decimal(500_000)
        assert advance.is_closed is True
        assert debt.settled == Decimal(500_000)
        assert debt.is_closed is True

        assert _control_rows(session, context) == []
        return None

    run(work)


def test_a_refund_payment_settles_the_advance(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Đường thứ hai để đóng khoản ứng trước: trả lại tiền bằng phiếu chi.

    Phiếu chi cho khách hàng đứng ở bên THUẬN của phải thu, nên nó tất toán
    khoản ứng trước — không phải khoản nợ. Không có đường này thì tiền khách
    ứng mà đơn hàng không thành sẽ treo vĩnh viễn trên sổ phụ.
    """

    def work(session: Session) -> object:
        advance_voucher = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(90_000),
                partner_kind=PartnerKind.CUSTOMER,
            ),
        )
        advance = _entries(session, advance_voucher)[0]

        refund = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.PAYMENT,
                operation=REFUND_OPERATION,
                debit_code="131",
                credit_code="111",
                amount_fc=Decimal(90_000),
                partner_kind=PartnerKind.CUSTOMER,
                settlements=(
                    CashSettlementIn(
                        target_kind=SettlementTargetKind.ADVANCE_FROM_CUSTOMER,
                        target_id=advance.id,
                        amount_fc=Decimal(90_000),
                    ),
                ),
            ),
        )
        # Phiếu có khối đối trừ thì mọi chuyển động công nợ của nó coi như đã
        # đi qua `settled` — không sinh thêm khoản nào. Xấp xỉ, không bất biến:
        # xem docstring `record_pair_voucher_debt` và điều kiện #9 ở đầu
        # `arap_matches_control.sql`.
        assert _entries(session, refund) == []

        session.refresh(advance)
        assert advance.settled == Decimal(90_000)
        assert _control_rows(session, context) == []
        return None

    run(work)


# ------------------------------------------------------------ luật biên chiều


def test_a_receipt_cannot_settle_an_advance(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Thu thêm tiền KHÔNG tất toán được khoản ta đang nợ khách.

    Khoản ứng trước nằm trên CÙNG tài khoản và CÙNG đối tác với hóa đơn của
    chính khách ấy, nên bốn phép kiểm đích có sẵn (đối tác / chi nhánh / tiền
    tệ / tài khoản) đều cho qua — chỉ phép kiểm CHIỀU tách được hai thứ.
    """

    def work(session: Session) -> object:
        advance_voucher = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(70_000),
                partner_kind=PartnerKind.CUSTOMER,
            ),
        )
        advance = _entries(session, advance_voucher)[0]

        with pytest.raises(PostingValidationError) as error:
            CashVoucherService(session).create(
                _cash(
                    context,
                    accounts,
                    kind=CashVoucherKind.RECEIPT,
                    operation="thu-no-khach-hang",
                    debit_code="111",
                    credit_code="131",
                    amount_fc=Decimal(70_000),
                    partner_kind=PartnerKind.CUSTOMER,
                    settlements=(
                        CashSettlementIn(
                            target_kind=SettlementTargetKind.ADVANCE_FROM_CUSTOMER,
                            target_id=advance.id,
                            amount_fc=Decimal(70_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        assert {violation.code for violation in error.value.violations} == {
            SETTLEMENT_DIRECTION_MISMATCH_CODE
        }
        return None

    run(work)


def test_a_receipt_settles_a_manually_booked_receivable(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Lỗ đã shipping ở 7C-3: `receivables` cấp source cho `JOURNAL_RECEIVABLE`
    và màn thu tiền liệt kê nó, nhưng `cash_settlements` còn khóa trần ở
    `OPENING_BALANCE` — lượt ghi nổ CHECK ở DB thay vì chạy."""

    def work(session: Session) -> object:
        debt_voucher = _post_journal(
            session,
            context,
            lines=(
                _journal_line(accounts["131"], debit=150_000, partner_kind=PartnerKind.CUSTOMER),
                _journal_line(accounts["642"], credit=150_000),
            ),
        )
        debt = _entries(session, debt_voucher)[0]

        receipt = _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(150_000),
                partner_kind=PartnerKind.CUSTOMER,
                settlements=(
                    CashSettlementIn(
                        target_kind=SettlementTargetKind.JOURNAL_RECEIVABLE,
                        target_id=debt.id,
                        amount_fc=Decimal(150_000),
                    ),
                ),
            ),
        )
        stored = list(
            session.execute(
                select(CashSettlement).where(CashSettlement.voucher_id == receipt)
            ).scalars()
        )
        assert [row.target_kind for row in stored] == [SettlementTargetKind.JOURNAL_RECEIVABLE]

        session.refresh(debt)
        assert debt.settled == Decimal(150_000)
        assert _control_rows(session, context) == []
        return None

    run(work)


def test_the_control_draft_measures_nothing_outside_a_running_fiscal_year(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Điều kiện #8: không có năm tài chính đang chạy ⇒ câu check không đo gì.

    Vế sổ cái neo vào năm phủ `CURRENT_DATE`, còn nhánh `ar_ap_ledger` của vế
    sổ phụ cố ý KHÔNG lọc năm (điều kiện #4). Thiếu phép nối tắt-cùng-lúc thì
    một ngày chạy job nằm ngoài mọi năm tài chính — đầu tháng 1 trước khi mở
    niên độ mới — cho vế sổ cái rỗng và vế sổ phụ đầy, và `FULL JOIN` biến MỌI
    khoản công nợ đang treo thành một dòng đỏ.

    Bài mô phỏng ca ấy bằng cách làm CTE `current_year` rỗng, vì `CURRENT_DATE`
    không giả lập được từ đây.
    """

    def work(session: Session) -> object:
        _post_cash(
            session,
            _cash(
                context,
                accounts,
                kind=CashVoucherKind.RECEIPT,
                operation="thu-no-khach-hang",
                debit_code="111",
                credit_code="131",
                amount_fc=Decimal(45_000),
                partner_kind=PartnerKind.CUSTOMER,
            ),
        )
        sql = (
            resources.files("ket.posting.integrity.checks")
            .joinpath("arap_matches_control.sql")
            .read_text("utf-8")
        )
        without_year = sql.replace(
            "WHERE CURRENT_DATE BETWEEN y.start_date AND y.end_date",
            "WHERE FALSE",
        )
        assert without_year != sql
        rows = list(session.execute(text(without_year), {"branch_id": context.branch_id}))
        assert rows == []
        return None

    run(work)
