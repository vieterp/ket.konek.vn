"""Check toàn vẹn `settlement_matches_subledger` (BR-QUY-02) — lát 7A.

Phép kiểm này canh **lệch giữa đường cộng và đường gỡ** số đã đối trừ: nó so
`ar_ap_ledger.settled`/`opening_balance_invoices.paid_amount` với tổng dòng
`cash_settlements`/`bank_settlements` của những chứng từ đang ghi sổ trỏ vào
chúng. Bốn nhóm ở đây tương ứng bốn cách nó có thể sai:

1. **Xanh trên dữ liệu đúng** — cả đường đối trừ thật (phiếu thu qua
   `CashVoucherService`) lẫn dòng sổ phụ chưa ai đụng tới.
2. **Bắt được lệch thật** — `UPDATE` thẳng bằng SQL, đúng thứ mà mọi phép kiểm
   toàn vẹn sinh ra để bắt (cùng khuôn `test_integrity_checks.py`: phá bằng
   đường vòng mà validator lúc ghi sổ không thấy).
3. **Không đếm chứng từ chưa ghi sổ** — phiếu mới Cất đã có dòng đối trừ nhưng
   chưa cộng vào sổ phụ; đếm nó là tự dựng ra chênh lệch.
4. **Không báo sai trên chứng từ ngoại tệ** — số VND so là `amount − fx_diff`
   chứ không `amount`, nếu không thì check đỏ đúng ở những chứng từ khó nhất.

Mỗi tệp test dùng một chi nhánh riêng (`seed_posting_context`), nên khẳng định
"không dòng nào" ở đây chỉ nói về dữ liệu của chính tệp này.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data, seed_open_invoice
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.protocols import SettlementTargetKind, SubledgerEntry
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashSettlementIn, CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.modules.receivables.ledger_service import SERVICE as LEDGER
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.integrity.checks.registry import check_of
from ket.posting.integrity.runner import run_check
from ket.posting.opening_balances.models import OpeningDetailKind
from posting_support import USD_RATE, PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

CHECK_CODE = "settlement_matches_subledger"
ACTOR_ID = 1
CUSTOMER = 9101

JAN_20 = date(2026, 1, 20)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    return seed_cash_book_package_data(session_factory, dataset_alpha, context)


@pytest.fixture
def scope(dataset_alpha: DatasetRef, context: PostingContext) -> RequestScope:
    return posting_scope(dataset_alpha, context, user_id=ACTOR_ID)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(session_factory: sessionmaker[Session], scope: RequestScope) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


# --------------------------------------------------------------------- khung


def _num(value: object) -> Decimal:
    """Cột số của báo cáo chênh lệch về `Decimal`.

    `runner._primitive` trả chuỗi cho mọi `Decimal` (báo cáo chênh lệch mà
    chính con số bị nhị phân hóa là tự phủ định mình), nên so bằng chuỗi sẽ
    buộc test biết trước `AMOUNT_SCALE` — một hằng không thuộc về nó.
    """
    return Decimal(str(value))


def _reported(session: Session, context: PostingContext) -> dict[str, dict[str, object]]:
    """Chạy check, trả các dòng chênh theo `target_id`.

    Đọc theo id chứ không theo tổng số dòng: khẳng định "tổng = 0" sẽ vỡ vì
    một lý do KHÁC ngay khi tệp test này thêm một kịch bản lệch cố ý, và một
    test vỡ vì lý do khác lý do nó viết ra là một test không còn nói gì.
    """
    outcome = run_check(session, check_of(CHECK_CODE), branch_id=context.branch_id)
    rows = {str(row["target_id"]): row for row in outcome.sample}
    assert len(rows) == outcome.total, "mẫu bị cắt — kịch bản test đang tạo quá nhiều dòng chênh"
    return rows


def _receipt(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    target_kind: SettlementTargetKind,
    target_id: UUID,
    amount_fc: Decimal,
    currency: str = "VND",
    rate: Decimal = Decimal(1),
) -> CashVoucherIn:
    """Phiếu thu tiền mặt đối trừ đúng một chứng từ công nợ của khách hàng."""
    return CashVoucherIn(
        kind=CashVoucherKind.RECEIPT,
        operation_code="thu-no-khach-hang",
        cash_account_id=accounts["111"],
        branch_id=context.branch_id,
        document_date=JAN_20,
        posting_date=JAN_20,
        currency_code=currency,
        exchange_rate=rate,
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=CUSTOMER,
        lines=(
            CashVoucherLineIn(
                debit_account_id=accounts["111"],
                credit_account_id=accounts["131"],
                amount_fc=amount_fc,
                partner_kind=PartnerKind.CUSTOMER,
                partner_id=CUSTOMER,
            ),
        ),
        settlements=(
            CashSettlementIn(target_kind=target_kind, target_id=target_id, amount_fc=amount_fc),
        ),
    )


def _seed_ar_ap_row(
    session: Session,
    context: PostingContext,
    *,
    document_no: str,
    amount: Decimal = Decimal("500000"),
) -> UUID:
    """Một dòng `ar_ap_ledger` treo dưới một chứng từ GLE làm chứng từ gốc.

    Lát 7A chưa có chứng từ mua/bán; thứ đang kiểm là **sổ phụ**, nên chứng từ
    gốc chỉ cần hợp lệ (cùng lập luận `test_ar_ap_ledger.py`).
    """
    voucher = JournalVoucherService(session).create(
        JournalVoucherIn(
            branch_id=context.branch_id,
            document_date=JAN_20,
            posting_date=JAN_20,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description=f"chứng từ gốc {document_no}",
            lines=(
                JournalLineIn(
                    account_id=context.accounts["131"],
                    debit_fc=amount,
                    partner_kind=PartnerKind.CUSTOMER,
                    partner_id=CUSTOMER,
                ),
                JournalLineIn(account_id=context.accounts["511"], credit_fc=amount),
            ),
        ),
        user_id=ACTOR_ID,
    )
    LEDGER.record(
        session,
        voucher_id=voucher.id,
        entries=[
            SubledgerEntry(
                target_kind=SettlementTargetKind.SALES_INVOICE,
                partner_kind=PartnerKind.CUSTOMER,
                partner_id=CUSTOMER,
                ledger=0,
                account_id=context.accounts["131"],
                document_no=document_no,
                document_date=JAN_20,
                due_date=None,
                currency_code="VND",
                exchange_rate=Decimal(1),
                amount_fc=amount,
                amount=amount,
            )
        ],
    )
    return (
        session.execute(select(ArApLedgerEntry.id).where(ArApLedgerEntry.document_id == voucher.id))
        .scalars()
        .one()
    )


# ------------------------------------------------------------ dữ liệu đúng


def test_untouched_subledger_rows_report_nothing(run: Runner, context: PostingContext) -> None:
    """Sổ phụ chưa ai đối trừ: hai vế cùng 0 — không phải một dòng chênh."""

    def work(session: Session) -> object:
        target_id = _seed_ar_ap_row(session, context, document_no="HD-CLEAN-0")
        reported = _reported(session, context)
        assert str(target_id) not in reported
        return None

    run(work)


def test_a_posted_receipt_leaves_the_two_sides_equal(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Đường đối trừ thật, cả hai nguồn sổ phụ, đối trừ TỪNG PHẦN.

    Từng phần chứ không trả hết: một bản cài so nhầm `amount` với `remaining`
    thay vì với tổng dòng đối trừ vẫn xanh khi hóa đơn được trả trọn.
    """
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER,
        amount_fc=Decimal("900000"),
        invoice_no="HD-OPEN-CLEAN",
    )

    def work(session: Session) -> object:
        arap_id = _seed_ar_ap_row(session, context, document_no="HD-CLEAN-1")
        service = CashVoucherService(session)
        for target_kind, target_id, amount_fc in (
            (SettlementTargetKind.OPENING_BALANCE, invoice_id, Decimal("400000")),
            (SettlementTargetKind.SALES_INVOICE, arap_id, Decimal("200000")),
        ):
            voucher = service.create(
                _receipt(
                    context,
                    accounts,
                    target_kind=target_kind,
                    target_id=target_id,
                    amount_fc=amount_fc,
                ),
                user_id=ACTOR_ID,
            )
            service.post(voucher.id, user_id=ACTOR_ID)

        reported = _reported(session, context)
        assert str(invoice_id) not in reported
        assert str(arap_id) not in reported
        return None

    run(work)


