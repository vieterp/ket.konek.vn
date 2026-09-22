"""Lắp ráp / tháo dỡ theo định mức (FR-STK-005/016, lát 8C-2, ADR-027).

Một phiếu = một dòng thành phẩm + N dòng linh kiện. Giá vế nhập do engine suy từ
vế xuất cùng phiếu mỗi vòng (`assembly_in_legs.sql` Σ; `disassembly_in_legs.sql`
chia theo tỷ lệ, dư dồn dòng cuối); bút toán nằm trên dòng linh kiện. Chuỗi lắp
ráp nhiều vòng qua hai tháng hội tụ trong MỘT lượt engine (FR-STK-005).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    SECOND_WAREHOUSE_ID,
    UNIT_PIECE_ID,
    assembly_payload,
    financial_postings,
    fresh_item,
    in_movement,
    marks_of_branch,
    movements_of,
    out_movement,
    post_assembly,
    post_issue,
    post_receipt,
    run_engine,
    seed_inventory_package_data,
    set_system_setting,
    set_valuation_method,
)
from ket.kernel.config.catalog import MONEY_SCALE_KEY, STOCK_NEGATIVE_WARNING_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import PostingValidationError
from ket.kernel.periods.models import InventoryValuationMethod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.costing.uncosted import uncosted_vouchers
from ket.modules.inventory.guards import STOCK_NEGATIVE_CODE
from ket.modules.inventory.models import (
    CostState,
    InventoryMovement,
    InventoryVoucherKind,
    MovementDirection,
    StockLayer,
)
from ket.modules.inventory.schemas import InventoryVoucherIn, InventoryVoucherLineIn
from ket.modules.inventory.service import (
    ASSEMBLY_COMPONENT_IS_PRODUCT_CODE,
    SOURCE_ON_RECEIPT_SIDE_CODE,
    SPECIFIC_SOURCE_REQUIRED_CODE,
    InventoryVoucherService,
)
from posting_support import PostingContext, posting_scope, seed_posting_context
from test_opening_balances_carry_forward import ensure_fiscal_year

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_1, MAR_2, MAR_5, MAR_10 = (
    date(2026, 3, 1),
    date(2026, 3, 2),
    date(2026, 3, 5),
    date(2026, 3, 10),
)
APR_5, APR_10 = date(2026, 4, 5), date(2026, 4, 10)
NOV_15, DEC_1, DEC_5 = date(2026, 11, 15), date(2026, 12, 1), date(2026, 12, 5)
JAN_10_NEXT = date(2027, 1, 10)
ASSEMBLY = InventoryVoucherKind.ASSEMBLY
DISASSEMBLY = InventoryVoucherKind.DISASSEMBLY


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


@pytest.fixture(autouse=True)
def _guard_off(run: Runner) -> None:
    run(lambda session: set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "none"))


def _pairs(session: Session, voucher_id: UUID) -> list[tuple[int, Decimal, Decimal]]:
    return [
        (p.account_id, p.debit, p.credit)
        for p in financial_postings(session, voucher_id)
        if p.debit > 0 or p.credit > 0
    ]


def _product_in(session: Session, voucher_id: UUID) -> InventoryMovement:
    return in_movement(session, voucher_id)


def _stock_two_components(
    session: Session, context: PostingContext, accounts: dict[str, int], tag: str
) -> tuple[int, int]:
    """Hai linh kiện: 10 × 20.000 và 20 × 5.000 nhập ngày 1/3 → tổng 300.000."""
    c1, c2 = fresh_item(session, f"{tag}-C1"), fresh_item(session, f"{tag}-C2")
    post_receipt(
        session,
        context,
        accounts,
        item_id=c1,
        posting_date=MAR_1,
        quantity=Decimal(10),
        unit_cost=Decimal(20_000),
    )
    post_receipt(
        session,
        context,
        accounts,
        item_id=c2,
        posting_date=MAR_1,
        quantity=Decimal(20),
        unit_cost=Decimal(5_000),
    )
    return c1, c2


# ------------------------------------------------------------------ lắp ráp


def test_assembly_product_costs_the_sum_of_components_and_posts_on_component_lines(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        c1, c2 = _stock_two_components(session, context, accounts, "LR1")
        product = fresh_item(session, "LR1-P")
        pair = (accounts["156"], accounts["152"])
        voucher_id = post_assembly(
            session,
            context,
            kind=ASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product,
            product_quantity=Decimal(2),
            components=((c1, Decimal(10), None), (c2, Decimal(20), None)),
            accounts=pair,
        )
        rows = movements_of(session, voucher_id)
        assert sorted(m.direction for m in rows) == [-1, -1, 1]
        assert all(m.cost_state == CostState.PENDING for m in rows)
        # Chưa tính giá → 0 dòng GL (thiết kế "giá vốn tính sau", 8A).
        assert _pairs(session, voucher_id) == []

        result = run_engine(session, context)
        assert result.passes >= 2  # vòng 1 linh kiện, vòng 2 thành phẩm STALE → COSTED

        product_in = _product_in(session, voucher_id)
        assert product_in.item_id == product
        assert product_in.cost_state == CostState.COSTED
        assert product_in.amount == Decimal("300000.00")
        assert product_in.unit_cost == Decimal("150000.000000")
        outs = [
            m for m in movements_of(session, voucher_id) if m.direction == MovementDirection.OUT
        ]
        assert sorted(m.amount for m in outs) == [Decimal("100000.00"), Decimal("200000.00")]

        # GL: hai dòng linh kiện, mỗi dòng một cặp Nợ 156 / Có 152; thành phẩm không GL.
        pairs = _pairs(session, voucher_id)
        debits = sorted(d for account, d, c in pairs if account == pair[0] and d > 0)
        credits = sorted(c for account, d, c in pairs if account == pair[1] and c > 0)
        assert debits == [Decimal("100000.00"), Decimal("200000.00")]
        assert credits == [Decimal("100000.00"), Decimal("200000.00")]
        assert len(pairs) == 4

    run(work)


def test_multi_round_assembly_across_two_months_converges_in_one_run(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-STK-005: A lắp tháng 3 từ linh kiện, B lắp tháng 4 từ A. Nhập lùi ngày
    đổi giá linh kiện → MỘT lượt engine đổi cả A và B (hai tháng)."""

    def work(session: Session) -> None:
        comp = fresh_item(session, "LR2-C")
        a, b = fresh_item(session, "LR2-A"), fresh_item(session, "LR2-B")
        post_receipt(
            session,
            context,
            accounts,
            item_id=comp,
            posting_date=MAR_1,
            quantity=Decimal(10),
            unit_cost=Decimal(20_000),
        )
        lr_a = post_assembly(
            session,
            context,
            kind=ASSEMBLY,
            posting_date=MAR_5,
            product_item_id=a,
            product_quantity=Decimal(1),
            components=((comp, Decimal(10), None),),
            accounts=(accounts["156"], accounts["152"]),
        )
        lr_b = post_assembly(
            session,
            context,
            kind=ASSEMBLY,
            posting_date=APR_5,
            product_item_id=b,
            product_quantity=Decimal(1),
            components=((a, Decimal(1), None),),
            accounts=(accounts["156"], accounts["156"]),
        )
        first = run_engine(session, context)
        assert first.passes >= 3
        assert _product_in(session, lr_a).amount == Decimal("200000.00")
        assert _product_in(session, lr_b).amount == Decimal("200000.00")
        assert out_movement(session, lr_b).amount == Decimal("200000.00")

        # Nhập lùi ngày 2/3 giá khác → bình quân linh kiện ngày 5/3 = 30.000.
        post_receipt(
            session,
            context,
            accounts,
            item_id=comp,
            posting_date=MAR_2,
            quantity=Decimal(10),
            unit_cost=Decimal(40_000),
        )
        second = run_engine(session, context)
        assert second.passes >= 3
        assert _product_in(session, lr_a).amount == Decimal("300000.00")
        assert out_movement(session, lr_b).amount == Decimal("300000.00")
        assert _product_in(session, lr_b).amount == Decimal("300000.00")
        assert _product_in(session, lr_b).cost_state == CostState.COSTED
        assert second.vouchers_reposted >= 2
        # Bút toán của cả hai phiếu ghi lại theo giá mới.
        assert sorted(d for _, d, _c in _pairs(session, lr_b) if d > 0) == [Decimal("300000.00")]
        assert marks_of_branch(session, context.branch_id) == []

    run(work)


