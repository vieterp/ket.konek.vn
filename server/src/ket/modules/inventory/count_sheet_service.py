"""Kiểm kê kho (FR-STK-030/031, U8, lát 8D) — biên bản + phiếu xử lý chênh lệch.

Luồng U8: lập biên bản (chụp tồn sổ sách tại ngày kiểm kê) → nhập số đếm →
hệ thống hiện **chỉ** dòng lệch → duyệt thì **tự** sinh phiếu nhập (thừa) /
phiếu xuất (thiếu) đã định khoản sẵn. Người dùng không phải tự lập chứng từ xử
lý chênh lệch — đó là cả điểm của U8.

**Cặp TK đọc thẳng từ `purpose`, không từ một nghiệp vụ riêng** (quyết định user
2026-09-23). Khuôn kiểm kê quỹ 6E dùng hai nghiệp vụ có tên
(`thua-quy-kiem-ke`/`thieu-quy-kiem-ke`), nhưng khuôn ấy không bê sang kho được:
`packages.seed._ensure_auto_posting_backfilled` vá nghiệp vụ **theo từng
`document_type`**, và NK/XK đã có nghiệp vụ từ 8A — thêm hai dòng vào
`auto_posting_rules.csv` sẽ không bao giờ tới được dataset đã gieo trước lát
này (khác ca 8C-2: LR/TD là `document_type` **mới** nên backfill chạy). Hai
purpose cần dùng thì khai ở `document_type = '*'` từ phase 6 nên có ở mọi
dataset:

* thừa → Nợ TK kho / Có `unexplained_surplus` (3381);
* thiếu → Nợ `unexplained_shortage` (1381) / Có TK kho.

TK kho suy từ **tính chất mã hàng** (`goods` → `inventory_goods`,
`finished_goods` → `finished_goods`): mã hàng không có cột TK kho riêng, và
tính chất là thứ quyết định 156 hay 155 (xem `ItemNature`).

**Hàng giữ hộ đếm chung** (BR-STK-07, quyết định user 2026-09-23): một khóa vừa
có hàng của mình vừa có hàng nhận giữ hộ ra **hai dòng** biên bản — thủ kho đếm
một lượt trên kệ thật. Chênh lệch của hàng giữ hộ đi vào cùng phiếu NK/XK nhưng
là dòng `is_custodial`: không cặp TK, không giá, nên phiếu chỉ điều chỉnh số
lượng và ghi 0 dòng sổ cái nếu toàn bộ chênh lệch là hàng giữ hộ (ADR-024).

Phiếu sinh ra là phiếu **nháp** đi qua chính `InventoryVoucherService.create` —
cùng cấp số, cùng bộ kiểm, cùng guard tồn âm; biên bản giữ id của chúng nên
lượt duyệt thứ hai bị từ chối thay vì sinh phiếu thứ hai.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.accounts_provider import default_account, resolve_package
from ket.kernel.errors import (
    CountSheetAdjustmentError,
    CountSheetInvalidError,
    CountSheetNotFoundError,
)
from ket.kernel.identifiers import uuid7
from ket.kernel.master_data.models.item import INVENTORY_NATURES, Item, ItemNature
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule, NumberingService
from ket.kernel.periods.service import fiscal_year_covering
from ket.modules.inventory.models import (
    COUNT_SHEET_DOCUMENT_TYPE,
    InventoryCountSheet,
    InventoryCountSheetLine,
    InventoryMovement,
    InventoryVoucherKind,
)
from ket.modules.inventory.movements import lot_key_of
from ket.modules.inventory.schemas import (
    InventoryCountSheetCountsIn,
    InventoryCountSheetDifference,
    InventoryCountSheetIn,
    InventoryVoucherIn,
    InventoryVoucherLineIn,
)
from ket.modules.inventory.stock import average_unit_cost_at
from ket.posting.contracts import Voucher

_ZERO = Decimal(0)

SURPLUS_PURPOSE = "unexplained_surplus"
"""TK 3381 — tài sản thừa chờ giải quyết."""
DEFICIT_PURPOSE = "unexplained_shortage"
"""TK 1381 — tài sản thiếu chờ xử lý."""

STOCK_PURPOSE_BY_NATURE = {
    ItemNature.GOODS: "inventory_goods",
    ItemNature.FINISHED_GOODS: "finished_goods",
}
"""TK kho theo tính chất mã hàng — hai tính chất có tồn kho, hai tài khoản."""

SURPLUS_OPERATION = "nhap-khac"
DEFICIT_OPERATION = "xuat-khac"
"""Nghiệp vụ "tự định khoản" của NK/XK — có ở MỌI dataset từ 8A; cặp TK do
service này điền, không do gói khai."""

COUNT_SHEET_NUMBERING = NumberingRule(
    document_type=COUNT_SHEET_DOCUMENT_TYPE, prefix="KK{YY}-", reset_rule=ResetRule.YEARLY
)


class InventoryCountSheetService:
    """Biên bản kiểm kê trong transaction người gọi — cùng khuôn các service khác."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ lập

    def create(self, payload: InventoryCountSheetIn, *, user_id: int) -> InventoryCountSheet:
        """Lập biên bản: chụp tồn sổ sách của kho tại `count_date` thành dòng.

        Phạm vi mặc định là **mọi mã hàng còn dấu vết ở kho ấy** tới ngày kiểm
        kê, kể cả mã tồn 0: kiểm kê phải trả lời được "mã này đáng lẽ hết mà
        vẫn còn 3 cái" — bỏ dòng tồn 0 đi là bỏ đúng những ca ấy.
        """
        if not payload.item_ids and not self._warehouse_has_history(payload):
            raise CountSheetInvalidError(
                "Kho chưa có dấu vết nhập xuất nào tới ngày kiểm kê — không có gì để kiểm",
                warehouse_id=payload.warehouse_id,
                count_date=payload.count_date.isoformat(),
            )
        sheet = InventoryCountSheet(
            id=uuid7(),
            branch_id=payload.branch_id,
            warehouse_id=payload.warehouse_id,
            count_date=payload.count_date,
            note=payload.note,
            created_by=user_id,
        )
        sheet.sheet_no = NumberingService(self._session).allocate(
            COUNT_SHEET_NUMBERING,
            branch_id=payload.branch_id,
            on_date=payload.count_date,
            document_id=sheet.id,
        )
        self._session.add(sheet)
        self._session.flush()
        for index, row in enumerate(self._book_rows(payload), start=1):
            self._session.add(
                InventoryCountSheetLine(
                    sheet_id=sheet.id,
                    line_no=index,
                    item_id=row.item_id,
                    lot_id=row.lot_id,
                    lot_key=lot_key_of(row.lot_id),
                    is_custodial=row.is_custodial,
                    book_qty=row.book_qty,
                    unit_cost=row.unit_cost,
                )
            )
        self._session.flush()
        return sheet

    def _warehouse_has_history(self, payload: InventoryCountSheetIn) -> bool:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(InventoryMovement)
                .where(
                    InventoryMovement.branch_id == payload.branch_id,
                    InventoryMovement.warehouse_id == payload.warehouse_id,
                    InventoryMovement.posting_date <= payload.count_date,
                )
            )
            or 0
        ) > 0

    def _book_rows(self, payload: InventoryCountSheetIn) -> list[_BookRow]:
        """Tồn sổ sách theo `(mã hàng, lô, loại hàng)` tại cuối ngày kiểm kê.

        Đọc thẳng `inventory_movements` như `stock.stock_rows`, nhưng giữ
        `is_custodial` trong khóa nhóm thay vì gộp: hai loại hàng là hai dòng
        đếm, và chỉ dòng của mình mới cần đơn giá.
        """
        signed = InventoryMovement.direction * InventoryMovement.quantity
        query = (
            select(
                InventoryMovement.item_id,
                InventoryMovement.lot_id,
                InventoryMovement.is_custodial,
                func.sum(signed).label("book_qty"),
            )
            .where(
                InventoryMovement.branch_id == payload.branch_id,
                InventoryMovement.warehouse_id == payload.warehouse_id,
                InventoryMovement.posting_date <= payload.count_date,
            )
            .group_by(
                InventoryMovement.item_id,
                InventoryMovement.lot_id,
                InventoryMovement.is_custodial,
            )
            .order_by(
                InventoryMovement.item_id,
                InventoryMovement.lot_id,
                InventoryMovement.is_custodial,
            )
        )
        if payload.item_ids:
            query = query.where(InventoryMovement.item_id.in_(sorted(set(payload.item_ids))))
        rows: list[_BookRow] = []
        for row in self._session.execute(query):
            # Tồn âm trên sổ là lỗi khác (guard FR-STK-040 kêu lúc ghi sổ);
            # biên bản chụp NGUYÊN số âm — nắn nó về 0 là để chênh lệch nói dối.
            book_qty = Decimal(row.book_qty or 0)
            rows.append(
                _BookRow(
                    item_id=row.item_id,
                    lot_id=row.lot_id,
                    is_custodial=row.is_custodial,
                    book_qty=book_qty,
                    unit_cost=(
                        None
                        if row.is_custodial
                        else average_unit_cost_at(
                            self._session,
                            branch_id=payload.branch_id,
                            warehouse_id=payload.warehouse_id,
                            item_id=row.item_id,
                            lot_key=lot_key_of(row.lot_id),
                            as_of=payload.count_date,
                        )
                    ),
                )
            )
        return rows

    # ---------------------------------------------------------------- đọc

    def require(self, sheet_id: UUID) -> InventoryCountSheet:
        sheet = self._session.get(InventoryCountSheet, sheet_id)
        if sheet is None:
            raise CountSheetNotFoundError(
                "Không tìm thấy biên bản kiểm kê kho", sheet_id=str(sheet_id)
            )
        return sheet

    def lines_of(self, sheet_id: UUID) -> list[InventoryCountSheetLine]:
        return list(
            self._session.execute(
                select(InventoryCountSheetLine)
                .where(InventoryCountSheetLine.sheet_id == sheet_id)
                .order_by(InventoryCountSheetLine.line_no)
            )
            .scalars()
            .all()
        )

    def list_page(
        self,
        *,
        warehouse_id: int | None,
        from_date: date | None,
        to_date: date | None,
        page: int,
        page_size: int,
    ) -> tuple[list[InventoryCountSheet], int]:
        query = select(InventoryCountSheet)
        if warehouse_id is not None:
            query = query.where(InventoryCountSheet.warehouse_id == warehouse_id)
        if from_date is not None:
            query = query.where(InventoryCountSheet.count_date >= from_date)
        if to_date is not None:
            query = query.where(InventoryCountSheet.count_date <= to_date)
        total = self._session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = list(
            self._session.execute(
                query.order_by(
                    InventoryCountSheet.count_date.desc(), InventoryCountSheet.created_at.desc()
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return rows, total

    def differences(self, sheet_id: UUID) -> list[InventoryCountSheetDifference]:
        """Chỉ dòng ĐÃ đếm và LỆCH (U8) — dòng khớp và dòng chưa đếm không hiện."""
        self.require(sheet_id)
        rows: list[InventoryCountSheetDifference] = []
        for line in self.lines_of(sheet_id):
            counted = line.counted_qty
            if counted is None or counted == line.book_qty:
                continue
            rows.append(
                InventoryCountSheetDifference(
                    line_no=line.line_no,
                    item_id=line.item_id,
                    lot_id=line.lot_id,
                    is_custodial=line.is_custodial,
                    book_qty=line.book_qty,
                    counted_qty=counted,
                    difference=counted - line.book_qty,
                    unit_cost=line.unit_cost,
                )
            )
        return rows

    # ---------------------------------------------------------------- ghi

    def set_counts(self, sheet_id: UUID, payload: InventoryCountSheetCountsIn) -> None:
        """Nhập số đếm thật cho các dòng đã chụp; sửa được tới khi duyệt."""
        sheet = self._lock(sheet_id)
        self._refuse_when_adjusted(sheet)
        by_line_no = {line.line_no: line for line in self.lines_of(sheet_id)}
        for entry in payload.lines:
            line = by_line_no.get(entry.line_no)
            if line is None:
                raise CountSheetInvalidError(
                    "Biên bản không có dòng này", line_no=entry.line_no, sheet_id=str(sheet_id)
                )
            if line.is_custodial and entry.unit_cost is not None:
                raise CountSheetInvalidError(
                    "Dòng hàng giữ hộ không có đơn giá — hàng nhận giữ hộ không vào "
                    "giá trị tồn kho",
                    line_no=entry.line_no,
                )
            line.counted_qty = entry.counted_qty
            if entry.unit_cost is not None:
                line.unit_cost = entry.unit_cost
            if entry.note is not None:
                line.note = entry.note
        self._session.flush()

    def delete(self, sheet_id: UUID) -> None:
        """Xóa biên bản CHƯA sinh phiếu — đã sinh thì biên bản là căn cứ của
        phiếu, xóa phiếu trước rồi mới tới biên bản (khuôn 6E)."""
        sheet = self._lock(sheet_id)
        self._refuse_when_adjusted(sheet)
        self._session.delete(sheet)
        self._session.flush()

    def apply_differences(
        self, sheet_id: UUID, *, user_id: int, acknowledged_warnings: bool = False
    ) -> tuple[Voucher | None, Voucher | None]:
        """Duyệt: sinh tối đa **hai** phiếu nháp — thừa → NK, thiếu → XK.

        Khóa biên bản TRƯỚC khi đọc-rồi-quyết: request thứ hai chờ ở đây, thấy
        id phiếu đã có và bị từ chối thay vì sinh bộ phiếu thứ hai (cùng lý do
        `CashCountSheetService.create_adjustment`, review 6B H-3).
        """
        sheet = self._lock(sheet_id)
        self._refuse_when_adjusted(sheet)
        differences = self.differences(sheet_id)
        if not differences:
            raise CountSheetAdjustmentError(
                "Biên bản không có chênh lệch — không có gì để xử lý", sheet_id=str(sheet_id)
            )
        surplus = [row for row in differences if row.difference > _ZERO]
        deficit = [row for row in differences if row.difference < _ZERO]
        receipt = (
            self._adjustment_voucher(
                sheet,
                rows=surplus,
                surplus=True,
                user_id=user_id,
                acknowledged_warnings=acknowledged_warnings,
            )
            if surplus
            else None
        )
        issue = (
            self._adjustment_voucher(
                sheet,
                rows=deficit,
                surplus=False,
                user_id=user_id,
                acknowledged_warnings=acknowledged_warnings,
            )
            if deficit
            else None
        )
        sheet.adjustment_receipt_id = receipt.id if receipt is not None else None
        sheet.adjustment_issue_id = issue.id if issue is not None else None
        self._session.flush()
        return receipt, issue

    # ------------------------------------------------------------- nội bộ

    def _lock(self, sheet_id: UUID) -> InventoryCountSheet:
        sheet = self._session.execute(
            select(InventoryCountSheet).where(InventoryCountSheet.id == sheet_id).with_for_update()
        ).scalar_one_or_none()
        if sheet is None:
            raise CountSheetNotFoundError(
                "Không tìm thấy biên bản kiểm kê kho", sheet_id=str(sheet_id)
            )
        return sheet

    def _refuse_when_adjusted(self, sheet: InventoryCountSheet) -> None:
        if sheet.adjustment_receipt_id is None and sheet.adjustment_issue_id is None:
            return
        raise CountSheetAdjustmentError(
            "Biên bản đã sinh phiếu xử lý chênh lệch — xóa phiếu đó trước",
            sheet_id=str(sheet.id),
            adjustment_receipt_id=(
                str(sheet.adjustment_receipt_id) if sheet.adjustment_receipt_id else None
            ),
            adjustment_issue_id=(
                str(sheet.adjustment_issue_id) if sheet.adjustment_issue_id else None
            ),
        )

    def _adjustment_voucher(
        self,
        sheet: InventoryCountSheet,
        *,
        rows: Sequence[InventoryCountSheetDifference],
        surplus: bool,
        user_id: int,
        acknowledged_warnings: bool,
    ) -> Voucher:
        from ket.modules.inventory.service import InventoryVoucherService

        items = self._items_of(rows)
        # TK đối ứng tra **chỉ khi có dòng định khoản**: phiếu toàn dòng giữ hộ
        # không có bút toán nào, nên nó không được đổ vì gói chưa khai một
        # purpose mà nó không dùng.
        posts_ledger = any(not row.is_custodial for row in rows)
        counter_account_id = (
            self._purpose_account_id(sheet, SURPLUS_PURPOSE if surplus else DEFICIT_PURPOSE)
            if posts_ledger
            else 0
        )
        year = fiscal_year_covering(self._session, sheet.count_date)
        base_currency = year.base_currency if year is not None else "VND"
        lines: list[InventoryVoucherLineIn] = []
        for row in rows:
            item = items[row.item_id]
            quantity = abs(row.difference)
            if row.is_custodial:
                # Không định khoản, không giá: phiếu chỉ điều chỉnh số lượng
                # hàng đang giữ hộ (BR-STK-07).
                lines.append(
                    InventoryVoucherLineIn(
                        item_id=row.item_id,
                        warehouse_id=sheet.warehouse_id,
                        unit_id=self._base_unit_id(item),
                        quantity=quantity,
                        is_custodial=True,
                        description=self._line_description(sheet, surplus=surplus),
                    )
                )
                continue
            stock_account_id = self._stock_account_id(sheet, item)
            unit_cost = row.unit_cost if surplus else None
            if surplus and unit_cost is None:
                raise CountSheetAdjustmentError(
                    "Dòng thừa chưa có đơn giá — khóa tồn kho này chưa có lần nhập nào "
                    "có giá, hãy nhập đơn giá trên biên bản",
                    sheet_id=str(sheet.id),
                    line_no=row.line_no,
                    item_id=row.item_id,
                )
            lines.append(
                InventoryVoucherLineIn(
                    item_id=row.item_id,
                    warehouse_id=sheet.warehouse_id,
                    unit_id=self._base_unit_id(item),
                    quantity=quantity,
                    unit_cost_fc=unit_cost,
                    debit_account_id=stock_account_id if surplus else counter_account_id,
                    credit_account_id=counter_account_id if surplus else stock_account_id,
                    description=self._line_description(sheet, surplus=surplus),
                )
            )
        return InventoryVoucherService(self._session).create(
            InventoryVoucherIn(
                kind=(InventoryVoucherKind.RECEIPT if surplus else InventoryVoucherKind.ISSUE),
                operation_code=SURPLUS_OPERATION if surplus else DEFICIT_OPERATION,
                warehouse_id=sheet.warehouse_id,
                branch_id=sheet.branch_id,
                document_date=sheet.count_date,
                posting_date=sheet.count_date,
                currency_code=base_currency,
                exchange_rate=Decimal(1),
                description=self._line_description(sheet, surplus=surplus),
                lines=tuple(lines),
            ),
            user_id=user_id,
            acknowledged_warnings=acknowledged_warnings,
        )

    def _line_description(self, sheet: InventoryCountSheet, *, surplus: bool) -> str:
        return (
            f"Xử lý {'thừa' if surplus else 'thiếu'} kho theo biên bản kiểm kê "
            f"{sheet.sheet_no} ngày {sheet.count_date.isoformat()}"
        )

    def _items_of(self, rows: Sequence[InventoryCountSheetDifference]) -> dict[int, Item]:
        item_ids = sorted({row.item_id for row in rows})
        items = {
            item.id: item
            for item in self._session.execute(select(Item).where(Item.id.in_(item_ids)))
            .scalars()
            .all()
        }
        missing = [item_id for item_id in item_ids if item_id not in items]
        if missing:  # pragma: no cover - FK RESTRICT giữ mã hàng còn sống
            raise CountSheetAdjustmentError(
                "Mã hàng trên biên bản không còn trong danh mục", item_id=missing[0]
            )
        return items

    def _base_unit_id(self, item: Item) -> int:
        if item.base_unit_id is None:
            raise CountSheetAdjustmentError(
                "Mã hàng chưa khai đơn vị tính chính — không quy đổi được số kiểm kê",
                item_id=item.id,
            )
        return item.base_unit_id

    def _stock_account_id(self, sheet: InventoryCountSheet, item: Item) -> int:
        if item.nature not in INVENTORY_NATURES:  # pragma: no cover - chỉ mã có tồn vào sổ kho
            raise CountSheetAdjustmentError("Mã hàng không theo dõi tồn kho", item_id=item.id)
        return self._purpose_account_id(sheet, STOCK_PURPOSE_BY_NATURE[item.nature])

    def _purpose_account_id(self, sheet: InventoryCountSheet, purpose: str) -> int:
        """TK của một `purpose` trong gói hiệu lực — khoảng trống cấu hình báo thẳng."""
        year = fiscal_year_covering(self._session, sheet.count_date)
        if year is None:
            raise CountSheetInvalidError(
                "Chưa có năm tài chính phủ ngày kiểm kê",
                count_date=sheet.count_date.isoformat(),
            )
        package = resolve_package(
            self._session, scheme=year.accounting_scheme, on_date=sheet.count_date
        )
        row = default_account(
            self._session,
            package_id=package.id,
            document_type=COUNT_SHEET_DOCUMENT_TYPE,
            purpose=purpose,
        )
        account_id = self._session.execute(
            select(ChartOfAccount.id).where(
                ChartOfAccount.package_id == package.id,
                ChartOfAccount.code == row.account_code,
            )
        ).scalar_one_or_none()
        if account_id is None:
            raise CountSheetAdjustmentError(
                "TK xử lý chênh lệch kiểm kê trong cấu hình không có trong hệ thống TK",
                account_code=row.account_code,
                purpose=purpose,
            )
        return account_id


class _BookRow:
    """Một dòng tồn sổ sách vừa chụp — nội bộ của lượt lập biên bản."""

    __slots__ = ("book_qty", "is_custodial", "item_id", "lot_id", "unit_cost")

    def __init__(
        self,
        *,
        item_id: int,
        lot_id: int | None,
        is_custodial: bool,
        book_qty: Decimal,
        unit_cost: Decimal | None,
    ) -> None:
        self.item_id = item_id
        self.lot_id = lot_id
        self.is_custodial = is_custodial
        self.book_qty = book_qty
        self.unit_cost = unit_cost