# ------------------------------------------------------------- lệch thật


def test_a_hand_edited_settled_column_is_reported(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """`UPDATE` thẳng vào `settled` — đúng đường vòng mà check sinh ra để bắt.

    Cộng thêm chứ không đặt lại về 0: một dòng sổ phụ 0 đối trừ và 0 dòng đối
    trừ là trạng thái BÌNH THƯỜNG, nên phá kiểu đó thì check im lặng đúng.
    """

    def work(session: Session) -> object:
        arap_id = _seed_ar_ap_row(session, context, document_no="HD-DRIFT-1")
        service = CashVoucherService(session)
        voucher = service.create(
            _receipt(
                context,
                accounts,
                target_kind=SettlementTargetKind.SALES_INVOICE,
                target_id=arap_id,
                amount_fc=Decimal("100000"),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        session.execute(
            sql_text("UPDATE ar_ap_ledger SET settled = settled + 1 WHERE id = :id"),
            {"id": arap_id},
        )
        session.flush()

        row = _reported(session, context)[str(arap_id)]
        assert row["source_table"] == "ar_ap_ledger"
        assert row["document_no"] == "HD-DRIFT-1"
        # Cột tự mô tả được chênh lệch: sổ phụ ghi 100.001, dòng đối trừ 100.000.
        assert _num(row["recorded_settled"]) == Decimal("100001")
        assert _num(row["settlement_rows_amount"]) == Decimal("100000")
        assert row["settlement_voucher_nos"] == voucher.voucher_no
        return None

    run(work)


def test_an_orphan_settlement_row_is_reported_too(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Chiều FULL JOIN thứ hai: dòng đối trừ còn, đích biến mất.

    Hai guard (`ensure_groups_not_settled`, `ensure_not_settled`) đóng lối này
    ở tầng dịch vụ, nên test phải xóa bằng SQL trực tiếp — và đó chính là lý do
    vế FULL JOIN tồn tại: nó là lưới đỡ cho ngày guard bị gỡ hay bị đi vòng.
    """

    def work(session: Session) -> object:
        arap_id = _seed_ar_ap_row(session, context, document_no="HD-ORPHAN")
        service = CashVoucherService(session)
        voucher = service.create(
            _receipt(
                context,
                accounts,
                target_kind=SettlementTargetKind.SALES_INVOICE,
                target_id=arap_id,
                amount_fc=Decimal("50000"),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        session.execute(sql_text("DELETE FROM ar_ap_ledger WHERE id = :id"), {"id": arap_id})
        session.flush()

        row = _reported(session, context)[str(arap_id)]
        # Không còn vế sổ phụ, nên các cột của nó rỗng — nhưng dòng vẫn mang
        # số chứng từ để lần về phiếu đang trỏ vào hư không.
        assert row["source_table"] is None
        assert _num(row["recorded_settled"]) == Decimal(0)
        assert _num(row["settlement_rows_amount"]) == Decimal("50000")
        assert row["settlement_voucher_nos"] == voucher.voucher_no
        return None

    run(work)


# ------------------------------------------------- chứng từ chưa ghi sổ


def test_a_settlement_on_an_unposted_voucher_is_ignored(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Phiếu mới Cất có dòng đối trừ nhưng chưa cộng vào sổ phụ.

    Đếm nó thì mọi phiếu đang soạn dở đều thành một dòng chênh — check sẽ đỏ
    trong giờ làm việc bình thường và người dùng học cách bỏ qua nó.
    """

    def work(session: Session) -> object:
        arap_id = _seed_ar_ap_row(session, context, document_no="HD-DRAFT")
        CashVoucherService(session).create(
            _receipt(
                context,
                accounts,
                target_kind=SettlementTargetKind.SALES_INVOICE,
                target_id=arap_id,
                amount_fc=Decimal("70000"),
            ),
            user_id=ACTOR_ID,
        )
        # Cố ý KHÔNG `post`: dòng `cash_settlements` đã có, `settled` vẫn 0.
        entry = session.get(ArApLedgerEntry, arap_id)
        assert entry is not None and entry.settled == Decimal(0)
        assert str(arap_id) not in _reported(session, context)
        return None

    run(work)


def test_unposting_a_voucher_takes_its_settlement_row_out_of_the_sum(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Bỏ ghi sổ gỡ `settled` VÀ rơi khỏi tổng — hai vế cùng về 0.

    Dòng `cash_settlements` ở lại sau khi bỏ ghi sổ; lọc theo `status = 2` là
    thứ giữ cho hai vế vẫn bằng nhau.
    """

    def work(session: Session) -> object:
        arap_id = _seed_ar_ap_row(session, context, document_no="HD-UNPOST")
        service = CashVoucherService(session)
        voucher = service.create(
            _receipt(
                context,
                accounts,
                target_kind=SettlementTargetKind.SALES_INVOICE,
                target_id=arap_id,
                amount_fc=Decimal("60000"),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        service.unpost(voucher.id, user_id=ACTOR_ID)

        entry = session.get(ArApLedgerEntry, arap_id)
        assert entry is not None and entry.settled == Decimal(0)
        assert str(arap_id) not in _reported(session, context)
        return None

    run(work)


# ------------------------------------------------------------ ngoại tệ


def test_an_fx_settlement_does_not_report_a_false_difference(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    run: Runner,
) -> None:
    """Hóa đơn USD ghi nhận ở 25.000, thu ở 26.000 → `fx_diff` ≠ 0.

    Số VND giải phóng trên sổ phụ là `fc × 25.000`, còn `cash_settlements.
    amount` là `fc × 26.000`. So bằng `amount` thì dòng này đỏ; đúng phép so
    là `amount − fx_diff`, và nó phải im.
    """
    invoice_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.RECEIVABLE,
        partner_id=CUSTOMER,
        currency_code="USD",
        exchange_rate=USD_RATE,
        amount_fc=Decimal(100),
        invoice_no="HD-FX-CHECK",
    )
    fc = Decimal(10)

    def work(session: Session) -> object:
        service = CashVoucherService(session)
        voucher = service.create(
            _receipt(
                context,
                accounts,
                target_kind=SettlementTargetKind.OPENING_BALANCE,
                target_id=invoice_id,
                amount_fc=fc,
                currency="USD",
                rate=Decimal(26_000),
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)

        reported = _reported(session, context)
        assert str(invoice_id) not in reported, (
            "chênh lệch tỷ giá bị đếm thành lệch sổ phụ — phép so đang dùng "
            "`amount` thay vì `amount - fx_diff`"
        )
        return None

    run(work)
