"""Hình dạng request/response của phiếu kho (SRS 09, lát 8A) — Pydantic ở mọi
ranh giới API (ADR-015), cùng khuôn `cash_book/schemas.py`.

Dòng phiếu mang **số lượng theo ĐVT gõ** + giá vốn tùy chọn; `base_quantity`
và `amount_fc` là số server tính (client không gửi — cùng luật "thẻ tổng là số
server" H15). Cặp TK trên dòng để trống được ở bản nháp; mapper từ chối ghi sổ
dòng có cặp TK mà thiếu giá (bút toán không có số tiền).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.modules.inventory.models import (
    ALLOCATION_RATIO_PRECISION,
    ALLOCATION_RATIO_SCALE,
    DESCRIPTION_MAX_LENGTH,
    LOT_NO_MAX_LENGTH,
    UNIT_COST_PRECISION,
    UNIT_COST_SCALE,
    InventoryVoucherKind,
)
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE, PartnerKind

_ZERO = Decimal(0)

OPERATION_CODE_INPUT_MAX = 50


class ExtendedDimensionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: int
    value_id: int


class InventoryVoucherLineIn(BaseModel):
    """Một dòng vật tư: mã hàng, kho, số lượng theo ĐVT gõ, giá vốn tùy chọn."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_variant_id: int | None = None
    warehouse_id: int | None = None
    """Trống = lấy kho của phiếu. Phiếu chuyển kho **không** nhận kho theo dòng:
    kho đi/đến là của cả phiếu."""
    lot_no: str | None = Field(default=None, max_length=LOT_NO_MAX_LENGTH)
    """Số lô gõ tay — service tra/tạo `lots` theo `(item_id, lot_no)`."""
    unit_id: int
    quantity: Decimal = Field(
        gt=_ZERO, max_digits=QUANTITY_PRECISION, decimal_places=QUANTITY_SCALE
    )
    unit_cost_fc: Decimal | None = Field(
        default=None, ge=_ZERO, max_digits=UNIT_COST_PRECISION, decimal_places=UNIT_COST_SCALE
    )
    """Giá vốn theo **đơn vị gõ** (`unit_id`), nguyên tệ — service quy về đơn vị
    chính khi lưu. Bắt buộc trên phiếu nhập gõ tay; dòng xuất để trống (engine
    8B tính)."""
    amount_fc: Decimal | None = Field(
        default=None, ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    """Thành tiền — chỉ phiếu sinh từ chứng từ nguồn gửi (số chủ, xem
    `InventoryMovementLine.amount_fc`); phiếu gõ tay để trống, server tính
    `quantity × unit_cost_fc`."""

    debit_account_id: int | None = None
    credit_account_id: int | None = None

    source_movement_id: int | None = Field(default=None, ge=1)
    """Đích danh: id movement **nhập** mà dòng xuất này lấy hàng — bắt buộc khi
    năm tài chính tính giá `specific`, bị từ chối ở phiếu nhập."""

    is_product: bool = False
    """Dòng thành phẩm của phiếu lắp ráp / tháo dỡ (đúng một dòng mỗi phiếu);
    luôn `false` trên NK/XK/CK."""
    allocation_ratio: Decimal | None = Field(
        default=None,
        gt=_ZERO,
        max_digits=ALLOCATION_RATIO_PRECISION,
        decimal_places=ALLOCATION_RATIO_SCALE,
    )
    """Tỷ lệ phân bổ giá trị — bắt buộc trên dòng linh kiện của phiếu tháo dỡ,
    vắng ở mọi dòng khác."""

    partner_id: int | None = None
    partner_kind: PartnerKind | None = None
    cost_object_id: int | None = None
    project_id: int | None = None
    order_id: int | None = None
    contract_id: int | None = None
    expense_item_id: int | None = None
    extended: tuple[ExtendedDimensionIn, ...] = ()

    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)

    @model_validator(mode="after")
    def _pairs_sane(self) -> InventoryVoucherLineIn:
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        if (self.debit_account_id is None) != (self.credit_account_id is None):
            raise ValueError("TK Nợ và TK Có của dòng phải cùng có hoặc cùng vắng")
        if self.is_product and (
            self.debit_account_id is not None
            or self.unit_cost_fc is not None
            or self.amount_fc is not None
            or self.source_movement_id is not None
            or self.allocation_ratio is not None
        ):
            raise ValueError(
                "Dòng thành phẩm không định khoản, không gõ giá, không chỉ nguồn, không tỷ lệ — "
                "giá thành phẩm do engine suy từ các dòng linh kiện"
            )
        return self


