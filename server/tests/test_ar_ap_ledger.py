"""Sổ phụ công nợ `ar_ap_ledger` — lát 7A.

Bốn nhóm, tương ứng bốn thứ có thể hỏng ở một sổ phụ:

1. **Đường ghi** (`ArApSubledger.record`/`remove`) — thay-trọn chứ không cộng
   dồn, và không xóa được khoản đã có người trả vào.
2. **Đường đối trừ** (`SettlementTargetSource.apply`/`revert`) — không vượt số
   còn nợ, kiểm cả nguyên tệ lẫn VND, và `CHECK` của DB là chặn chót.
3. **Đua hai kết nối** (RT-16) — hai phiếu thu cùng đối trừ một hóa đơn phải
   nối tiếp nhau, không cùng đọc một `settled` cũ.
4. **Ranh giới** — hai provider khóa cứng chiều (sửa H-1 review 6B), và RLS
   theo chi nhánh trên chính bảng.

Bài test dùng chứng từ GLE làm chứng từ gốc: lát 7A chưa có loại chứng từ
mua/bán, và thứ đang kiểm là **sổ phụ**, không phải phân hệ sinh ra nó. Đổi
sang hóa đơn thật ở 7B/7C không làm các bất biến này khác đi.
"""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.protocols import PROVIDERS, SettlementTargetKind, SubledgerEntry
from ket.kernel.security.models import Branch
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.modules.receivables.ledger_service import SERVICE as LEDGER
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.receivables.settlement_source import SOURCE
from posting_support import (
    PostingContext,
    ensure_second_branch,
    posting_scope,
    seed_posting_context,
)

pytestmark = pytest.mark.db

ACTOR_ID = 1
RACE_ROUNDS = 5

_TODAY = date(2026, 3, 10)
_DUE = date(2026, 4, 9)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture
def scope(dataset_alpha: DatasetRef, context: PostingContext) -> RequestScope:
    return posting_scope(dataset_alpha, context, user_id=ACTOR_ID)


def _make_voucher(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> UUID:
    """Một chứng từ GLE đã Cất, làm cha hợp lệ cho `document_id`.

    **Không ghi sổ** ở đây: ghi sổ phụ thuộc kỳ kế toán chưa khóa — trạng thái
    toàn cục mà tệp test khác đổi được trên dataset dùng chung — nên chỉ bài
    nào THẬT SỰ cần chứng từ đã ghi sổ mới tự gọi `post()`. Trong tệp này đúng
    một bài như thế (`test_the_reference_guard_fires_on_unpost`).

    Dùng 642/111 chứ không 131/511 để bài ấy ghi sổ được: TK 131 bật
    `detail_tracking` customer nên bút toán thiếu chiều đối tác bị từ chối.
    Dòng sổ phụ gắn vào chứng từ qua `document_id`, không qua TK của bút toán.
    """
    with unit_of_work(session_factory, scope) as session:
        voucher = JournalVoucherService(session).create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=_TODAY,
                posting_date=_TODAY,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="chứng từ gốc của khoản công nợ",
                lines=(
                    JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(10_000)),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(10_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )
        return voucher.id


def _entry(
    context: PostingContext,
    *,
    amount: Decimal = Decimal(10_000),
    target_kind: SettlementTargetKind = SettlementTargetKind.SALES_INVOICE,
    partner_kind: PartnerKind = PartnerKind.CUSTOMER,
    partner_id: int = 1,
    document_no: str = "HD-001",
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    amount_fc: Decimal | None = None,
) -> SubledgerEntry:
    return SubledgerEntry(
        target_kind=target_kind,
        partner_kind=partner_kind,
        partner_id=partner_id,
        ledger=0,
        account_id=context.accounts["131" if partner_kind is PartnerKind.CUSTOMER else "331"],
        document_no=document_no,
        document_date=_TODAY,
        due_date=_DUE,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        amount_fc=amount if amount_fc is None else amount_fc,
        amount=amount,
    )


def _rows_of(session: Session, voucher_id: UUID) -> list[ArApLedgerEntry]:
    return list(
        session.execute(select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == voucher_id))
        .scalars()
        .all()
    )


# ------------------------------------------------------------------ đường ghi


