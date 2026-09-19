"""Ba cổng cảnh báo kho (FR-STK-040/041/042, ba mức FR-SYS-062) + guard "phiếu
sinh từ nguồn đứng yên khi nguồn còn ghi sổ".

**Hai guard số lượng soi CHỨNG TỪ NGUỒN, không chỉ phiếu kho.** Phiếu xuất sinh
từ hóa đơn bán được ghi sổ bên trong hook `after_post` của hóa đơn, nơi
`acknowledged_warnings` của người dùng không tới được; nên guard hỏi
`InventoryLineSource` "chứng từ này sẽ xuất gì?" và kêu ngay trên màn hóa đơn —
băng "Vẫn ghi sổ?" (7H-1) là chỗ trả lời. Phiếu sinh ra sau đó ghi sổ với cảnh
báo đã xác nhận (`bridge.py`). Cùng lập luận với `CashBalanceGuard`: soi mọi
chứng từ làm tồn giảm, không riêng phiếu của module này.

Tồn soi bằng `stock_floor_from` — thấp nhất từ ngày ghi sổ tới hết dữ liệu
(bài học 6B M-2: chứng từ lùi ngày trừ vào mọi ngày về sau).

**FR-STK-042** nhìn ở chiều ngược: chứng từ KHÔNG phải phiếu kho mà bút toán
chạm TK kho (nhận diện qua **mục đích** `inventory_goods`/`raw_materials`/
`tools_supplies`/`finished_goods` của gói — không tiền tố số hiệu, doctrine
7G-3) và không sinh phiếu nào → sổ cái có giá trị mà sổ kho không có số lượng.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_provider import accounts_by_id, default_account, resolve_package
from ket.kernel.config.catalog import (
    INVENTORY_ACCOUNT_NO_MOVEMENT_WARNING_KEY,
    STOCK_BELOW_MIN_WARNING_KEY,
    STOCK_NEGATIVE_WARNING_KEY,
    WARNING_LEVEL_BLOCK,
    WARNING_LEVEL_NONE,
)
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    DefaultAccountNotConfiguredError,
    InventoryLayerReferencedError,
    InventoryVoucherDerivedError,
    PostingViolation,
)
from ket.kernel.master_data.models.item import Item
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.protocols import PROVIDERS, InventoryMovementKind, PlannedMovement
from ket.modules.inventory.models import (
    INVENTORY_DOCUMENT_TYPES,
    InventoryMovement,
    InventoryVoucher,
    InventoryVoucherKind,
    InventoryVoucherLine,
    Lot,
)
from ket.modules.inventory.movements import lot_key_of
from ket.modules.inventory.stock import stock_floor_from
from ket.posting.contracts import GuardFinding, Voucher, VoucherStatus
from ket.posting.engine.prepared import PreparedLine

STOCK_NEGATIVE_CODE = "inventory.stock_negative"
STOCK_BELOW_MIN_CODE = "inventory.stock_below_min"
INVENTORY_ACCOUNT_NO_MOVEMENT_CODE = "inventory.account_without_movement"

INVENTORY_ACCOUNT_PURPOSES: tuple[str, ...] = (
    "inventory_goods",
    "raw_materials",
    "tools_supplies",
    "finished_goods",
)
"""Bốn mục đích TK kho của gói cấu hình — BR-STK-03 nói về đúng nhóm này
(152/153/155/156 ở TT99)."""

_ZERO = Decimal(0)
_ONE = Decimal(1)

StockKey = tuple[int, int, int]
"""`(warehouse_id, item_id, lot_key)` — chi nhánh là của chứng từ."""


def planned_movement_of(session: Session, voucher: Voucher) -> PlannedMovement | None:
    """Phiếu kho mà một chứng từ sẽ sinh — của chính module này thì đọc dòng
    phiếu, của module khác thì hỏi các `InventoryLineSource` đã đăng ký."""
    if voucher.document_type in INVENTORY_DOCUMENT_TYPES:
        return None
    for source in PROVIDERS.inventory_line_sources():
        planned = source.planned_movement(session, voucher.id)
        if planned is not None:
            return planned
    return None


_OUTGOING_CACHE_KEY = "inventory.outgoing_quantities"


def outgoing_quantities(session: Session, voucher: Voucher) -> dict[StockKey, Decimal]:
    """Số lượng (đơn vị chính) rời từng khóa kho khi chứng từ này ghi sổ.

    Nhớ theo `(voucher.id)` trong `session.info`: hai guard cùng hỏi một câu
    trong một lượt ghi sổ, và nguồn dòng (`InventoryLineSource`) đọc thân hóa
    đơn mỗi lần hỏi (review 8A L-6). Khóa nằm trong phiên nên chết cùng phiên.
    """
    cache: dict[UUID, dict[StockKey, Decimal]] = session.info.setdefault(_OUTGOING_CACHE_KEY, {})
    cached = cache.get(voucher.id)
    if cached is not None:
        return cached
    computed = _outgoing_quantities(session, voucher)
    cache[voucher.id] = computed
    return computed


def _outgoing_quantities(session: Session, voucher: Voucher) -> dict[StockKey, Decimal]:
    if voucher.document_type in INVENTORY_DOCUMENT_TYPES:
        return _outgoing_of_inventory_voucher(session, voucher)
    planned = planned_movement_of(session, voucher)
    if planned is None or planned.kind != InventoryMovementKind.ISSUE:
        return {}
    quantities: dict[StockKey, Decimal] = {}
    factors = _factors_for(session, sorted({line.item_id for line in planned.lines}))
    for line in planned.lines:
        factor = factors.get((line.item_id, line.unit_id), _ONE)
        key = (
            line.warehouse_id,
            line.item_id,
            lot_key_of(_lot_id(session, line.item_id, line.lot_no)),
        )
        quantities[key] = quantities.get(key, _ZERO) + line.quantity * factor
    return quantities


class StockNegativeGuard:
    """FR-STK-040 — xuất quá số lượng tồn theo (kho, vật tư, lô)."""

    def check(
        self, session: Session, *, voucher: Voucher, lines: Sequence[PreparedLine]
    ) -> Sequence[GuardFinding]:
        level = value_of(session, key=STOCK_NEGATIVE_WARNING_KEY, user_id=voucher.created_by)
        if level == WARNING_LEVEL_NONE:
            return ()
        outgoing = outgoing_quantities(session, voucher)
        if not outgoing:
            return ()
        blocking = level == WARNING_LEVEL_BLOCK
        findings: list[GuardFinding] = []
        for (warehouse_id, item_id, lot_key), quantity in sorted(outgoing.items()):
            floor = stock_floor_from(
                session,
                branch_id=voucher.branch_id,
                warehouse_id=warehouse_id,
                item_id=item_id,
                lot_key=lot_key,
                from_date=voucher.posting_date,
            )
            projected = floor - quantity
            if projected >= 0:
                continue
            findings.append(
                GuardFinding(
                    violation=PostingViolation(
                        STOCK_NEGATIVE_CODE,
                        f"Ghi sổ xong thì tồn kho âm {-projected:f} — xuất quá số tồn (FR-STK-040)",
                        warehouse_id=warehouse_id,
                        item_id=item_id,
                        lot_key=lot_key,
                        projected=str(projected),
                    ),
                    blocking=blocking,
                )
            )
        return findings


class StockBelowMinGuard:
    """FR-STK-041 — tồn sau ghi sổ rơi dưới `items.min_stock_qty` (FR-SYS-047)."""

    def check(
        self, session: Session, *, voucher: Voucher, lines: Sequence[PreparedLine]
    ) -> Sequence[GuardFinding]:
        level = value_of(session, key=STOCK_BELOW_MIN_WARNING_KEY, user_id=voucher.created_by)
        if level == WARNING_LEVEL_NONE:
            return ()
        outgoing = outgoing_quantities(session, voucher)
        if not outgoing:
            return ()
        thresholds = {
            item.id: item.min_stock_qty
            for item in session.execute(
                select(Item).where(
                    Item.id.in_(sorted({key[1] for key in outgoing})),
                    Item.min_stock_qty.is_not(None),
                )
            )
            .scalars()
            .all()
        }
        if not thresholds:
            return ()
        # Ngưỡng khai theo mã hàng, tồn soi theo (kho, mã hàng) gộp mọi lô:
        # "tồn tối thiểu" là tồn của mã hàng ở kho, không phải của một lô.
        per_item_warehouse: dict[tuple[int, int], Decimal] = {}
        for (warehouse_id, item_id, _lot_key), quantity in outgoing.items():
            if item_id in thresholds:
                key = (warehouse_id, item_id)
                per_item_warehouse[key] = per_item_warehouse.get(key, _ZERO) + quantity
        blocking = level == WARNING_LEVEL_BLOCK
        findings: list[GuardFinding] = []
        for (warehouse_id, item_id), quantity in sorted(per_item_warehouse.items()):
            floor = _floor_all_lots(session, voucher, warehouse_id, item_id)
            threshold = thresholds[item_id]
            projected = floor - quantity
            if threshold is None or projected >= threshold:
                continue
            findings.append(
                GuardFinding(
                    violation=PostingViolation(
                        STOCK_BELOW_MIN_CODE,
                        f"Ghi sổ xong thì tồn còn {projected:f}, dưới mức tối thiểu "
                        f"{threshold:f} (FR-STK-041)",
                        warehouse_id=warehouse_id,
                        item_id=item_id,
                        projected=str(projected),
                        min_stock_qty=str(threshold),
                    ),
                    blocking=blocking,
                )
            )
        return findings


class InventoryAccountWithoutMovementGuard:
    """FR-STK-042 — hạch toán vào TK kho nhưng không ghi sổ kho."""

    def check(
        self, session: Session, *, voucher: Voucher, lines: Sequence[PreparedLine]
    ) -> Sequence[GuardFinding]:
        if voucher.document_type in INVENTORY_DOCUMENT_TYPES or not lines:
            return ()
        level = value_of(
            session, key=INVENTORY_ACCOUNT_NO_MOVEMENT_WARNING_KEY, user_id=voucher.created_by
        )
        if level == WARNING_LEVEL_NONE:
            return ()
        touched = self._inventory_accounts_touched(session, voucher, lines)
        if not touched:
            return ()
        planned = planned_movement_of(session, voucher)
        if planned is not None and planned.lines:
            return ()
        return (
            GuardFinding(
                violation=PostingViolation(
                    INVENTORY_ACCOUNT_NO_MOVEMENT_CODE,
                    f"Chứng từ hạch toán vào TK kho ({', '.join(sorted(touched))}) nhưng "
                    "không sinh phiếu nhập/xuất kho (FR-STK-042)",
                    account_codes=",".join(sorted(touched)),
                ),
                blocking=level == WARNING_LEVEL_BLOCK,
            ),
        )

    @staticmethod
    def _inventory_accounts_touched(
        session: Session, voucher: Voucher, lines: Sequence[PreparedLine]
    ) -> set[str]:
        year = fiscal_year_covering(session, voucher.posting_date)
        if year is None:
            return set()
        package = resolve_package(
            session, scheme=year.accounting_scheme, on_date=voucher.posting_date
        )
        purpose_codes: list[str] = []
        for purpose in INVENTORY_ACCOUNT_PURPOSES:
            try:
                purpose_codes.append(
                    default_account(
                        session, package_id=package.id, document_type="*", purpose=purpose
                    ).account_code
                )
            except DefaultAccountNotConfiguredError:
                # Gói chưa khai mục đích này (gói test tối giản) — không có TK
                # nào để nhận diện, không phải lỗi.
                continue
        if not purpose_codes:
            return set()
        accounts = accounts_by_id(session, [line.source.account_id for line in lines])
        return {
            account.code
            for account in accounts.values()
            if any(account.code == code or account.code.startswith(code) for code in purpose_codes)
        }


def refuse_when_source_posted(session: Session, voucher_id: UUID) -> None:
    """Phiếu sinh từ chứng từ nguồn không sửa/xóa/bỏ ghi sổ độc lập chừng nào
    nguồn còn ghi sổ — đăng ký cả `EDIT_GUARDS` lẫn `REFERENCE_GUARDS`.

    Không trạng thái, không cờ "đang gỡ": khi nguồn bỏ ghi sổ, `PostingService.
    unpost` đổi trạng thái nguồn TRƯỚC rồi hook mới gọi `remove_movement`, nên
    phép kiểm "nguồn còn ghi sổ?" tự cho qua đúng lúc cần.
    """
    voucher = session.get(Voucher, voucher_id)
    if (
        voucher is None
        or voucher.document_type not in INVENTORY_DOCUMENT_TYPES
        or voucher.source_document_id is None
    ):
        return
    source = session.get(Voucher, voucher.source_document_id)
    if source is None or source.status != VoucherStatus.DA_GHI_SO:
        return
    raise InventoryVoucherDerivedError(
        "Phiếu kho này sinh từ một chứng từ đã ghi sổ nên không sửa, xóa hay bỏ ghi "
        "sổ riêng được — bỏ ghi sổ chứng từ nguồn thì phiếu tự gỡ theo",
        voucher_no=voucher.voucher_no,
        source_voucher_no=source.voucher_no,
        source_voucher_id=str(source.id),
    )


def refuse_when_named_as_specific_source(session: Session, voucher_id: UUID) -> None:
    """Phiếu nhập có movement được dòng xuất đích danh trỏ tới không bỏ ghi sổ
    được (lát 8B) — đăng ký `REFERENCE_GUARDS`. Khóa ngoại `RESTRICT` trên
    `inventory_voucher_lines.source_movement_id` là hàng rào cuối; guard này
    nói được phải sửa phiếu nào."""
    referencing = (
        session.execute(
            select(Voucher.voucher_no)
            .join(InventoryVoucherLine, InventoryVoucherLine.voucher_id == Voucher.id)
            .join(
                InventoryMovement, InventoryMovement.id == InventoryVoucherLine.source_movement_id
            )
            .where(InventoryMovement.voucher_id == voucher_id)
            .distinct()
            .order_by(Voucher.voucher_no)
        )
        .scalars()
        .all()
    )
    if not referencing:
        return
    raise InventoryLayerReferencedError(
        "Lần nhập này đã được phiếu xuất đích danh chỉ tới — sửa hoặc bỏ ghi sổ các "
        "phiếu xuất đó trước",
        referenced_by=",".join(referencing[:10]),
        count=len(referencing),
    )


# ------------------------------------------------------------------ nội bộ


def _outgoing_of_inventory_voucher(session: Session, voucher: Voucher) -> dict[StockKey, Decimal]:
    body = session.get(InventoryVoucher, voucher.id)
    if body is None or body.kind == InventoryVoucherKind.RECEIPT:
        return {}
    quantities: dict[StockKey, Decimal] = {}
    for line in session.execute(
        select(InventoryVoucherLine).where(InventoryVoucherLine.voucher_id == voucher.id)
    ).scalars():
        warehouse_id = (
            body.warehouse_id if body.kind == InventoryVoucherKind.TRANSFER else line.warehouse_id
        )
        key = (warehouse_id, line.item_id, lot_key_of(line.lot_id))
        quantities[key] = quantities.get(key, _ZERO) + line.base_quantity
    return quantities


def _factors_for(session: Session, item_ids: list[int]) -> dict[tuple[int, int], Decimal]:
    """Tỷ lệ quy đổi `(item_id, unit_id) → factor`; đơn vị chính trả `1`."""
    factors: dict[tuple[int, int], Decimal] = {}
    for item in session.execute(select(Item).where(Item.id.in_(item_ids))).scalars():
        if item.base_unit_id is not None:
            factors[(item.id, item.base_unit_id)] = _ONE
    for row in session.execute(select(ItemUnit).where(ItemUnit.item_id.in_(item_ids))).scalars():
        factors[(row.item_id, row.unit_id)] = row.factor
    return factors


def _lot_id(session: Session, item_id: int, lot_no: str | None) -> int | None:
    if lot_no is None or not lot_no.strip():
        return None
    return session.scalar(
        select(Lot.id).where(Lot.item_id == item_id, Lot.lot_no == lot_no.strip())
    )


def _floor_all_lots(session: Session, voucher: Voucher, warehouse_id: int, item_id: int) -> Decimal:
    """Tồn thấp nhất của mã hàng ở kho, gộp mọi lô — một câu cửa sổ (M-5)."""
    return stock_floor_from(
        session,
        branch_id=voucher.branch_id,
        warehouse_id=warehouse_id,
        item_id=item_id,
        lot_key=None,
        from_date=voucher.posting_date,
    )