ASSEMBLY_KINDS = frozenset({InventoryVoucherKind.ASSEMBLY, InventoryVoucherKind.DISASSEMBLY})
"""Hai loại phiếu có dòng thành phẩm + dòng linh kiện (lát 8C-2)."""


class InventoryVoucherIn(BaseModel):
    """Thân phiếu kho cho cả tạo mới lẫn sửa (PUT gửi trọn bộ thay thế)."""

    model_config = ConfigDict(extra="forbid")

    kind: int = Field(ge=InventoryVoucherKind.RECEIPT, le=InventoryVoucherKind.DISASSEMBLY)
    operation_code: str = Field(min_length=1, max_length=OPERATION_CODE_INPUT_MAX)
    warehouse_id: int
    to_warehouse_id: int | None = None

    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    exchange_rate: Decimal = Field(
        default=Decimal(1), max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )

    partner_id: int | None = None
    partner_kind: PartnerKind | None = None
    delivered_by: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)

    lines: tuple[InventoryVoucherLineIn, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _voucher_sane(self) -> InventoryVoucherIn:
        if self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        is_transfer = self.kind == InventoryVoucherKind.TRANSFER
        if is_transfer != (self.to_warehouse_id is not None):
            raise ValueError("Chỉ phiếu chuyển kho mới có (và phải có) kho đến")
        if is_transfer and self.to_warehouse_id == self.warehouse_id:
            raise ValueError("Kho đến phải khác kho đi")
        if is_transfer and any(
            line.warehouse_id not in (None, self.warehouse_id) for line in self.lines
        ):
            raise ValueError("Phiếu chuyển kho không nhận kho riêng theo dòng")
        products = sum(1 for line in self.lines if line.is_product)
        if self.kind in ASSEMBLY_KINDS:
            if products != 1:
                raise ValueError("Phiếu lắp ráp / tháo dỡ phải có đúng một dòng thành phẩm")
            if len(self.lines) < 2:
                raise ValueError("Phiếu lắp ráp / tháo dỡ phải có ít nhất một dòng linh kiện")
            needs_ratio = self.kind == InventoryVoucherKind.DISASSEMBLY
            for line in self.lines:
                if line.is_product:
                    continue
                if needs_ratio and line.allocation_ratio is None:
                    raise ValueError(
                        "Dòng linh kiện của phiếu tháo dỡ phải có tỷ lệ phân bổ giá trị"
                    )
                if not needs_ratio and line.allocation_ratio is not None:
                    raise ValueError("Chỉ phiếu tháo dỡ mới có tỷ lệ phân bổ trên dòng linh kiện")
        elif products or any(line.allocation_ratio is not None for line in self.lines):
            raise ValueError(
                "Dòng thành phẩm / tỷ lệ phân bổ chỉ có trên phiếu lắp ráp hoặc tháo dỡ"
            )
        return self


class InventoryVoucherUpdate(InventoryVoucherIn):
    """PUT mang thêm `row_version` — khóa lạc quan (FR-NFR-005)."""

    row_version: int


class InventoryVoucherLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    item_id: int
    item_variant_id: int | None
    warehouse_id: int
    lot_id: int | None
    serial_id: int | None
    unit_id: int
    quantity: Decimal
    base_quantity: Decimal
    unit_cost_fc: Decimal | None
    amount_fc: Decimal | None
    debit_account_id: int | None
    credit_account_id: int | None
    partner_id: int | None
    partner_kind: int | None
    cost_object_id: int | None
    project_id: int | None
    order_id: int | None
    contract_id: int | None
    expense_item_id: int | None
    extended_dimensions: dict[str, int] | None
    source_line_id: UUID | None
    source_movement_id: int | None = None
    is_product: bool = False
    allocation_ratio: Decimal | None = None
    description: str | None


class InventoryVoucherOut(BaseModel):
    """Header chứng từ + thân phiếu — client cần cả hai để vẽ lại form."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_type: str
    voucher_no: str
    branch_id: int
    document_date: date
    posting_date: date
    period_id: int
    currency_code: str
    exchange_rate: Decimal
    description: str | None
    status: int
    entry_kind: int
    source_document_id: UUID | None
    created_at: datetime
    created_by: int
    posted_at: datetime | None
    posted_by: int | None
    row_version: int

    kind: int = 0
    operation_code: str = ""
    warehouse_id: int = 0
    to_warehouse_id: int | None = None
    partner_id: int | None = None
    partner_kind: int | None = None
    delivered_by: str | None = None
    keeper_status: int = 0

    lines: tuple[InventoryVoucherLineOut, ...] = ()


class ReorderDayIn(BaseModel):
    """Sắp xếp lại thứ tự trong ngày cho một khóa tồn kho (FR-STK-017)."""

    model_config = ConfigDict(extra="forbid")

    branch_id: int
    warehouse_id: int
    item_id: int
    lot_id: int | None = None
    posting_date: date
    ordered_movement_ids: tuple[int, ...] = Field(min_length=1)
    """Đủ và đúng tập movement của `(khóa, ngày)` theo thứ tự mới — thiếu hay
    thừa một id là từ chối cả lượt."""

    @model_validator(mode="after")
    def _ids_unique(self) -> ReorderDayIn:
        if len(set(self.ordered_movement_ids)) != len(self.ordered_movement_ids):
            raise ValueError("Danh sách thứ tự có id lặp")
        return self


class ReorderDayOut(BaseModel):
    reordered: int
    marked_from: date


class StockRow(BaseModel):
    """Một dòng tồn theo khóa `(kho, vật tư, lô)` tại `as_of` — số lượng đơn vị
    chính; giá trị `None` cho tới khi 8B có engine tính giá."""

    branch_id: int
    warehouse_id: int
    item_id: int
    lot_id: int | None
    on_hand: Decimal
    value: Decimal | None = None
    """Giá trị tồn (VND) — chỉ khi mọi movement của khóa đã tính giá."""


class StockResponse(BaseModel):
    as_of: date
    items: tuple[StockRow, ...]


class AffectedVoucher(BaseModel):
    """Một chứng từ sẽ bị tính lại giá xuất (FR-STK-003) — đủ để client mở nó."""

    voucher_id: UUID
    voucher_no: str
    document_type: str
    posting_date: date
    movements: int
    """Số dòng sổ kho của chứng từ nằm trong horizon."""


class LockedPeriodTouched(BaseModel):
    """Kỳ đã khóa mà horizon tính lại chạm tới (RT-11) — job sẽ từ chối."""

    period_id: int
    period_no: int
    movements: int


class CostingAffectedPreview(BaseModel):
    """Xem trước trước khi bấm "Tính giá xuất kho" (FR-STK-003, RT-11)."""

    branch_id: int
    valuation_method: str | None
    """Phương pháp của năm chứa `earliest_from_date`; `None` khi không có gì để tính."""
    earliest_from_date: date | None
    keys: int
    movements: int
    voucher_count: int
    vouchers: tuple[AffectedVoucher, ...]
    """Tối đa `AFFECTED_VOUCHER_LIMIT` chứng từ sớm nhất; `voucher_count` là tổng thật."""
    locked_periods: tuple[LockedPeriodTouched, ...]
    """Rỗng là điều kiện để job chạy; có dòng = phải mở khóa kỳ đó trước."""


class UncostedVouchersResponse(BaseModel):
    """FR-STK-008: chứng từ kho còn dòng sổ kho **chưa tính giá** (chờ hoặc cần
    tính lại) của chi nhánh đang thao tác — danh sách cắt ở giới hạn, `count` là
    tổng thật; `movements` của mỗi chứng từ = số dòng chưa giá."""

    branch_id: int
    date_from: date | None
    date_to: date | None
    count: int
    vouchers: tuple[AffectedVoucher, ...]