def test_record_writes_one_row_per_entry(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(
            session,
            voucher_id=voucher_id,
            entries=[_entry(context, document_no="HD-001"), _entry(context, document_no="HD-002")],
        )
    with unit_of_work(session_factory, scope) as session:
        rows = _rows_of(session, voucher_id)
        assert sorted(row.document_no for row in rows) == ["HD-001", "HD-002"]
        assert all(row.settled == Decimal(0) for row in rows)
        assert all(row.is_closed is False for row in rows)


def test_record_replaces_instead_of_accumulating(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Ghi sổ → bỏ ghi sổ → sửa → ghi sổ lại là đường thường ngày.

    Một bản cài cộng dồn nhân đôi công nợ ở lượt thứ hai và chỉ lộ ra ở số dư
    131/331 nhiều kỳ sau — đúng thứ ADR-021 §Decision 3 cấm.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(
            session, voucher_id=voucher_id, entries=[_entry(context, amount=Decimal(7_000))]
        )
    with unit_of_work(session_factory, scope) as session:
        rows = _rows_of(session, voucher_id)
        assert len(rows) == 1
        assert rows[0].amount == Decimal(7_000)


def test_remove_deletes_every_row_of_the_voucher(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(
            session,
            voucher_id=voucher_id,
            entries=[_entry(context, document_no="HD-A"), _entry(context, document_no="HD-B")],
        )
    with unit_of_work(session_factory, scope) as session:
        LEDGER.remove(session, voucher_id=voucher_id)
    with unit_of_work(session_factory, scope) as session:
        assert _rows_of(session, voucher_id) == []


def test_remove_refuses_when_a_row_is_partly_settled(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Xóa khoản đã có người trả vào là bỏ lại phiếu thu trỏ vào hư không."""
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        SOURCE.apply(session, target_id=target_id, amount_fc=Decimal(3_000), amount=Decimal(3_000))
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(PostingValidationError) as caught:
            LEDGER.remove(session, voucher_id=voucher_id)
    assert caught.value.violations[0].code == "receivables.entry_already_settled"

    with unit_of_work(session_factory, scope) as session:
        assert len(_rows_of(session, voucher_id)) == 1


def test_record_refuses_to_overwrite_a_settled_row(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Cùng luật với `remove`: ghi đè cũng làm mất dấu lượt trả."""
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        SOURCE.apply(session, target_id=target_id, amount_fc=Decimal(1), amount=Decimal(1))
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(PostingValidationError):
            LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])


# -------------------------------------------------------------- đường đối trừ


def test_apply_and_revert_move_settled_both_ways(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id

    with unit_of_work(session_factory, scope) as session:
        SOURCE.apply(session, target_id=target_id, amount_fc=Decimal(4_000), amount=Decimal(4_000))
    with unit_of_work(session_factory, scope) as session:
        row = _rows_of(session, voucher_id)[0]
        assert row.settled == Decimal(4_000)
        assert row.is_closed is False

    with unit_of_work(session_factory, scope) as session:
        SOURCE.revert(session, target_id=target_id, amount_fc=Decimal(4_000), amount=Decimal(4_000))
    with unit_of_work(session_factory, scope) as session:
        assert _rows_of(session, voucher_id)[0].settled == Decimal(0)


def test_settling_the_whole_amount_closes_the_row(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """`is_closed` là cột SINH — không đường ghi nào phải nhớ cập nhật nó."""
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        SOURCE.apply(
            session, target_id=target_id, amount_fc=Decimal(10_000), amount=Decimal(10_000)
        )
    with unit_of_work(session_factory, scope) as session:
        assert _rows_of(session, voucher_id)[0].is_closed is True


def test_apply_refuses_to_exceed_the_remaining_amount(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(PostingValidationError) as caught:
            SOURCE.apply(
                session, target_id=target_id, amount_fc=Decimal(10_001), amount=Decimal(10_001)
            )
    assert caught.value.violations[0].code == "settlement.exceeds_remaining"


def test_apply_checks_the_vnd_side_on_its_own(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Nguyên tệ đủ mà VND vượt vẫn phải là 422 (review 6B H-2).

    Bỏ vế VND thì `CHECK settled_within_amount` của DB nổ thành
    IntegrityError 500 — cùng dữ liệu, hỏng hơn nhiều cho người dùng.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(
            session,
            voucher_id=voucher_id,
            entries=[
                _entry(
                    context,
                    currency_code="USD",
                    exchange_rate=Decimal("25000"),
                    amount_fc=Decimal(100),
                    amount=Decimal(2_500_000),
                )
            ],
        )
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(PostingValidationError) as caught:
            # Nguyên tệ vừa khít, VND vượt một đồng.
            SOURCE.apply(
                session,
                target_id=target_id,
                amount_fc=Decimal(100),
                amount=Decimal(2_500_001),
            )
    assert caught.value.violations[0].code == "settlement.exceeds_remaining"


def test_revert_more_than_applied_is_a_bug_not_a_violation(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Gỡ nhiều hơn đã ghi = đường ghi và đường gỡ đã lệch nhau — nổ chứ không
    kẹp về 0, cùng triết lý với nguồn số dư đầu kỳ."""
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(RuntimeError):
            SOURCE.revert(session, target_id=target_id, amount_fc=Decimal(1), amount=Decimal(1))


def test_apply_on_a_vanished_target_is_a_violation(
    session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(PostingValidationError) as caught:
            SOURCE.apply(session, target_id=uuid4(), amount_fc=Decimal(1), amount=Decimal(1))
    assert caught.value.violations[0].code == "settlement.target_missing"


def test_database_check_is_the_last_line_against_over_settling(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Chặn chót RT-16: một đường ghi đi vòng qua service vẫn phải nổ.

    Test này cố ý `UPDATE` thẳng bằng SQL — đúng thứ mà `CHECK` sinh ra để
    bắt, và là bằng chứng rằng phép kiểm ở tầng Python không phải chỗ duy nhất.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with pytest.raises(IntegrityError):
        with unit_of_work(session_factory, scope) as session:
            session.execute(
                sql_text("UPDATE ar_ap_ledger SET settled = amount + 1 WHERE id = :id"),
                {"id": target_id},
            )


# --------------------------------------------------------- đua hai kết nối


def test_two_settlements_of_one_invoice_are_serialised(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """RT-16 mặt khóa: `apply` giữ `FOR UPDATE` nên kết nối thứ hai phải chờ.

    Gate đặt SAU `apply` và trước commit — đúng cửa sổ mà hai phiếu thu đồng
    thời cần chen vào để cùng đọc một `settled` cũ.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id

    in_apply, release = threading.Event(), threading.Event()
    outcome: dict[str, BaseException] = {}

    def first() -> None:
        try:
            with unit_of_work(session_factory, scope) as session:
                SOURCE.apply(
                    session, target_id=target_id, amount_fc=Decimal(6_000), amount=Decimal(6_000)
                )
                in_apply.set()
                assert release.wait(timeout=30), "test không nhả gate"
        except BaseException as exc:  # pragma: no cover - chỉ chạy khi test hỏng
            outcome["error"] = exc
            in_apply.set()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert in_apply.wait(timeout=30), f"lượt đối trừ không tới gate: {outcome.get('error')}"
        with pytest.raises(OperationalError):
            with unit_of_work(session_factory, scope) as session:
                session.execute(
                    sql_text("SELECT id FROM ar_ap_ledger WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": target_id},
                )
    finally:
        release.set()
        thread.join(timeout=30)
    assert "error" not in outcome, outcome.get("error")


def _race_one_settlement_round(
    session_factory: sessionmaker[Session], scope: RequestScope, target_id: UUID
) -> list[BaseException]:
    """Hai luồng cùng đối trừ 6.000 vào một đích, thả cùng lúc bằng barrier.

    Tách khỏi vòng lặp gọi nó: closure định nghĩa TRONG vòng lặp bắt biến theo
    tham chiếu, nên cả hai luồng của vòng sau có thể đọc `target_id` của vòng
    trước — một bài test đua tự sinh ra cuộc đua thứ hai mà nó không kiểm.
    """
    failures: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=30)

    def settle() -> None:
        try:
            barrier.wait()
            with unit_of_work(session_factory, scope) as session:
                SOURCE.apply(
                    session,
                    target_id=target_id,
                    amount_fc=Decimal(6_000),
                    amount=Decimal(6_000),
                )
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=settle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return failures


def test_racing_settlements_never_exceed_the_invoice(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Bất biến của vòng đua: hai lượt 6.000 trên hóa đơn 10.000 — đúng MỘT
    lượt thắng, và tổng đã trả không bao giờ vượt giá trị hóa đơn."""
    for round_no in range(RACE_ROUNDS):
        voucher_id = _make_voucher(session_factory, scope, context)
        with unit_of_work(session_factory, scope) as session:
            LEDGER.record(
                session,
                voucher_id=voucher_id,
                entries=[_entry(context, document_no=f"HD-R{round_no}")],
            )
            target_id = _rows_of(session, voucher_id)[0].id

        failures = _race_one_settlement_round(session_factory, scope, target_id)

        assert len(failures) == 1, f"vòng {round_no}: phải đúng một lượt thua, {failures}"
        with unit_of_work(session_factory, scope) as session:
            assert _rows_of(session, voucher_id)[0].settled == Decimal(6_000)


# ----------------------------------------------------------------- ranh giới


def test_the_two_providers_are_locked_to_their_own_direction(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Sửa H-1 review 6B: hỏi registry chiều THU không được ra hóa đơn MUA.

    Một đối tượng trả lời theo `partner_kind` bất kể được hỏi qua registry nào
    sẽ đi vòng cổng quyền theo chiều ở tầng API.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(
            session,
            voucher_id=voucher_id,
            entries=[
                _entry(context, partner_id=101, document_no="HD-BAN"),
                _entry(
                    context,
                    target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                    partner_kind=PartnerKind.VENDOR,
                    partner_id=202,
                    document_no="HD-MUA",
                ),
            ],
        )

    with unit_of_work(session_factory, scope) as session:
        receivable = [
            invoice
            for provider in PROVIDERS.receivable_providers()
            for invoice in provider.open_invoices(
                session,
                partner_kind=PartnerKind.VENDOR,
                partner_id=202,
                branch_id=context.branch_id,
                as_of=_TODAY,
            )
        ]
        # Hóa đơn MUA của NCC 202 tồn tại, nhưng hỏi qua registry chiều THU
        # phải ra rỗng — không phải "ra vì partner_kind khớp".
        assert receivable == []

        payable = [
            invoice
            for provider in PROVIDERS.payable_providers()
            for invoice in provider.open_invoices(
                session,
                partner_kind=PartnerKind.VENDOR,
                partner_id=202,
                branch_id=context.branch_id,
                as_of=_TODAY,
            )
        ]
        assert [invoice.invoice_no for invoice in payable] == ["HD-MUA"]


def test_find_returns_only_rows_this_source_owns(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        found = SOURCE.find(session, target_ids=[target_id, uuid4()])
        assert [invoice.target_id for invoice in found] == [target_id]
        assert found[0].remaining == Decimal(10_000)


def test_rows_are_invisible_from_another_real_branch(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    context: PostingContext,
) -> None:
    """RLS trên chính bảng, không mượn phạm vi dòng cha.

    Bảng này bị đọc thẳng bởi màn chọn đối trừ, thẻ công nợ, báo cáo tuổi nợ
    và check toàn vẹn — bốn cửa, và một cửa quên join là một lỗ im lặng.

    Dùng chi nhánh **có thật** thứ hai chứ không phải một id bịa: phạm vi trỏ
    một chi nhánh không tồn tại chỉ chứng minh "fail-closed với đầu vào lạ",
    còn lỗ mà cột `branch_id` sinh ra để bịt là "người của chi nhánh A đọc
    được dòng của chi nhánh B" — hai mệnh đề khác nhau.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])

    # `ensure_second_branch` chỉ hứa "có ít nhất hai chi nhánh", KHÔNG hứa
    # chi nhánh nó trả về khác `context.branch_id` — chạy cả bộ thì các tệp
    # trước đã tạo sẵn vài chi nhánh và nó trả về đúng chi nhánh của bài này.
    # Chọn thẳng một id KHÁC mới là điều kiện bài test cần, và chọn thế thì
    # thứ tự tệp không đổi được kết quả.
    ensure_second_branch(session_factory, dataset_alpha)
    with unit_of_work(session_factory, scope) as session:
        other_branch_id = session.execute(
            select(Branch.id).where(Branch.id != context.branch_id).order_by(Branch.id).limit(1)
        ).scalar_one()

    other_branch_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(other_branch_id,),
        acting_branch_id=other_branch_id,
    )
    with unit_of_work(session_factory, other_branch_scope) as session:
        assert _rows_of(session, voucher_id) == []


def test_the_reference_guard_fires_on_unpost(
    session_factory: sessionmaker[Session], scope: RequestScope, context: PostingContext
) -> None:
    """Bộ guard DÙNG CHUNG thật sự chạy ở `PostingService.unpost`.

    `posting/documents/registry.py` nói thẳng: "đừng tin một lời gọi không có
    bài kiểm". Đây là bài kiểm ấy — nó đi qua `unpost` thật chứ không gọi
    `ensure_not_settled` trực tiếp, nên nếu ai đó gỡ `REFERENCE_GUARDS.register`
    khỏi `receivables/__init__` thì test này đỏ.
    """
    voucher_id = _make_voucher(session_factory, scope, context)
    with unit_of_work(session_factory, scope) as session:
        JournalVoucherService(session).post(voucher_id, user_id=ACTOR_ID)
        LEDGER.record(session, voucher_id=voucher_id, entries=[_entry(context)])
        target_id = _rows_of(session, voucher_id)[0].id
    with unit_of_work(session_factory, scope) as session:
        SOURCE.apply(session, target_id=target_id, amount_fc=Decimal(2_000), amount=Decimal(2_000))

    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(PostingValidationError) as caught:
            JournalVoucherService(session).unpost(voucher_id, user_id=ACTOR_ID)
    assert caught.value.violations[0].code == "receivables.entry_already_settled"