def test_assembled_product_used_next_year_gets_a_next_year_mark(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Review 8C-2 H-1: khóa thành phẩm chỉ vào lượt qua STALE (vế nhập LR) — dấu
    đầu năm sau phải được để TRƯỚC khi `finalize.sql` hạ STALE, không thì giá
    xuất thành phẩm ở năm sau lệch im lặng."""

    def work(session: Session) -> None:
        ensure_fiscal_year(session, "2027", date(2027, 1, 1))
        comp, a = fresh_item(session, "LR9-C"), fresh_item(session, "LR9-A")
        post_receipt(
            session,
            context,
            accounts,
            item_id=comp,
            posting_date=DEC_1,
            quantity=Decimal(10),
            unit_cost=Decimal(20_000),
        )
        lr = post_assembly(
            session,
            context,
            kind=ASSEMBLY,
            posting_date=DEC_5,
            product_item_id=a,
            product_quantity=Decimal(1),
            components=((comp, Decimal(10), None),),
        )
        issue = post_issue(
            session, context, accounts, item_id=a, posting_date=JAN_10_NEXT, quantity=Decimal(1)
        )
        run_engine(session, context)
        run_engine(session, context)  # năm 2027 nhặt dấu đầu năm do lượt 2026 để lại
        assert out_movement(session, issue).amount == Decimal("200000.00")

        # Nhập lùi ngày ở năm 2026 đổi giá linh kiện → thành phẩm A đổi giá → A
        # có movement năm 2027 phải nhận dấu đầu năm sau.
        post_receipt(
            session,
            context,
            accounts,
            item_id=comp,
            posting_date=NOV_15,
            quantity=Decimal(10),
            unit_cost=Decimal(40_000),
        )
        run_engine(session, context)
        assert _product_in(session, lr).amount == Decimal("300000.00")
        marked = {(m.item_id, m.from_date) for m in marks_of_branch(session, context.branch_id)}
        assert (a, date(2027, 1, 1)) in marked
        run_engine(session, context)
        assert out_movement(session, issue).amount == Decimal("300000.00")
        assert marks_of_branch(session, context.branch_id) == []

    run(work)


# ------------------------------------------------------------------ tháo dỡ


def test_disassembly_splits_product_value_by_ratio_and_posts_on_component_in_legs(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        product = fresh_item(session, "TD1-P")
        c1, c2 = fresh_item(session, "TD1-C1"), fresh_item(session, "TD1-C2")
        post_receipt(
            session,
            context,
            accounts,
            item_id=product,
            posting_date=MAR_1,
            quantity=Decimal(1),
            unit_cost=Decimal(300_000),
        )
        pair = (accounts["152"], accounts["155"])
        voucher_id = post_assembly(
            session,
            context,
            kind=DISASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product,
            product_quantity=Decimal(1),
            components=((c1, Decimal(2), Decimal(2)), (c2, Decimal(1), Decimal(1))),
            accounts=pair,
        )
        run_engine(session, context)
        product_out = out_movement(session, voucher_id)
        assert product_out.amount == Decimal("300000.00")
        ins = {
            m.item_id: m
            for m in movements_of(session, voucher_id)
            if m.direction == MovementDirection.IN
        }
        assert ins[c1].amount == Decimal("200000.00")
        assert ins[c1].unit_cost == Decimal("100000.000000")
        assert ins[c2].amount == Decimal("100000.00")
        assert all(m.cost_state == CostState.COSTED for m in ins.values())
        pairs = _pairs(session, voucher_id)
        assert sorted(d for account, d, _c in pairs if account == pair[0] and d > 0) == [
            Decimal("100000.00"),
            Decimal("200000.00"),
        ]
        assert len(pairs) == 4

    run(work)


def test_disassembly_rounding_remainder_lands_on_the_last_component_line(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        scale = value_of(session, key=MONEY_SCALE_KEY, user_id=ACTOR_ID)
        assert isinstance(scale, int)
        unit = Decimal(1).scaleb(-scale)
        product = fresh_item(session, "TD2-P")
        parts = [fresh_item(session, f"TD2-C{i}") for i in range(3)]
        post_receipt(
            session,
            context,
            accounts,
            item_id=product,
            posting_date=MAR_1,
            quantity=Decimal(1),
            unit_cost=Decimal(100),
        )
        voucher_id = post_assembly(
            session,
            context,
            kind=DISASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product,
            product_quantity=Decimal(1),
            components=tuple((p, Decimal(1), Decimal(1)) for p in parts),
        )
        run_engine(session, context)
        ins = sorted(
            (m for m in movements_of(session, voucher_id) if m.direction == MovementDirection.IN),
            key=lambda m: m.id,
        )
        amounts = [m.amount for m in ins]
        assert sum(amounts, Decimal(0)) == Decimal(100)
        assert max(amounts) - min(amounts) == unit
        # Tỷ lệ bằng nhau → dòng cuối (line_no lớn nhất) nhận phần dư.
        assert amounts[-1] == max(amounts)

        # Tỷ lệ tí hon ở dòng cuối (review L-1): phần dư dồn vào dòng LỚN NHẤT,
        # không dòng nào âm, Σ vẫn bằng total.
        product2 = fresh_item(session, "TD3-P")
        parts2 = [fresh_item(session, f"TD3-C{i}") for i in range(5)]
        post_receipt(
            session,
            context,
            accounts,
            item_id=product2,
            posting_date=MAR_1,
            quantity=Decimal(1),
            unit_cost=Decimal(1_000_003),
        )
        ratios = [Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal("0.000001")]
        voucher2 = post_assembly(
            session,
            context,
            kind=DISASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product2,
            product_quantity=Decimal(1),
            components=tuple((p, Decimal(1), r) for p, r in zip(parts2, ratios, strict=True)),
        )
        run_engine(session, context)
        amounts2 = [
            m.amount for m in movements_of(session, voucher2) if m.direction == MovementDirection.IN
        ]
        assert sum(amounts2, Decimal(0)) == Decimal(1_000_003)
        assert min(amounts2) >= Decimal(0)

    run(work)


# ---------------------------------------------------------------- hình thức


def _bare(context: PostingContext, kind: int, lines: tuple[InventoryVoucherLineIn, ...]) -> None:
    InventoryVoucherIn(
        kind=kind,
        operation_code="lap-rap",
        warehouse_id=MAIN_WAREHOUSE_ID,
        branch_id=context.branch_id,
        document_date=MAR_5,
        posting_date=MAR_5,
        currency_code="VND",
        lines=lines,
    )


def _line(item_id: int, **extra: object) -> InventoryVoucherLineIn:
    return InventoryVoucherLineIn(
        item_id=item_id,
        unit_id=UNIT_PIECE_ID,
        quantity=Decimal(1),
        **extra,  # type: ignore[arg-type]
    )


def test_assembly_shape_is_enforced_at_the_schema(context: PostingContext) -> None:
    with pytest.raises(ValidationError, match="đúng một dòng thành phẩm"):
        _bare(context, ASSEMBLY, (_line(1), _line(2)))
    with pytest.raises(ValidationError, match="đúng một dòng thành phẩm"):
        _bare(context, ASSEMBLY, (_line(1, is_product=True), _line(2, is_product=True)))
    with pytest.raises(ValidationError, match="ít nhất một dòng linh kiện"):
        _bare(context, ASSEMBLY, (_line(1, is_product=True),))
    with pytest.raises(ValidationError, match="tỷ lệ phân bổ"):
        _bare(context, DISASSEMBLY, (_line(1, is_product=True), _line(2)))
    with pytest.raises(ValidationError, match="Chỉ phiếu tháo dỡ"):
        _bare(context, ASSEMBLY, (_line(1, is_product=True), _line(2, allocation_ratio=Decimal(1))))
    with pytest.raises(ValidationError, match="Dòng thành phẩm không định khoản"):
        _bare(
            context,
            ASSEMBLY,
            (_line(1, is_product=True, debit_account_id=1, credit_account_id=2), _line(2)),
        )
    with pytest.raises(ValidationError, match="chỉ có trên phiếu lắp ráp"):
        _bare(context, InventoryVoucherKind.ISSUE, (_line(1, is_product=True),))


def test_component_cannot_be_the_product_and_in_side_lines_take_no_source(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        product = fresh_item(session, "LR3-P")
        comp = fresh_item(session, "LR3-C")
        service = InventoryVoucherService(session)
        with pytest.raises(PostingValidationError) as caught:
            service.create(
                assembly_payload(
                    context,
                    kind=ASSEMBLY,
                    posting_date=MAR_5,
                    product_item_id=product,
                    product_quantity=Decimal(1),
                    components=((product, Decimal(2), None),),
                ),
                user_id=ACTOR_ID,
            )
        assert [v.code for v in caught.value.violations] == [ASSEMBLY_COMPONENT_IS_PRODUCT_CODE]

        receipt = post_receipt(
            session,
            context,
            accounts,
            item_id=comp,
            posting_date=MAR_1,
            quantity=Decimal(5),
            unit_cost=Decimal(10),
        )
        payload = assembly_payload(
            context,
            kind=DISASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product,
            product_quantity=Decimal(1),
            components=((comp, Decimal(2), Decimal(1)),),
        )
        payload = payload.model_copy(
            update={
                "lines": tuple(
                    line
                    if line.is_product
                    else line.model_copy(
                        update={"source_movement_id": in_movement(session, receipt).id}
                    )
                    for line in payload.lines
                )
            }
        )
        with pytest.raises(PostingValidationError) as caught:
            service.create(payload, user_id=ACTOR_ID)
        assert [v.code for v in caught.value.violations] == [SOURCE_ON_RECEIPT_SIDE_CODE]

    run(work)


def test_negative_stock_guard_targets_component_lines_of_assembly_and_product_of_disassembly(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        set_system_setting(session, STOCK_NEGATIVE_WARNING_KEY, "block")
        product, comp = fresh_item(session, "LR4-P"), fresh_item(session, "LR4-C")
        service = InventoryVoucherService(session)
        lr = service.create(
            assembly_payload(
                context,
                kind=ASSEMBLY,
                posting_date=MAR_5,
                product_item_id=product,
                product_quantity=Decimal(1),
                components=((comp, Decimal(3), None),),
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(lr.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == STOCK_NEGATIVE_CODE
        assert violation.details["item_id"] == comp  # linh kiện, không phải thành phẩm

        td = service.create(
            assembly_payload(
                context,
                kind=DISASSEMBLY,
                posting_date=MAR_5,
                product_item_id=product,
                product_quantity=Decimal(1),
                components=((comp, Decimal(3), Decimal(1)),),
            ),
            user_id=ACTOR_ID,
        )
        with pytest.raises(PostingValidationError) as caught:
            service.post(td.id, user_id=ACTOR_ID)
        (violation,) = caught.value.violations
        assert violation.code == STOCK_NEGATIVE_CODE
        assert violation.details["item_id"] == product

    run(work)


def test_unposting_an_assembly_removes_both_legs_and_marks_both_keys(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        c1, c2 = _stock_two_components(session, context, accounts, "LR5")
        product = fresh_item(session, "LR5-P")
        voucher_id = post_assembly(
            session,
            context,
            kind=ASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product,
            product_quantity=Decimal(1),
            components=((c1, Decimal(10), None), (c2, Decimal(20), None)),
            product_warehouse_id=SECOND_WAREHOUSE_ID,
        )
        run_engine(session, context)
        assert _product_in(session, voucher_id).warehouse_id == SECOND_WAREHOUSE_ID
        InventoryVoucherService(session).unpost(voucher_id, user_id=ACTOR_ID)
        assert movements_of(session, voucher_id) == []
        marked = {(m.warehouse_id, m.item_id) for m in marks_of_branch(session, context.branch_id)}
        assert {
            (MAIN_WAREHOUSE_ID, c1),
            (MAIN_WAREHOUSE_ID, c2),
            (SECOND_WAREHOUSE_ID, product),
        } <= marked
        run_engine(session, context)  # dọn dấu, không lỗi

    run(work)


# -------------------------------------------------------- FIFO / đích danh


def test_assembled_product_is_a_fifo_layer_and_a_specific_issue_can_name_it(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def fifo(session: Session) -> None:
        set_valuation_method(session, context, InventoryValuationMethod.FIFO.value)
        try:
            c1, c2 = _stock_two_components(session, context, accounts, "LR6")
            product = fresh_item(session, "LR6-P")
            lr = post_assembly(
                session,
                context,
                kind=ASSEMBLY,
                posting_date=MAR_5,
                product_item_id=product,
                product_quantity=Decimal(3),
                components=((c1, Decimal(10), None), (c2, Decimal(20), None)),
            )
            issue = post_issue(
                session,
                context,
                accounts,
                item_id=product,
                posting_date=MAR_10,
                quantity=Decimal(1),
            )
            run_engine(session, context)
            layer = session.scalar(
                select(StockLayer).where(StockLayer.movement_id == _product_in(session, lr).id)
            )
            assert layer is not None
            assert layer.original_qty == Decimal(3)
            assert layer.remaining_qty == Decimal(2)
            assert out_movement(session, issue).amount == Decimal("100000.00")
        finally:
            set_valuation_method(
                session, context, InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value
            )

    def specific(session: Session) -> None:
        set_valuation_method(session, context, InventoryValuationMethod.SPECIFIC.value)
        try:
            comp, product = fresh_item(session, "LR7-C"), fresh_item(session, "LR7-P")
            receipt = post_receipt(
                session,
                context,
                accounts,
                item_id=comp,
                posting_date=MAR_1,
                quantity=Decimal(4),
                unit_cost=Decimal(25_000),
            )
            service = InventoryVoucherService(session)
            # Năm đích danh: dòng linh kiện (XUẤT) phải chỉ lần nhập.
            with pytest.raises(PostingValidationError) as caught:
                service.create(
                    assembly_payload(
                        context,
                        kind=ASSEMBLY,
                        posting_date=MAR_5,
                        product_item_id=product,
                        product_quantity=Decimal(1),
                        components=((comp, Decimal(4), None),),
                    ),
                    user_id=ACTOR_ID,
                )
            assert [v.code for v in caught.value.violations] == [SPECIFIC_SOURCE_REQUIRED_CODE]
            payload = assembly_payload(
                context,
                kind=ASSEMBLY,
                posting_date=MAR_5,
                product_item_id=product,
                product_quantity=Decimal(1),
                components=((comp, Decimal(4), None),),
            )
            payload = payload.model_copy(
                update={
                    "lines": tuple(
                        line
                        if line.is_product
                        else line.model_copy(
                            update={"source_movement_id": in_movement(session, receipt).id}
                        )
                        for line in payload.lines
                    )
                }
            )
            lr = service.create(payload, user_id=ACTOR_ID)
            service.post(lr.id, user_id=ACTOR_ID, acknowledged_warnings=True)
            # Thành phẩm chỉ có giá sau lượt engine — phiếu xuất đích danh trỏ
            # nó trước đó bị từ chối "lần nhập chưa có giá" (luật 8B), nên
            # quy trình là lắp → tính giá → xuất.
            run_engine(session, context)
            issue = post_issue(
                session,
                context,
                accounts,
                item_id=product,
                posting_date=MAR_10,
                quantity=Decimal(1),
                source_movement_id=_product_in(session, lr.id).id,
            )
            run_engine(session, context)
            assert _product_in(session, lr.id).amount == Decimal("100000.00")
            assert out_movement(session, issue).amount == Decimal("100000.00")
            assert out_movement(session, issue).cost_state == CostState.COSTED
        finally:
            set_valuation_method(
                session, context, InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value
            )

    run(fifo)
    run(specific)


# --------------------------------------------------------------- FR-STK-008


def test_uncosted_list_shows_the_assembly_until_the_engine_runs(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        c1, c2 = _stock_two_components(session, context, accounts, "LR8")
        product = fresh_item(session, "LR8-P")
        voucher_id = post_assembly(
            session,
            context,
            kind=ASSEMBLY,
            posting_date=MAR_5,
            product_item_id=product,
            product_quantity=Decimal(1),
            components=((c1, Decimal(10), None), (c2, Decimal(20), None)),
        )
        before = uncosted_vouchers(
            session, branch_id=context.branch_id, date_from=MAR_5, date_to=MAR_5
        )
        listed = {v.voucher_id: v for v in before.vouchers}
        assert voucher_id in listed
        assert listed[voucher_id].movements == 3
        assert listed[voucher_id].document_type == "LR"
        assert before.count >= 1
        run_engine(session, context)
        after = uncosted_vouchers(
            session, branch_id=context.branch_id, date_from=MAR_5, date_to=MAR_5
        )
        assert voucher_id not in {v.voucher_id for v in after.vouchers}

    run(work)
