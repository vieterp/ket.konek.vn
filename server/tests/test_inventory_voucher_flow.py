"""Vòng đời phiếu kho NK/XK/CK trên PostgreSQL thật (lát 8A).

Kiểm những bất biến RIÊNG của phân hệ kho — luật ghi sổ chung đã có
`test_posting_engine_flow.py`:

* Cất cấp số `NK{YY}-`/`XK{YY}-`/`CK{YY}-`; nghiệp vụ phải thuộc gói (FR-SYS-025);
  dịch vụ không nhập xuất được; đơn vị chưa khai tỷ lệ bị từ chối; phiếu nhập
  gõ tay phải có giá.
* Ghi sổ phiếu nhập → movement **theo đơn vị chính** (thùng 12 → 12 × số thùng),
  `sequence_in_day` 1..n, `COSTED` với giá VND, bút toán Nợ 155 / Có 154 bằng
  đúng thành tiền.
* Ghi sổ phiếu xuất → **0 dòng GL** (giá vốn chờ engine 8B) mà vẫn `DA_GHI_SO`,
  movement `PENDING`; đây là điều kiện tiên quyết của thiết kế "giá vốn tính sau".
* Chuyển kho → cặp movement cùng `line_id`, tổng tồn toàn công ty không đổi
  (BR-STK-06), không bút toán.
* Bỏ ghi sổ → movement mất, dấu bẩn tính lại đúng khóa/ngày; ghi sổ lùi ngày
  khi đã có movement muộn hơn → dấu bẩn (SRS 19 §9 #6).
* Hai phiên ghi sổ cùng khóa cùng ngày: phiên hai chờ advisory lock (RT-09),
  thứ tự không trùng.
* Sắp xếp lại thứ tự trong ngày: tập id phải khớp, `cost_state` về `STALE`,
  dấu bẩn từ ngày đó.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    BOX_FACTOR,
    GOODS_ITEM_ID,
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    SERVICE_ITEM_ID,
    UNIT_BOX_ID,
    UNIT_PIECE_ID,
    issue_payload,
    movements_of,
    receipt_payload,
    seed_inventory_package_data,
    transfer_payload,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.models import (
    CostState,
    InventoryRecalcMark,
    InventoryVoucherKind,
    MovementDirection,
)
from ket.modules.inventory.movements import REORDER_SET_MISMATCH_CODE, reorder_day
from ket.modules.inventory.schemas import InventoryVoucherLineIn, ReorderDayIn
from ket.modules.inventory.service import (
    ISSUE_COST_NOT_ALLOWED_CODE,
    ITEM_NOT_STOCKED_CODE,
    OPERATION_UNKNOWN_CODE,
    RECEIPT_COST_REQUIRED_CODE,
    UNIT_NOT_DECLARED_CODE,
    InventoryVoucherService,
)
from ket.modules.inventory.stock import stock_rows
from ket.posting.contracts import VoucherStatus
from ket.posting.engine.models import GlPosting, Ledger
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_5 = date(2026, 3, 5)
MAR_10 = date(2026, 3, 10)
MAR_20 = date(2026, 3, 20)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    return seed_inventory_package_data(session_factory, dataset_alpha, context)


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _postings(session: Session, voucher_id: UUID) -> list[GlPosting]:
    return list(
        session.execute(
            select(GlPosting)
            .where(GlPosting.voucher_id == voucher_id, GlPosting.ledger == Ledger.FINANCIAL)
            .order_by(GlPosting.line_no)
        )
        .scalars()
        .all()
    )


def _marks(session: Session, branch_id: int) -> list[InventoryRecalcMark]:
    return list(
        session.execute(
            select(InventoryRecalcMark).where(InventoryRecalcMark.branch_id == branch_id)
        )
        .scalars()
        .all()
    )


def _clear_marks(session: Session, branch_id: int) -> None:
    for mark in _marks(session, branch_id):
        session.delete(mark)
    session.flush()


def _post_receipt(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    quantity: Decimal = Decimal(100),
    unit_cost: Decimal = Decimal(20_000),
    unit_id: int = UNIT_PIECE_ID,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
    lot_no: str | None = None,
) -> UUID:
    service = InventoryVoucherService(session)
    voucher = service.create(
        receipt_payload(
            context,
            accounts,
            posting_date=posting_date,
            quantity=quantity,
            unit_cost=unit_cost,
            unit_id=unit_id,
            warehouse_id=warehouse_id,
            lot_no=lot_no,
        ),
        user_id=ACTOR_ID,
    )
    service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


# ----------------------------------------------------------------- cất phiếu


def test_receipt_gets_its_own_number_series_and_operation_must_exist(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        service = InventoryVoucherService(session)
        voucher = service.create(
            receipt_payload(context, accounts, posting_date=MAR_10), user_id=ACTOR_ID
        )
        assert voucher.voucher_no.startswith("NK26-")
        assert voucher.status == VoucherStatus.DA_CAT
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                receipt_payload(context, accounts, posting_date=MAR_10, operation="mua-hang-hoa"),
                user_id=ACTOR_ID,
            )
        assert caught.value.violations[0].code == OPERATION_UNKNOWN_CODE

    run(work)


def test_services_units_without_factor_and_priceless_receipts_are_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        service = InventoryVoucherService(session)
        with pytest.raises(PostingValidationError) as service_item:
            service.create(
                receipt_payload(context, accounts, posting_date=MAR_10, item_id=SERVICE_ITEM_ID),
                user_id=ACTOR_ID,
            )
        assert service_item.value.violations[0].code == ITEM_NOT_STOCKED_CODE
        # Thùng chỉ khai cho hàng hóa, không khai cho thành phẩm.
        payload = receipt_payload(context, accounts, posting_date=MAR_10, unit_id=UNIT_BOX_ID)
        payload = payload.model_copy(
            update={
                "lines": (payload.lines[0].model_copy(update={"item_id": 8122}),),
            }
        )
        with pytest.raises(PostingValidationError) as unit:
            service.create(payload, user_id=ACTOR_ID)
        assert unit.value.violations[0].code == UNIT_NOT_DECLARED_CODE
        priceless = receipt_payload(context, accounts, posting_date=MAR_10)
        priceless = priceless.model_copy(
            update={"lines": (priceless.lines[0].model_copy(update={"unit_cost_fc": None}),)}
        )
        with pytest.raises(PostingValidationError) as cost:
            service.create(priceless, user_id=ACTOR_ID)
        assert cost.value.violations[0].code == RECEIPT_COST_REQUIRED_CODE
        # Chiều ngược: phiếu xuất KHÔNG nhận giá — giá xuất là của engine (M-3).
        priced_issue = issue_payload(context, accounts, posting_date=MAR_10)
        priced_issue = priced_issue.model_copy(
            update={
                "lines": (priced_issue.lines[0].model_copy(update={"unit_cost_fc": Decimal(1)}),)
            }
        )
        with pytest.raises(PostingValidationError) as issue_cost:
            service.create(priced_issue, user_id=ACTOR_ID)
        assert issue_cost.value.violations[0].code == ISSUE_COST_NOT_ALLOWED_CODE

    run(work)


# ----------------------------------------------------------------- ghi sổ


def test_posting_a_receipt_writes_base_quantity_movements_and_a_balanced_pair(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """3 thùng × 12 = 36 cái; giá 240.000/thùng → 20.000/cái; thành tiền 720.000."""

    def work(session: Session) -> None:
        voucher_id = _post_receipt(
            session,
            context,
            accounts,
            posting_date=MAR_10,
            quantity=Decimal(3),
            unit_cost=Decimal(240_000),
            unit_id=UNIT_BOX_ID,
        )
        (movement,) = movements_of(session, voucher_id)
        assert movement.direction == MovementDirection.IN
        assert movement.quantity == Decimal(3) * BOX_FACTOR
        assert movement.cost_state == CostState.COSTED
        assert movement.unit_cost == Decimal("20000.000000")
        assert movement.amount == Decimal("720000.00")
        assert movement.sequence_in_day >= 1
        assert movement.lot_key == 0

        postings = _postings(session, voucher_id)
        assert [(row.account_id, row.debit, row.credit) for row in postings] == [
            (accounts["155"], Decimal("720000.00"), Decimal(0)),
            (accounts["154"], Decimal(0), Decimal("720000.00")),
        ]
        assert postings[0].item_id == GOODS_ITEM_ID
        assert postings[0].warehouse_id == MAIN_WAREHOUSE_ID

    run(work)


def test_an_issue_posts_with_zero_gl_lines_and_a_pending_cost(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        service = InventoryVoucherService(session)
        voucher = service.create(
            issue_payload(context, accounts, posting_date=MAR_10, quantity=Decimal(5)),
            user_id=ACTOR_ID,
        )
        assert voucher.voucher_no.startswith("XK26-")
        posted = service.post(voucher.id, user_id=ACTOR_ID)
        assert posted.status == VoucherStatus.DA_GHI_SO
        assert _postings(session, voucher.id) == []
        (movement,) = movements_of(session, voucher.id)
        assert movement.direction == MovementDirection.OUT
        assert movement.cost_state == CostState.PENDING
        assert movement.unit_cost is None and movement.amount is None

    run(work)


def test_a_transfer_moves_quantity_between_warehouses_without_changing_the_total(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        _post_receipt(session, context, accounts, posting_date=MAR_5, quantity=Decimal(50))
        before = {
            (row.warehouse_id): row.on_hand
            for row in stock_rows(
                session, as_of=MAR_20, branch_id=context.branch_id, item_id=GOODS_ITEM_ID
            )
        }
        service = InventoryVoucherService(session)
        voucher = service.create(
            transfer_payload(context, posting_date=MAR_10, quantity=Decimal(10)), user_id=ACTOR_ID
        )
        assert voucher.voucher_no.startswith("CK26-")
        service.post(voucher.id, user_id=ACTOR_ID)
        legs = movements_of(session, voucher.id)
        assert [(leg.warehouse_id, leg.direction) for leg in legs] == [
            (MAIN_WAREHOUSE_ID, MovementDirection.OUT),
            (SECOND_WAREHOUSE_ID, MovementDirection.IN),
        ]
        assert legs[0].line_id == legs[1].line_id
        assert _postings(session, voucher.id) == []
        after = {
            (row.warehouse_id): row.on_hand
            for row in stock_rows(
                session, as_of=MAR_20, branch_id=context.branch_id, item_id=GOODS_ITEM_ID
            )
        }
        assert sum(after.values()) == sum(before.values())
        assert after[MAIN_WAREHOUSE_ID] == before[MAIN_WAREHOUSE_ID] - Decimal(10)
        assert after.get(SECOND_WAREHOUSE_ID, Decimal(0)) == before.get(
            SECOND_WAREHOUSE_ID, Decimal(0)
        ) + Decimal(10)

    run(work)


def test_unposting_removes_movements_and_marks_the_key_for_recalc(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        _clear_marks(session, context.branch_id)
        voucher_id = _post_receipt(session, context, accounts, posting_date=MAR_10)
        service = InventoryVoucherService(session)
        service.unpost(voucher_id, user_id=ACTOR_ID)
        assert movements_of(session, voucher_id) == []
        marks = _marks(session, context.branch_id)
        assert [(m.warehouse_id, m.item_id, m.lot_key, m.from_date) for m in marks] == [
            (MAIN_WAREHOUSE_ID, GOODS_ITEM_ID, 0, MAR_10)
        ]
        # Ghi sổ lại → thứ tự cấp mới, chứ không giữ số cũ.
        service.post(voucher_id, user_id=ACTOR_ID)
        assert len(movements_of(session, voucher_id)) == 1

    run(work)


def test_a_backdated_receipt_marks_recalc_from_its_own_date(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        _clear_marks(session, context.branch_id)
        _post_receipt(session, context, accounts, posting_date=MAR_20)
        _clear_marks(session, context.branch_id)
        _post_receipt(session, context, accounts, posting_date=MAR_5)
        marks = _marks(session, context.branch_id)
        assert [(m.item_id, m.from_date) for m in marks] == [(GOODS_ITEM_ID, MAR_5)]
        # Dấu muộn hơn không che dấu sớm: upsert giữ MIN(from_date).
        _post_receipt(session, context, accounts, posting_date=MAR_10)
        assert [(m.from_date) for m in _marks(session, context.branch_id)] == [MAR_5]

    run(work)


def test_lot_numbers_create_lots_and_split_the_stock_key(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        first = _post_receipt(session, context, accounts, posting_date=MAR_10, lot_no="LO-A")
        second = _post_receipt(session, context, accounts, posting_date=MAR_10, lot_no="LO-A")
        third = _post_receipt(session, context, accounts, posting_date=MAR_10, lot_no="LO-B")
        (a1,) = movements_of(session, first)
        (a2,) = movements_of(session, second)
        (b,) = movements_of(session, third)
        assert a1.lot_id == a2.lot_id and a1.lot_id is not None
        assert b.lot_id != a1.lot_id
        assert a2.sequence_in_day == a1.sequence_in_day + 1
        assert b.sequence_in_day == 1
        rows = stock_rows(session, as_of=MAR_20, branch_id=context.branch_id, item_id=GOODS_ITEM_ID)
        by_lot = {row.lot_id: row.on_hand for row in rows if row.warehouse_id == MAIN_WAREHOUSE_ID}
        assert by_lot[a1.lot_id] == Decimal(200)
        assert by_lot[b.lot_id] == Decimal(100)

    run(work)


# --------------------------------------------------------------- đua thứ tự


def test_two_sessions_posting_the_same_key_on_the_same_day_are_serialised(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """RT-09 mặt khóa: phiên một giữ advisory lock của khóa tồn kho tới khi
    commit; phiên hai thử `pg_try_advisory_xact_lock` cùng khóa phải thấy bận."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    in_post, release = threading.Event(), threading.Event()
    outcome: dict[str, object] = {}

    def first() -> None:
        try:
            with unit_of_work(session_factory, scope) as session:
                outcome["first"] = _post_receipt(
                    session,
                    context,
                    accounts,
                    posting_date=MAR_20,
                    warehouse_id=SECOND_WAREHOUSE_ID,
                )
                in_post.set()
                assert release.wait(timeout=30), "test không nhả gate"
        except BaseException as exc:  # pragma: no cover - chỉ chạy khi test hỏng
            outcome["error"] = exc
            in_post.set()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert in_post.wait(timeout=30), f"lượt ghi sổ không tới gate: {outcome.get('error')}"
        with unit_of_work(session_factory, scope) as session:
            # Khóa mang schema của dataset (advisory lock là phạm vi cả database).
            busy = session.scalar(
                sql_text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {
                    "key": (
                        f"inventory:{dataset_alpha.schema_name}:{context.branch_id}:"
                        f"{SECOND_WAREHOUSE_ID}:{GOODS_ITEM_ID}:0"
                    )
                },
            )
            assert busy is False, "khóa tồn kho phải còn bị phiên một giữ"
    finally:
        release.set()
        thread.join(timeout=30)
    assert "error" not in outcome, outcome.get("error")

    with unit_of_work(session_factory, scope) as session:
        second = _post_receipt(
            session, context, accounts, posting_date=MAR_20, warehouse_id=SECOND_WAREHOUSE_ID
        )
        first_seq = movements_of(session, outcome["first"])[0].sequence_in_day  # type: ignore[arg-type]
        second_seq = movements_of(session, second)[0].sequence_in_day
        assert second_seq == first_seq + 1


