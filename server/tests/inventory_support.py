"""Khung dựng bối cảnh cho test phân hệ Kho (lát 8A).

Bồi lên `posting_support.seed_posting_context` cùng lối `purchase_support`:
gói `TT99-TEST` thắng `resolve_package` trong dataset test, nên TK kho (152/155/
156), TK đối ứng (154/621/632) và nghiệp vụ NK/XK/CK (FR-SYS-025) phải được
gieo vào CHÍNH gói đó — idempotent theo khóa tự nhiên vì nhiều tệp test dùng
chung dataset (kể cả giẫm lên phần `purchase_support`/`sales_support` gieo).

Danh mục dựng ở đây: hai kho, một mã hàng `goods` với đơn vị chính "cái" và đơn
vị quy đổi "thùng = 12 cái" (FR-STK-006), một thành phẩm, một dịch vụ (để kiểm
"không qua kho").
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, DefaultAccount
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.item import Item, ItemNature
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.models.warehouse import Warehouse
from ket.kernel.periods.models import FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.models import Setting
from ket.modules.inventory.costing.engine import CostingRunResult, recalc_branch
from ket.modules.inventory.models import (
    InventoryMovement,
    InventoryRecalcMark,
    InventoryVoucherKind,
    MovementDirection,
)
from ket.modules.inventory.schemas import InventoryVoucherIn, InventoryVoucherLineIn
from ket.modules.inventory.service import InventoryVoucherService
from ket.posting.engine.models import GlPosting, Ledger
from posting_support import PostingContext, posting_scope

SEED_ACTOR_ID = 1

# `id` cố định đặt CAO (9xx.xxx): chèn `id` tường minh không nhích sequence, và
# bài nhập 10.000 kho (`test_import_pipeline`) cấp `nextval` từ chỗ sequence đang
# đứng — đặt ở 8101 thì thứ tự tệp quyết định có đụng `pk_warehouses` hay không
# (lát 8B gặp khi `test_costing_*` chạy trước bài nhập). Vai trò app không
# `setval` được nên dời id, không dời sequence.
MAIN_WAREHOUSE_ID = 908101
SECOND_WAREHOUSE_ID = 908102
UNIT_PIECE_ID = 908111
UNIT_BOX_ID = 908112
BOX_FACTOR = Decimal(12)
GOODS_ITEM_ID = 908121
FINISHED_ITEM_ID = 908122
SERVICE_ITEM_ID = 908123

_EXTRA_ACCOUNT_SPECS: tuple[tuple[str, str, int, list[str] | None], ...] = (
    # (code, name, balance_nature, detail_tracking)
    ("152", "Nguyên liệu, vật liệu", BalanceNature.DEBIT, ["item", "warehouse"]),
    ("154", "Chi phí sản xuất, kinh doanh dở dang", BalanceNature.DEBIT, None),
    ("155", "Thành phẩm", BalanceNature.DEBIT, ["item", "warehouse"]),
    # KHÔNG theo dõi chiều trên 156/157: `purchase_support` gieo cùng TK không
    # theo dõi và người gieo trước thắng (idempotent theo mã) — tệp test chạy
    # trước làm bài mua hàng chạy sau đổ `dimension.missing` (lát 8B đã gặp).
    # Chiều item/warehouse của dòng kho vẫn được mapper gắn, không cần TK đòi.
    ("156", "Hàng hóa", BalanceNature.DEBIT, None),
    ("157", "Hàng gửi đi bán", BalanceNature.DEBIT, None),
    ("621", "Chi phí nguyên liệu, vật liệu trực tiếp", BalanceNature.NONE, None),
    ("632", "Giá vốn hàng bán", BalanceNature.NONE, None),
    ("1331", "Thuế GTGT được khấu trừ của hàng hóa, dịch vụ", BalanceNature.DEBIT, None),
    # TK hàng hóa KHÔNG theo dõi kho — để kiểm "hóa đơn mua không kho thì không
    # sinh phiếu" mà validator chiều phân tích không chặn trước.
    ("1568", "Hàng hóa khác (không theo kho)", BalanceNature.DEBIT, None),
)

_DEFAULT_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("inventory_goods", "156"),
    ("raw_materials", "152"),
    ("finished_goods", "155"),
    ("work_in_progress", "154"),
    ("production_materials", "621"),
    ("cogs", "632"),
)

_RULES: tuple[tuple[str, str, str, str | None, str | None, bool, int | None, int], ...] = (
    # (document_type, code, name, debit_purpose, credit_purpose, requires_partner, partner_kind, order)
    (
        "NK",
        "nhap-thanh-pham",
        "Nhập kho thành phẩm",
        "finished_goods",
        "work_in_progress",
        False,
        None,
        1,
    ),
    ("NK", "nhap-mua-hang", "Nhập kho hàng mua", None, None, True, 1, 2),
    (
        "NK",
        "nhap-hang-ban-tra-lai",
        "Nhập kho hàng bán bị trả lại",
        "inventory_goods",
        "cogs",
        True,
        0,
        3,
    ),
    ("NK", "nhap-khac", "Nhập kho khác", None, None, False, None, 9),
    (
        "XK",
        "xuat-nvl-san-xuat",
        "Xuất kho NVL cho sản xuất",
        "production_materials",
        "raw_materials",
        False,
        None,
        1,
    ),
    ("XK", "xuat-ban-hang", "Xuất kho bán hàng", "cogs", "inventory_goods", True, 0, 2),
    ("XK", "xuat-tra-lai-hang-mua", "Xuất kho trả lại hàng mua", None, None, True, 1, 3),
    ("XK", "xuat-khac", "Xuất kho khác", None, None, False, None, 9),
    ("CK", "chuyen-kho-noi-bo", "Chuyển kho nội bộ", None, None, False, None, 1),
)


def seed_inventory_package_data(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    """Gieo TK + purpose + nghiệp vụ kho vào gói test; trả bảng số hiệu → id."""
    accounts = dict(context.accounts)
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        for code, name, nature, tracking in _EXTRA_ACCOUNT_SPECS:
            row = session.scalar(
                select(ChartOfAccount).where(
                    ChartOfAccount.package_id == context.package_id, ChartOfAccount.code == code
                )
            )
            if row is None:
                row = ChartOfAccount(
                    package_id=context.package_id,
                    code=code,
                    name=name,
                    path="0.",
                    balance_nature=nature,
                    is_summary=False,
                    detail_tracking=tracking,
                )
                session.add(row)
                session.flush()
                row.path = f"{row.id}."
                session.flush()
            accounts[code] = row.id
        for purpose, account_code in _DEFAULT_ACCOUNTS:
            if session.get(DefaultAccount, (context.package_id, "*", purpose)) is None:
                session.add(
                    DefaultAccount(
                        package_id=context.package_id,
                        document_type="*",
                        purpose=purpose,
                        account_code=account_code,
                    )
                )
        for doc_type, code, name, debit, credit, requires, partner_kind, order in _RULES:
            existing = session.scalar(
                select(AutoPostingRule.id).where(
                    AutoPostingRule.package_id == context.package_id,
                    AutoPostingRule.document_type == doc_type,
                    AutoPostingRule.operation_code == code,
                )
            )
            if existing is None:
                session.add(
                    AutoPostingRule(
                        package_id=context.package_id,
                        document_type=doc_type,
                        operation_code=code,
                        operation_name=name,
                        debit_purpose=debit,
                        credit_purpose=credit,
                        requires_partner=requires,
                        partner_kind=partner_kind,
                        display_order=order,
                    )
                )
        session.flush()
        ensure_inventory_catalog(session)
    return accounts


def ensure_inventory_catalog(session: Session) -> None:
    """Hai kho, hai đơn vị, ba mã hàng — `id` cố định để nhiều tệp dùng chung."""
    for warehouse_id, code in ((MAIN_WAREHOUSE_ID, "KHO-8A-1"), (SECOND_WAREHOUSE_ID, "KHO-8A-2")):
        if session.get(Warehouse, warehouse_id) is None:
            session.add(
                Warehouse(id=warehouse_id, code=code, name=f"Kho {code}", path=f"{warehouse_id}.")
            )
    for unit_id, code in ((UNIT_PIECE_ID, "CAI-8A"), (UNIT_BOX_ID, "THUNG-8A")):
        if session.get(UnitOfMeasure, unit_id) is None:
            session.add(UnitOfMeasure(id=unit_id, code=code, name=code, path=f"{unit_id}."))
    session.flush()
    for item_id, code, nature in (
        (GOODS_ITEM_ID, "HH-8A-01", ItemNature.GOODS),
        (FINISHED_ITEM_ID, "TP-8A-01", ItemNature.FINISHED_GOODS),
        (SERVICE_ITEM_ID, "DV-8A-01", ItemNature.SERVICE),
    ):
        if session.get(Item, item_id) is None:
            session.add(
                Item(
                    id=item_id,
                    code=code,
                    name=f"Vật tư {code}",
                    path=f"{item_id}.",
                    nature=nature,
                    base_unit_id=UNIT_PIECE_ID if nature is not ItemNature.SERVICE else None,
                )
            )
    session.flush()
    if (
        session.scalar(
            select(ItemUnit.id).where(
                ItemUnit.item_id == GOODS_ITEM_ID, ItemUnit.unit_id == UNIT_BOX_ID
            )
        )
        is None
    ):
        session.add(ItemUnit(item_id=GOODS_ITEM_ID, unit_id=UNIT_BOX_ID, factor=BOX_FACTOR))
        session.flush()


def set_system_setting(session: Session, key: str, value: str, value_type: str = "string") -> None:
    """Upsert thẳng dòng `settings` — cùng lối `test_cash_balance_guard`."""
    row = session.scalar(select(Setting).where(Setting.key == key, Setting.scope == "system"))
    if row is None:
        session.add(Setting(scope="system", key=key, value=value, value_type=value_type))
    else:
        row.value = value
    session.flush()


def receipt_payload(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    quantity: Decimal = Decimal(100),
    unit_cost: Decimal = Decimal(20_000),
    item_id: int = GOODS_ITEM_ID,
    unit_id: int = UNIT_PIECE_ID,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
    operation: str = "nhap-thanh-pham",
    lot_no: str | None = None,
    with_accounts: bool = True,
) -> InventoryVoucherIn:
    """Phiếu nhập gõ tay: mặc định Nợ 155 / Có 154 (nhập thành phẩm)."""
    return InventoryVoucherIn(
        kind=InventoryVoucherKind.RECEIPT,
        operation_code=operation,
        warehouse_id=warehouse_id,
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="nhập kho test",
        lines=(
            InventoryVoucherLineIn(
                item_id=item_id,
                unit_id=unit_id,
                quantity=quantity,
                unit_cost_fc=unit_cost,
                lot_no=lot_no,
                debit_account_id=accounts["155"] if with_accounts else None,
                credit_account_id=accounts["154"] if with_accounts else None,
            ),
        ),
    )


def issue_payload(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    quantity: Decimal = Decimal(30),
    item_id: int = GOODS_ITEM_ID,
    unit_id: int = UNIT_PIECE_ID,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
    lot_no: str | None = None,
) -> InventoryVoucherIn:
    """Phiếu xuất gõ tay: Nợ 621 / Có 152, giá chờ engine."""
    return InventoryVoucherIn(
        kind=InventoryVoucherKind.ISSUE,
        operation_code="xuat-nvl-san-xuat",
        warehouse_id=warehouse_id,
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="xuất kho test",
        lines=(
            InventoryVoucherLineIn(
                item_id=item_id,
                unit_id=unit_id,
                quantity=quantity,
                lot_no=lot_no,
                debit_account_id=accounts["621"],
                credit_account_id=accounts["152"],
            ),
        ),
    )


def transfer_payload(
    context: PostingContext,
    *,
    posting_date: date,
    quantity: Decimal = Decimal(10),
    item_id: int = GOODS_ITEM_ID,
) -> InventoryVoucherIn:
    return InventoryVoucherIn(
        kind=InventoryVoucherKind.TRANSFER,
        operation_code="chuyen-kho-noi-bo",
        warehouse_id=MAIN_WAREHOUSE_ID,
        to_warehouse_id=SECOND_WAREHOUSE_ID,
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="chuyển kho test",
        lines=(InventoryVoucherLineIn(item_id=item_id, unit_id=UNIT_PIECE_ID, quantity=quantity),),
    )


def movements_of(session: Session, voucher_id: UUID) -> list[InventoryMovement]:
    return list(
        session.execute(
            select(InventoryMovement)
            .where(InventoryMovement.voucher_id == voucher_id)
            .order_by(InventoryMovement.warehouse_id, InventoryMovement.direction)
        )
        .scalars()
        .all()
    )


def create_goods_item(session: Session, code: str) -> int:
    """Một mã hàng `goods` MỚI (đơn vị chính "cái") — khóa tồn kho sạch cho các
    bài guard: dataset dùng chung cả phiên nên tồn của `GOODS_ITEM_ID` là tổng
    của mọi tệp đã chạy trước, và một bài "tồn âm" trên nó sẽ đúng hay sai tùy
    thứ tự tệp."""
    item = Item(
        code=code,
        name=f"Vật tư {code}",
        path="0.",
        nature=ItemNature.GOODS,
        base_unit_id=UNIT_PIECE_ID,
    )
    session.add(item)
    session.flush()
    item.path = f"{item.id}."
    session.flush()
    return item.id


# ------------------------------------------------------------ lát 8B: engine


def fresh_item(session: Session, tag: str) -> int:
    """Khóa tồn kho sạch cho bài tính giá — xem `create_goods_item`."""
    return create_goods_item(session, f"HH-8B-{tag}-{uuid4().hex[:6].upper()}")


def post_receipt(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    posting_date: date,
    quantity: Decimal,
    unit_cost: Decimal,
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
            item_id=item_id,
            warehouse_id=warehouse_id,
            lot_no=lot_no,
        ),
        user_id=SEED_ACTOR_ID,
    )
    service.post(voucher.id, user_id=SEED_ACTOR_ID)
    return voucher.id


def post_issue(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    item_id: int,
    posting_date: date,
    quantity: Decimal,
    warehouse_id: int = MAIN_WAREHOUSE_ID,
    lot_no: str | None = None,
    source_movement_id: int | None = None,
) -> UUID:
    payload = issue_payload(
        context,
        accounts,
        posting_date=posting_date,
        quantity=quantity,
        item_id=item_id,
        warehouse_id=warehouse_id,
        lot_no=lot_no,
    )
    if source_movement_id is not None:
        payload = payload.model_copy(
            update={
                "lines": tuple(
                    line.model_copy(update={"source_movement_id": source_movement_id})
                    for line in payload.lines
                )
            }
        )
    service = InventoryVoucherService(session)
    voucher = service.create(payload, user_id=SEED_ACTOR_ID)
    # Guard tồn kho tắt trong bài tính giá: chính engine định nghĩa luật vượt tồn.
    service.post(voucher.id, user_id=SEED_ACTOR_ID, acknowledged_warnings=True)
    return voucher.id


def post_transfer(
    session: Session,
    context: PostingContext,
    *,
    item_id: int,
    posting_date: date,
    quantity: Decimal,
    from_warehouse_id: int = MAIN_WAREHOUSE_ID,
    to_warehouse_id: int = SECOND_WAREHOUSE_ID,
    accounts: tuple[int, int] | None = None,
) -> UUID:
    """Phiếu chuyển kho; `accounts` = cặp (Nợ, Có) cho chuyển gửi bán đại lý."""
    payload = transfer_payload(
        context, posting_date=posting_date, quantity=quantity, item_id=item_id
    ).model_copy(update={"warehouse_id": from_warehouse_id, "to_warehouse_id": to_warehouse_id})
    if accounts is not None:
        payload = payload.model_copy(
            update={
                "lines": tuple(
                    line.model_copy(
                        update={"debit_account_id": accounts[0], "credit_account_id": accounts[1]}
                    )
                    for line in payload.lines
                )
            }
        )
    service = InventoryVoucherService(session)
    voucher = service.create(payload, user_id=SEED_ACTOR_ID)
    service.post(voucher.id, user_id=SEED_ACTOR_ID, acknowledged_warnings=True)
    return voucher.id


def set_valuation_method(session: Session, context: PostingContext, method: str) -> None:
    """Đổi phương pháp tính giá của năm test — bài đổi phải trả lại
    `wavg_moving` trong `finally` vì dataset dùng chung cả phiên."""
    year = session.get(FiscalYear, context.fiscal_year_id)
    assert year is not None
    year.inventory_valuation_method = method
    session.flush()


def run_engine(
    session: Session,
    context: PostingContext,
    *,
    force_from: date | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> CostingRunResult:
    """Chạy engine thẳng trong phiên test (không qua worker) — cùng transaction
    với dữ liệu vừa dựng, kết quả nhìn được ngay."""
    return recalc_branch(
        session,
        branch_id=context.branch_id,
        user_id=SEED_ACTOR_ID,
        force_from=force_from,
        cancel_requested=cancel_requested,
    )


def financial_postings(session: Session, voucher_id: UUID) -> list[GlPosting]:
    return list(
        session.execute(
            select(GlPosting)
            .where(GlPosting.voucher_id == voucher_id, GlPosting.ledger == Ledger.FINANCIAL)
            .order_by(GlPosting.line_no)
        )
        .scalars()
        .all()
    )


def out_movement(session: Session, voucher_id: UUID) -> InventoryMovement:
    """Movement xuất duy nhất của một phiếu xuất một dòng."""
    rows = [m for m in movements_of(session, voucher_id) if m.direction == MovementDirection.OUT]
    assert len(rows) == 1, rows
    return rows[0]


def in_movement(session: Session, voucher_id: UUID) -> InventoryMovement:
    rows = [m for m in movements_of(session, voucher_id) if m.direction == MovementDirection.IN]
    assert len(rows) == 1, rows
    return rows[0]


def marks_of_branch(session: Session, branch_id: int) -> list[InventoryRecalcMark]:
    return list(
        session.execute(
            select(InventoryRecalcMark).where(InventoryRecalcMark.branch_id == branch_id)
        )
        .scalars()
        .all()
    )
