"""Hàng đợi thủ kho + sổ kho / thẻ kho (SRS 17 §3.2, lát 8D) trên PostgreSQL thật.

* **Phân hệ tắt** (mặc định, FR-WHK-021): phiếu ghi sổ kế toán vào **thẳng** sổ
  kho theo ngày hạch toán, cùng transaction; `keeper_status = NOT_APPLICABLE`
  và bộ dấu `keeper_*` để trống (dấu ai-ghi-lúc-nào nằm trên dòng sổ kho).
* **Phân hệ bật**: phiếu treo `PENDING`, sổ kho rỗng cho tới khi thủ kho ghi;
  ghi hàng loạt là MỘT transaction — phiếu đầu tiên vi phạm làm cả lượt dừng.
* Ngày ghi sổ tùy chọn phải ≥ ngày hạch toán từng phiếu và ≤ hôm nay.
* Bỏ ghi sổ kế toán gỡ dòng sổ kho và trả phiếu về hàng đợi.
* Bộ quyền của vai Thủ kho **không có** `edit`/`delete` để cấp (FR-WHK-020).
* Thẻ kho cộng dồn từ tồn trước dòng đầu trang, không từ 0.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    enable_keeper,
    fresh_item,
    issue_payload,
    post_receipt,
    receipt_payload,
    seed_inventory_package_data,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import KeeperBookDateInvalidError, KeeperVoucherStateError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import REGISTRY as PERMISSION_REGISTRY
from ket.kernel.security.permissions import Action
from ket.modules.inventory import KEEPER_PERMISSION_MODULE, KEEPER_WAREHOUSE_BOOK_CODE
from ket.modules.inventory.keeper import book_rows, book_vouchers, card_opening_qty, pending_queue
from ket.modules.inventory.models import InventoryVoucher, KeeperStatus, WarehouseBookEntry
from ket.modules.inventory.service import InventoryVoucherService
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAY_5 = date(2026, 5, 5)
MAY_10 = date(2026, 5, 10)


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


def _book_of(session: Session, voucher_id: UUID) -> list[WarehouseBookEntry]:
    return list(
        session.execute(
            select(WarehouseBookEntry)
            .where(WarehouseBookEntry.voucher_id == voucher_id)
            .order_by(WarehouseBookEntry.id)
        )
        .scalars()
        .all()
    )


def test_module_disabled_books_straight_into_the_warehouse_book(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "keeper-off")
        voucher_id = post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=MAY_5,
            quantity=Decimal(20),
            unit_cost=Decimal(1_000),
        )
        body = session.get(InventoryVoucher, voucher_id)
        assert body is not None
        assert body.keeper_status == KeeperStatus.NOT_APPLICABLE
        # Dấu trên thân chỉ dành cho "THỦ KHO đã ghi" (CHECK keeper_booked_has_date).
        assert body.keeper_book_date is None
        assert body.keeper_posted_at is None

        rows = _book_of(session, voucher_id)
        assert len(rows) == 1
        assert rows[0].book_date == MAY_5
        assert rows[0].in_qty == Decimal(20)
        assert rows[0].out_qty == Decimal(0)
        assert rows[0].posted_by == ACTOR_ID
        assert pending_queue(session) == []

    run(work)


def test_module_enabled_queues_then_books_the_batch(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        enable_keeper(session)
        try:
            item_id = fresh_item(session, "keeper-on")
            service = InventoryVoucherService(session)
            first = service.create(
                receipt_payload(
                    context,
                    accounts,
                    posting_date=MAY_5,
                    quantity=Decimal(30),
                    unit_cost=Decimal(1_000),
                    item_id=item_id,
                ),
                user_id=ACTOR_ID,
            )
            service.post(first.id, user_id=ACTOR_ID)
            second = service.create(
                issue_payload(
                    context, accounts, posting_date=MAY_5, quantity=Decimal(5), item_id=item_id
                ),
                user_id=ACTOR_ID,
            )
            service.post(second.id, user_id=ACTOR_ID, acknowledged_warnings=True)

            assert _book_of(session, first.id) == []
            queued = {voucher.id for voucher, _body in pending_queue(session)}
            assert {first.id, second.id} <= queued

            written = book_vouchers(
                session,
                voucher_ids=(first.id, second.id),
                book_date=None,
                user_id=ACTOR_ID,
            )
            assert written == 2
            body = session.get(InventoryVoucher, first.id)
            assert body is not None
            assert body.keeper_status == KeeperStatus.BOOKED
            assert body.keeper_book_date == MAY_5
            assert body.keeper_posted_by == ACTOR_ID
            assert len(_book_of(session, second.id)) == 1
            assert _book_of(session, second.id)[0].out_qty == Decimal(5)
            # Đã ghi rồi thì không nằm trong hàng đợi nữa.
            assert first.id not in {voucher.id for voucher, _ in pending_queue(session)}
        finally:
            enable_keeper(session, enabled=False)

    run(work)


def test_booking_twice_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        enable_keeper(session)
        try:
            item_id = fresh_item(session, "keeper-twice")
            service = InventoryVoucherService(session)
            voucher = service.create(
                receipt_payload(
                    context,
                    accounts,
                    posting_date=MAY_5,
                    quantity=Decimal(7),
                    unit_cost=Decimal(1_000),
                    item_id=item_id,
                ),
                user_id=ACTOR_ID,
            )
            service.post(voucher.id, user_id=ACTOR_ID)
            book_vouchers(session, voucher_ids=(voucher.id,), book_date=None, user_id=ACTOR_ID)
            with pytest.raises(KeeperVoucherStateError):
                book_vouchers(session, voucher_ids=(voucher.id,), book_date=None, user_id=ACTOR_ID)
        finally:
            enable_keeper(session, enabled=False)

    run(work)


def test_batch_is_all_or_nothing_when_one_voucher_is_not_queued(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Phiếu chưa ghi sổ kế toán trong lô làm CẢ lượt dừng — và phiếu hợp lệ
    đứng trước nó không được để lại dòng sổ kho nào sau khi transaction rã."""

    def prepare(session: Session) -> tuple[UUID, UUID]:
        enable_keeper(session)
        item_id = fresh_item(session, "keeper-atomic")
        service = InventoryVoucherService(session)
        good = service.create(
            receipt_payload(
                context,
                accounts,
                posting_date=MAY_5,
                quantity=Decimal(9),
                unit_cost=Decimal(1_000),
                item_id=item_id,
            ),
            user_id=ACTOR_ID,
        )
        service.post(good.id, user_id=ACTOR_ID)
        draft = service.create(
            receipt_payload(
                context,
                accounts,
                posting_date=MAY_5,
                quantity=Decimal(3),
                unit_cost=Decimal(1_000),
                item_id=item_id,
            ),
            user_id=ACTOR_ID,
        )
        return good.id, draft.id

    good_id, draft_id = run(prepare)  # type: ignore[misc]

    def attempt(session: Session) -> None:
        with pytest.raises(KeeperVoucherStateError):
            book_vouchers(
                session, voucher_ids=(good_id, draft_id), book_date=None, user_id=ACTOR_ID
            )
        # Ném ra khỏi `unit_of_work` để cả lô rã — đó mới là "một transaction".
        raise KeeperVoucherStateError("lô hỏng thì rã cả lô")

    with pytest.raises(KeeperVoucherStateError):
        run(attempt)

    def verify(session: Session) -> None:
        assert _book_of(session, good_id) == []
        body = session.get(InventoryVoucher, good_id)
        assert body is not None
        assert body.keeper_status == KeeperStatus.PENDING
        enable_keeper(session, enabled=False)

    run(verify)