# -------------------------------------------------------- sắp xếp trong ngày


def test_reorder_day_renumbers_marks_stale_and_needs_the_exact_set(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        day = date(2026, 4, 2)
        a = _post_receipt(
            session, context, accounts, posting_date=day, warehouse_id=SECOND_WAREHOUSE_ID
        )
        b = _post_receipt(
            session, context, accounts, posting_date=day, warehouse_id=SECOND_WAREHOUSE_ID
        )
        (ma,) = movements_of(session, a)
        (mb,) = movements_of(session, b)
        assert (ma.sequence_in_day, mb.sequence_in_day) == (1, 2)
        base = {
            "branch_id": context.branch_id,
            "warehouse_id": SECOND_WAREHOUSE_ID,
            "item_id": GOODS_ITEM_ID,
            "posting_date": day,
        }
        with pytest.raises(PostingValidationError) as caught:
            reorder_day(session, ReorderDayIn(**base, ordered_movement_ids=(mb.id,)))
        assert caught.value.violations[0].code == REORDER_SET_MISMATCH_CODE

        _clear_marks(session, context.branch_id)
        changed, marked_from = reorder_day(
            session, ReorderDayIn(**base, ordered_movement_ids=(mb.id, ma.id))
        )
        assert (changed, marked_from) == (2, day)
        (ma,) = movements_of(session, a)
        (mb,) = movements_of(session, b)
        assert (mb.sequence_in_day, ma.sequence_in_day) == (1, 2)
        assert ma.cost_state == CostState.STALE and mb.cost_state == CostState.STALE
        assert [(m.warehouse_id, m.from_date) for m in _marks(session, context.branch_id)] == [
            (SECOND_WAREHOUSE_ID, day)
        ]

    run(work)


def test_update_replaces_lines_and_keeps_kind_and_branch(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        service = InventoryVoucherService(session)
        voucher = service.create(
            receipt_payload(context, accounts, posting_date=MAR_10), user_id=ACTOR_ID
        )
        payload = receipt_payload(context, accounts, posting_date=MAR_10, quantity=Decimal(7))
        payload = payload.model_copy(
            update={
                "lines": (
                    *payload.lines,
                    InventoryVoucherLineIn(
                        item_id=GOODS_ITEM_ID,
                        unit_id=UNIT_BOX_ID,
                        quantity=Decimal(1),
                        unit_cost_fc=Decimal(120_000),
                        debit_account_id=accounts["155"],
                        credit_account_id=accounts["154"],
                    ),
                )
            }
        )
        service.update(
            voucher.id, payload, expected_row_version=voucher.row_version, user_id=ACTOR_ID
        )
        _, body, lines = service.get(voucher.id)
        assert body.kind == InventoryVoucherKind.RECEIPT
        assert [
            (line.line_no, line.base_quantity, line.unit_cost_fc, line.amount_fc) for line in lines
        ] == [
            (1, Decimal(7), Decimal("20000.000000"), Decimal("140000.00")),
            (2, BOX_FACTOR, Decimal("10000.000000"), Decimal("120000.00")),
        ]
        wrong_kind = payload.model_copy(
            update={"kind": InventoryVoucherKind.ISSUE, "operation_code": "xuat-khac"}
        )
        with pytest.raises(PostingValidationError):
            service.update(
                voucher.id, wrong_kind, expected_row_version=voucher.row_version, user_id=ACTOR_ID
            )

    run(work)
