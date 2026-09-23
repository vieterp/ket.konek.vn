"""Kiểm kê kho (U8, FR-STK-030/031, lát 8D) trên PostgreSQL thật.

Luồng U8 đầu-cuối và những chỗ nó dễ nói dối:

* Lập biên bản chụp tồn sổ sách, **hai dòng** cho khóa vừa có hàng của mình vừa
  có hàng giữ hộ; đơn giá phần thừa điền sẵn bằng bình quân hiện hành.
* `differences` trả **chỉ** dòng đã đếm và lệch.
* Duyệt sinh tối đa hai phiếu **nháp** (thừa → NK, thiếu → XK) đã định khoản từ
  `purpose` (`unexplained_surplus` 3381 / `unexplained_shortage` 1381 + TK kho
  theo `ItemNature`) — **không** qua một nghiệp vụ mới nào của gói.
* Chênh lệch hàng giữ hộ đi vào cùng phiếu nhưng là dòng `is_custodial` → phiếu
  toàn dòng giữ hộ ghi sổ ra **0 dòng GL**.
* Duyệt lần hai bị từ chối; xóa biên bản đã duyệt bị từ chối.
* Khóa chưa có cơ sở giá mà thừa → báo đích danh mã hàng, không bịa số 0.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from inventory_support import (
    MAIN_WAREHOUSE_ID,
    financial_postings,
    fresh_item,
    movements_of,
    post_custodial_receipt,
    post_issue,
    post_receipt,
    run_engine,
    seed_inventory_package_data,
)
from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import CountSheetAdjustmentError, CountSheetInvalidError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.inventory.count_sheet_service import InventoryCountSheetService
from ket.modules.inventory.models import InventoryVoucher, InventoryVoucherKind
from ket.modules.inventory.schemas import (
    InventoryCountSheetCountsIn,
    InventoryCountSheetIn,
    InventoryCountSheetLineCount,
)
from ket.modules.inventory.service import InventoryVoucherService
from ket.posting.contracts import VoucherStatus
from ket.posting.documents.models import Voucher
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
JUN_1 = date(2026, 6, 1)
JUN_30 = date(2026, 6, 30)


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


def _sheet_for(session: Session, context: PostingContext, item_ids: tuple[int, ...]) -> object:
    return InventoryCountSheetService(session).create(
        InventoryCountSheetIn(
            branch_id=context.branch_id,
            warehouse_id=MAIN_WAREHOUSE_ID,
            count_date=JUN_30,
            item_ids=item_ids,
        ),
        user_id=ACTOR_ID,
    )


def _account_code(session: Session, account_id: int) -> str:
    code = session.scalar(select(ChartOfAccount.code).where(ChartOfAccount.id == account_id))
    assert code is not None
    return code


def test_sheet_snapshots_book_quantity_and_prefills_average_cost(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-snapshot")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUN_1,
            quantity=Decimal(100),
            unit_cost=Decimal(15_000),
        )
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=JUN_1, quantity=Decimal(25)
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        assert sheet.sheet_no.startswith("KK26-")
        lines = service.lines_of(sheet.id)
        assert len(lines) == 2, "một khóa hai loại hàng ra HAI dòng đếm"
        owned = next(line for line in lines if not line.is_custodial)
        custodial = next(line for line in lines if line.is_custodial)
        assert owned.book_qty == Decimal(100)
        assert owned.unit_cost == Decimal("15000.000000")
        assert custodial.book_qty == Decimal(25)
        assert custodial.unit_cost is None

    run(work)


def test_differences_show_only_counted_and_mismatching_lines(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        matching = fresh_item(session, "count-match")
        mismatching = fresh_item(session, "count-mismatch")
        uncounted = fresh_item(session, "count-uncounted")
        for item_id in (matching, mismatching, uncounted):
            post_receipt(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=JUN_1,
                quantity=Decimal(10),
                unit_cost=Decimal(1_000),
            )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(matching, mismatching, uncounted),
            ),
            user_id=ACTOR_ID,
        )
        by_item = {line.item_id: line.line_no for line in service.lines_of(sheet.id)}
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(
                    InventoryCountSheetLineCount(
                        line_no=by_item[matching], counted_qty=Decimal(10)
                    ),
                    InventoryCountSheetLineCount(
                        line_no=by_item[mismatching], counted_qty=Decimal(12)
                    ),
                )
            ),
        )
        differences = service.differences(sheet.id)
        assert [row.item_id for row in differences] == [mismatching]
        assert differences[0].difference == Decimal(2)

    run(work)


def test_approving_creates_draft_receipt_and_issue_with_purpose_accounts(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        surplus_item = fresh_item(session, "count-surplus")
        deficit_item = fresh_item(session, "count-deficit")
        for item_id in (surplus_item, deficit_item):
            post_receipt(
                session,
                context,
                accounts,
                item_id=item_id,
                posting_date=JUN_1,
                quantity=Decimal(50),
                unit_cost=Decimal(2_000),
            )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(surplus_item, deficit_item),
            ),
            user_id=ACTOR_ID,
        )
        by_item = {line.item_id: line.line_no for line in service.lines_of(sheet.id)}
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(
                    InventoryCountSheetLineCount(
                        line_no=by_item[surplus_item], counted_qty=Decimal(53)
                    ),
                    InventoryCountSheetLineCount(
                        line_no=by_item[deficit_item], counted_qty=Decimal(46)
                    ),
                )
            ),
        )
        receipt, issue = service.apply_differences(sheet.id, user_id=ACTOR_ID)
        assert receipt is not None and issue is not None
        assert receipt.status == VoucherStatus.DA_CAT, "phiếu sinh ra là NHÁP"
        assert issue.status == VoucherStatus.DA_CAT

        receipt_body = session.get(InventoryVoucher, receipt.id)
        issue_body = session.get(InventoryVoucher, issue.id)
        assert receipt_body is not None and issue_body is not None
        assert receipt_body.kind == InventoryVoucherKind.RECEIPT
        assert receipt_body.operation_code == "nhap-khac"
        assert issue_body.kind == InventoryVoucherKind.ISSUE
        assert issue_body.operation_code == "xuat-khac"

        (receipt_line,) = InventoryVoucherService(session).get(receipt.id)[2]
        assert receipt_line.item_id == surplus_item
        assert receipt_line.base_quantity == Decimal(3)
        assert receipt_line.unit_cost_fc == Decimal("2000.000000")
        assert _account_code(session, receipt_line.debit_account_id or 0) == "156"
        assert _account_code(session, receipt_line.credit_account_id or 0) == "3381"

        (issue_line,) = InventoryVoucherService(session).get(issue.id)[2]
        assert issue_line.item_id == deficit_item
        assert issue_line.base_quantity == Decimal(4)
        assert issue_line.unit_cost_fc is None, "giá xuất do engine tính"
        assert _account_code(session, issue_line.debit_account_id or 0) == "1381"
        assert _account_code(session, issue_line.credit_account_id or 0) == "156"

    run(work)


def test_custodial_difference_makes_a_voucher_without_any_ledger_line(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-custodial")
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=JUN_1, quantity=Decimal(30)
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (line,) = service.lines_of(sheet.id)
        assert line.is_custodial is True
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(34)),)
            ),
        )
        receipt, issue = service.apply_differences(sheet.id, user_id=ACTOR_ID)
        assert issue is None
        assert receipt is not None
        (receipt_line,) = InventoryVoucherService(session).get(receipt.id)[2]
        assert receipt_line.is_custodial is True
        assert receipt_line.debit_account_id is None
        assert receipt_line.unit_cost_fc is None

        InventoryVoucherService(session).post(receipt.id, user_id=ACTOR_ID)
        assert financial_postings(session, receipt.id) == []
        assert len(movements_of(session, receipt.id)) == 1

    run(work)


def test_applying_twice_and_deleting_after_apply_are_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-once")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUN_1,
            quantity=Decimal(20),
            unit_cost=Decimal(1_000),
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (line,) = service.lines_of(sheet.id)
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(23)),)
            ),
        )
        service.apply_differences(sheet.id, user_id=ACTOR_ID)
        with pytest.raises(CountSheetAdjustmentError):
            service.apply_differences(sheet.id, user_id=ACTOR_ID)
        with pytest.raises(CountSheetAdjustmentError):
            service.delete(sheet.id)
        with pytest.raises(CountSheetAdjustmentError):
            service.set_counts(
                sheet.id,
                InventoryCountSheetCountsIn(
                    lines=(
                        InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(21)),
                    )
                ),
            )

    run(work)


def test_applying_without_any_difference_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-nodiff")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUN_1,
            quantity=Decimal(5),
            unit_cost=Decimal(1_000),
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (line,) = service.lines_of(sheet.id)
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(5)),)
            ),
        )
        with pytest.raises(CountSheetAdjustmentError):
            service.apply_differences(sheet.id, user_id=ACTOR_ID)

    run(work)


def test_surplus_without_a_cost_basis_names_the_item(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Khóa chỉ mới có phiếu XUẤT chưa tính giá: không có cơ sở giá nào để điền
    sẵn, và lượt duyệt phải nói mã hàng nào thay vì ghi một phiếu nhập giá 0."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-nocost")
        post_custodial_receipt(
            session, context, item_id=item_id, posting_date=JUN_1, quantity=Decimal(1)
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (custodial_line,) = service.lines_of(sheet.id)
        # Dựng một dòng THỪA của hàng của mình bằng cách đếm trên khóa chưa có giá:
        # ghi thêm một phiếu xuất (tồn âm, chưa tính giá) rồi lập lại biên bản.
        assert custodial_line.unit_cost is None
        service.delete(sheet.id)

        sheet2 = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (line,) = service.lines_of(sheet2.id)
        line.is_custodial = False
        line.unit_cost = None
        session.flush()
        service.set_counts(
            sheet2.id,
            InventoryCountSheetCountsIn(
                lines=(InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(4)),)
            ),
        )
        with pytest.raises(CountSheetAdjustmentError) as excinfo:
            service.apply_differences(sheet2.id, user_id=ACTOR_ID)
        assert excinfo.value.details["item_id"] == item_id

    run(work)


def test_creating_a_sheet_for_an_untouched_warehouse_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        with pytest.raises(CountSheetInvalidError):
            InventoryCountSheetService(session).create(
                InventoryCountSheetIn(
                    branch_id=context.branch_id,
                    warehouse_id=MAIN_WAREHOUSE_ID,
                    count_date=date(2020, 1, 31),
                ),
                user_id=ACTOR_ID,
            )

    run(work)


def test_adjustment_vouchers_post_and_cost_like_any_other(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Phiếu sinh từ biên bản đi đúng đường nghiệp vụ: ghi sổ được, và phiếu
    xuất thiếu nhận giá từ engine như mọi phiếu xuất khác."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-endtoend")
        post_receipt(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUN_1,
            quantity=Decimal(80),
            unit_cost=Decimal(2_500),
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (line,) = service.lines_of(sheet.id)
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(72)),)
            ),
        )
        _receipt, issue = service.apply_differences(sheet.id, user_id=ACTOR_ID)
        assert issue is not None
        InventoryVoucherService(session).post(
            issue.id, user_id=ACTOR_ID, acknowledged_warnings=True
        )
        run_engine(session, context)
        session.expire_all()
        (movement,) = movements_of(session, issue.id)
        assert movement.quantity == Decimal(8)
        assert movement.unit_cost == Decimal("2500.000000")
        assert movement.amount == Decimal("20000.00")
        postings = financial_postings(session, issue.id)
        assert len(postings) == 2
        assert {_account_code(session, posting.account_id) for posting in postings} == {
            "1381",
            "156",
        }
        header = session.get(Voucher, issue.id)
        assert header is not None
        assert header.status == VoucherStatus.DA_GHI_SO

    run(work)


def test_a_negative_book_quantity_is_snapshotted_as_is(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Tồn âm trên sổ (guard FR-STK-040 mặc định chỉ CẢNH BÁO nên khóa âm tới
    được đây) phải vào biên bản NGUYÊN con số ấy — nắn về 0 là để chênh lệch
    nói dối, còn cấm bằng CHECK là làm cả tính năng 500 trên đúng cái kho cần
    kiểm nhất (review 8D H-1)."""

    def work(session: Session) -> None:
        item_id = fresh_item(session, "count-negative")
        # Xuất mà chưa nhập: tồn −6.
        post_issue(
            session,
            context,
            accounts,
            item_id=item_id,
            posting_date=JUN_1,
            quantity=Decimal(6),
        )
        service = InventoryCountSheetService(session)
        sheet = service.create(
            InventoryCountSheetIn(
                branch_id=context.branch_id,
                warehouse_id=MAIN_WAREHOUSE_ID,
                count_date=JUN_30,
                item_ids=(item_id,),
            ),
            user_id=ACTOR_ID,
        )
        (line,) = service.lines_of(sheet.id)
        assert line.book_qty == Decimal(-6)
        service.set_counts(
            sheet.id,
            InventoryCountSheetCountsIn(
                lines=(InventoryCountSheetLineCount(line_no=line.line_no, counted_qty=Decimal(0)),)
            ),
        )
        (difference,) = service.differences(sheet.id)
        assert difference.difference == Decimal(6), "đếm 0 trên tồn −6 là THỪA 6"

    run(work)
