"""Hình dạng request/response của hóa đơn mua hàng (SRS 05, lát 7B).

Pydantic ở mọi ranh giới API (ADR-015). So với phiếu thu/chi, thân hóa đơn mua
có **ba danh sách** thay vì hai: dòng hàng/dịch vụ, chi phí mua hàng
(`landed_costs` — quyết định lồng trong thân, không mở endpoint riêng) và đối
trừ — cái sau chỉ hợp lệ trên chứng từ **trả lại hàng** (kind 4): khoản trả
lại đối trừ vào hóa đơn gốc qua cùng cơ chế mà phiếu chi dùng, thay vì sinh
một "công nợ âm".

Cặp `amount` tùy chọn trên dòng là số quy đổi client đang hiển thị — server so
với `round_money(fc × rate)` rồi từ chối nếu lệch, cùng vai với `cash_book`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import RATE_SCALE_DEFAULT, UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.modules.purchase.models import (
    DESCRIPTION_MAX_LENGTH,
    VAT_RATE_PRECISION,
    VAT_RATE_SCALE,
    VENDOR_INVOICE_FORM_MAX_LENGTH,
    VENDOR_INVOICE_NO_MAX_LENGTH,
    VENDOR_INVOICE_SERIAL_MAX_LENGTH,
    LandedCostAllocation,
    PurchaseInvoiceKind,
    VendorInvoiceStatus,
)
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

_ZERO = Decimal(0)

OPERATION_CODE_INPUT_MAX = 50


class ExtendedDimensionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: int
    value_id: int


class PurchaseInvoiceLineIn(BaseModel):
    """Một dòng hàng hóa / dịch vụ trên hóa đơn.

    `amount_fc` là số người dùng chốt, không suy từ `quantity × unit_price_fc`:
    hóa đơn của nhà cung cấp làm tròn theo cách của họ, và số trên chứng từ
    gốc là số phải khớp — cặp số lượng/đơn giá chỉ để tính giá vốn nhập kho.
    """

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)
    item_id: int | None = None
    unit_id: int | None = None
    warehouse_id: int | None = None
    quantity: Decimal | None = Field(
        default=None, gt=_ZERO, max_digits=QUANTITY_PRECISION, decimal_places=QUANTITY_SCALE
    )
    unit_price_fc: Decimal | None = Field(
        default=None, ge=_ZERO, max_digits=UNIT_PRICE_PRECISION, decimal_places=UNIT_PRICE_SCALE
    )
    amount_fc: Decimal = Field(gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE)
    amount: Decimal | None = Field(
        default=None, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    """Số quy đổi client hiển thị — gửi thì server kiểm khớp luật làm tròn."""

    vat_rate: Decimal | None = Field(
        default=None, ge=_ZERO, max_digits=VAT_RATE_PRECISION, decimal_places=VAT_RATE_SCALE
    )
    vat_amount_fc: Decimal = Field(
        default=_ZERO, ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    landed_cost_fc: Decimal = Field(
        default=_ZERO, ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    """Phần chi phí mua hàng phân bổ vào dòng — chỉ nhận khi hóa đơn chọn phân
    bổ THỦ CÔNG; hai cách còn lại server tự tính và ghi đè."""

    account_id: int
    vat_account_id: int | None = None

    cost_object_id: int | None = None
    project_id: int | None = None
    order_id: int | None = None
    contract_id: int | None = None
    expense_item_id: int | None = None
    extended: tuple[ExtendedDimensionIn, ...] = ()

    @model_validator(mode="after")
    def _line_sane(self) -> PurchaseInvoiceLineIn:
        if self.vat_amount_fc > _ZERO and self.vat_account_id is None:
            raise ValueError("Dòng có thuế GTGT phải có tài khoản thuế")
        if self.warehouse_id is not None and self.item_id is None:
            raise ValueError("Dòng nhập kho phải có vật tư/hàng hóa")
        return self


class LandedCostIn(BaseModel):
    """Một khoản chi phí mua hàng (vận chuyển, bốc xếp, thuế nhập khẩu…).

    `credit_account_id` do người dùng chọn: 331 khi nợ một nhà cung cấp dịch
    vụ (kèm `vendor_id`), 3333/3332 khi là thuế nhập khẩu/TTĐB, 111 khi đã chi
    tiền — mỗi loại một TK, không đoán theo gói cấu hình.
    """

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=DESCRIPTION_MAX_LENGTH)
    vendor_id: int | None = None
    credit_account_id: int
    amount_fc: Decimal = Field(
        default=_ZERO, ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    vat_rate: Decimal | None = Field(
        default=None, ge=_ZERO, max_digits=VAT_RATE_PRECISION, decimal_places=VAT_RATE_SCALE
    )
    vat_amount_fc: Decimal = Field(
        default=_ZERO, ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    vat_account_id: int | None = None

    @model_validator(mode="after")
    def _cost_sane(self) -> LandedCostIn:
        if self.amount_fc <= _ZERO and self.vat_amount_fc <= _ZERO:
            raise ValueError("Khoản chi phí mua hàng phải có số tiền hoặc thuế")
        if self.vat_amount_fc > _ZERO and self.vat_account_id is None:
            raise ValueError("Khoản chi phí có thuế GTGT phải có tài khoản thuế")
        return self


class PurchaseSettlementIn(BaseModel):
    """Đối trừ khoản trả lại vào hóa đơn mua gốc — chỉ nhận `amount_fc`, số VND
    và chênh lệch tỷ giá do server tính (FR-SYS-066)."""

    model_config = ConfigDict(extra="forbid")

    target_kind: SettlementTargetKind
    target_id: UUID
    amount_fc: Decimal = Field(gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE)


class PurchaseInvoiceIn(BaseModel):
    """Thân hóa đơn cho cả tạo mới lẫn sửa (PUT gửi trọn bộ thay thế)."""

    model_config = ConfigDict(extra="forbid")

    kind: int = Field(ge=PurchaseInvoiceKind.GOODS, le=PurchaseInvoiceKind.RETURN)
    operation_code: str = Field(min_length=1, max_length=OPERATION_CODE_INPUT_MAX)
    vendor_id: int
    payable_account_id: int

    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    exchange_rate: Decimal = Field(
        default=Decimal(1), max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )

    vendor_invoice_status: int = Field(
        default=VendorInvoiceStatus.RECEIVED,
        ge=VendorInvoiceStatus.RECEIVED,
        le=VendorInvoiceStatus.NONE,
    )
    vendor_invoice_form: str | None = Field(default=None, max_length=VENDOR_INVOICE_FORM_MAX_LENGTH)
    vendor_invoice_serial: str | None = Field(
        default=None, max_length=VENDOR_INVOICE_SERIAL_MAX_LENGTH
    )
    vendor_invoice_no: str | None = Field(default=None, max_length=VENDOR_INVOICE_NO_MAX_LENGTH)
    vendor_invoice_date: date | None = None

    payment_term_id: int | None = None
    due_date: date | None = None
    landed_cost_allocation: int = Field(
        default=LandedCostAllocation.BY_VALUE,
        ge=LandedCostAllocation.BY_VALUE,
        le=LandedCostAllocation.MANUAL,
    )
    description: str | None = Field(default=None, max_length=1000)

    lines: tuple[PurchaseInvoiceLineIn, ...] = Field(min_length=1)
    landed_costs: tuple[LandedCostIn, ...] = ()
    settlements: tuple[PurchaseSettlementIn, ...] = ()

    @model_validator(mode="after")
    def _invoice_sane(self) -> PurchaseInvoiceIn:
        if self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        if self.kind == PurchaseInvoiceKind.RETURN:
            if self.landed_costs:
                raise ValueError("Chứng từ trả lại hàng mua không có chi phí mua hàng")
            if not self.settlements:
                # Sổ phụ công nợ không có dòng âm: khoản trả lại chỉ tồn tại
                # dưới dạng giảm nợ của hóa đơn gốc — không chọn hóa đơn thì
                # không có chỗ nào để ghi nó.
                raise ValueError("Chứng từ trả lại hàng mua phải đối trừ vào hóa đơn gốc")
        elif self.settlements:
            raise ValueError("Chỉ chứng từ trả lại hàng mua mới đối trừ vào hóa đơn gốc")
        targets = [(row.target_kind, row.target_id) for row in self.settlements]
        if len(targets) != len(set(targets)):
            raise ValueError("Một chứng từ công nợ chỉ đối trừ một dòng trên mỗi hóa đơn")
        return self


class PurchaseInvoiceUpdate(PurchaseInvoiceIn):
    """PUT mang thêm `row_version` — khóa lạc quan (FR-NFR-005)."""

    row_version: int


class PurchaseInvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    description: str | None
    item_id: int | None
    unit_id: int | None
    warehouse_id: int | None
    quantity: Decimal | None
    unit_price_fc: Decimal | None
    amount_fc: Decimal
    vat_rate: Decimal | None
    vat_amount_fc: Decimal
    landed_cost_fc: Decimal
    account_id: int
    vat_account_id: int | None
    cost_object_id: int | None
    project_id: int | None
    order_id: int | None
    contract_id: int | None
    expense_item_id: int | None
    extended_dimensions: dict[str, int] | None


class LandedCostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    description: str
    vendor_id: int | None
    credit_account_id: int
    amount_fc: Decimal
    vat_rate: Decimal | None
    vat_amount_fc: Decimal
    vat_account_id: int | None


class PurchaseSettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_kind: int
    target_id: UUID
    amount_fc: Decimal
    amount: Decimal
    fx_diff: Decimal


class PurchaseInvoiceOut(BaseModel):
    """Header chứng từ + thân hóa đơn — client cần cả hai để vẽ lại form."""

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
    cashflow_activity: int | None
    entry_kind: int
    created_at: datetime
    created_by: int
    posted_at: datetime | None
    posted_by: int | None
    row_version: int

    kind: int = 0
    operation_code: str = ""
    vendor_id: int = 0
    vendor_invoice_status: int = 0
    vendor_invoice_form: str | None = None
    vendor_invoice_serial: str | None = None
    vendor_invoice_no: str | None = None
    vendor_invoice_date: date | None = None
    payment_term_id: int | None = None
    due_date: date | None = None
    payable_account_id: int = 0
    landed_cost_allocation: int = 0
    total_before_tax_fc: Decimal = _ZERO
    total_vat_fc: Decimal = _ZERO
    total_landed_cost_fc: Decimal = _ZERO
    total_fc: Decimal = _ZERO

    lines: tuple[PurchaseInvoiceLineOut, ...] = ()
    landed_costs: tuple[LandedCostOut, ...] = ()
    settlements: tuple[PurchaseSettlementOut, ...] = ()