def test_custom_book_date_must_not_precede_posting_date_nor_follow_today(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def prepare(session: Session) -> UUID:
        enable_keeper(session)
        item_id = fresh_item(session, "keeper-dates")
        service = InventoryVoucherService(session)
        voucher = service.create(
            receipt_payload(
                context,
                accounts,
                posting_date=MAY_10,
                quantity=Decimal(4),
                unit_cost=Decimal(1_000),
                item_id=item_id,
            ),
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        return voucher.id

    voucher_id = run(prepare)  # type: ignore[misc]

    def too_early(session: Session) -> None:
        with pytest.raises(KeeperBookDateInvalidError):
            book_vouchers(
                session,
                voucher_ids=(voucher_id,),
                book_date=MAY_5,
                user_id=ACTOR_ID,
                today=date(2026, 12, 31),
            )

    run(too_early)

    def in_the_future(session: Session) -> None:
        with pytest.raises(KeeperBookDateInvalidError):
            book_vouchers(
                session,
                voucher_ids=(voucher_id,),
                book_date=MAY_10 + timedelta(days=1),
                user_id=ACTOR_ID,
                today=MAY_10,
            )
        enable_keeper(session, enabled=False)

    run(in_the_future)


def test_unposting_removes_the_book_rows_and_requeues(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "keeper-unpost")
        voucher_id = post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=MAY_5,
            quantity=Decimal(6),
            unit_cost=Decimal(1_000),
        )
        assert len(_book_of(session, voucher_id)) == 1
        InventoryVoucherService(session).unpost(voucher_id, user_id=ACTOR_ID)
        assert _book_of(session, voucher_id) == []
        body = session.get(InventoryVoucher, voucher_id)
        assert body is not None
        assert body.keeper_status == KeeperStatus.PENDING
        assert body.keeper_book_date is None

    run(work)


def test_keeper_role_has_no_edit_or_delete_permission_to_grant() -> None:
    """FR-WHK-020 bằng THIẾT KẾ bộ quyền: không có mã `edit`/`delete` để cấp."""
    document = next(
        item
        for item in PERMISSION_REGISTRY.document_types()
        if item.module == KEEPER_PERMISSION_MODULE and item.code == KEEPER_WAREHOUSE_BOOK_CODE
    )
    assert document.actions == frozenset({Action.VIEW, Action.POST})


def test_warehouse_card_running_quantity_starts_from_stock_before_the_page(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "keeper-card")
        for day, quantity in ((MAY_5, Decimal(10)), (MAY_10, Decimal(4))):
            post_receipt(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=day,
                quantity=quantity,
                unit_cost=Decimal(1_000),
            )
        rows, total = book_rows(
            session,
            warehouse_id=MAIN_WAREHOUSE_ID,
            item_id=item_id,
            from_date=None,
            to_date=None,
            limit=1,
            offset=1,
        )
        assert total == 2
        assert len(rows) == 1
        opening = card_opening_qty(
            session,
            warehouse_id=MAIN_WAREHOUSE_ID,
            item_id=item_id,
            before_date=rows[0].book_date,
            before_id=rows[0].id,
        )
        # Trang 2 mở đầu bằng 10 (dòng trang 1), không bằng 0.
        assert opening == Decimal(10)
        assert opening + rows[0].in_qty - rows[0].out_qty == Decimal(14)

    run(work)


def test_empty_batch_and_duplicates_are_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        with pytest.raises(KeeperVoucherStateError):
            book_vouchers(session, voucher_ids=(), book_date=None, user_id=ACTOR_ID)
        duplicate = uuid4()
        with pytest.raises(KeeperVoucherStateError):
            book_vouchers(
                session,
                voucher_ids=(duplicate, duplicate),
                book_date=None,
                user_id=ACTOR_ID,
            )

    run(work)
