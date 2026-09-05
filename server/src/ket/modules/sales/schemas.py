"""Hình dạng request/response của hóa đơn bán hàng (SRS 06, lát 7C-2).

Pydantic ở mọi ranh giới API (ADR-015). Cùng hình dạng với hóa đơn mua (7B),
khác ở ba chỗ:

* **hai danh sách** thay vì ba — không có chi phí mua hàng ở chiều bán;
* dòng mang thêm **chiết khấu thương mại** (`discount_percent`,
  `discount_amount_fc`) và **ba cột giá vốn** (`cogs_account_id`,
  `inventory_account_id`, `unit_cost_fc`) mà phase 8 sẽ đọc;
* đối trừ hợp lệ trên **hai** loại chứng từ — trả lại hàng bán (kind 2) và
  giảm giá hàng bán (kind 3) — thay vì một.

**Đơn giá và chiết khấu là số client chốt, server không tra lại** (quyết định
user 2026-09-04). Client hỏi `POST /api/v1/pricing/quote-batch` một lượt cho cả
chứng từ rồi gửi lên số đã chốt, đúng vai mà `amount_fc` giữ ở hóa đơn mua:
người lập chứng từ **được phép** gõ tay đơn giá, và bộ định giá trả `source =
none` kèm đơn giá `0` cho mã hàng chưa khai giá — server ghi đè sẽ xóa im lặng
đúng những dòng ấy. Ba trường `price_list_id` / `price_source` /
`discount_percent` đi kèm để chứng từ trả lời được câu "số này ở đâu ra".

Cặp `amount` tùy chọn trên dòng là số quy đổi client đang hiển thị — server so
với `round_money(fc × rate)` rồi từ chối nếu lệch, cùng vai với `cash_book`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import (
    RATE_SCALE_DEFAULT,
    UNIT_PRICE_PRECISION,
    UNIT_PRICE_SCALE,
    VAT_RATE_PRECISION,
    VAT_RATE_SCALE,
)
from ket.kernel.pricing import PriceSource
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.modules.sales.models import (
    DESCRIPTION_MAX_LENGTH,
    DISCOUNT_PERCENT_PRECISION,
    DISCOUNT_PERCENT_SCALE,
    INVOICE_FORM_MAX_LENGTH,
    INVOICE_NO_MAX_LENGTH,
    INVOICE_SERIAL_MAX_LENGTH,
    RECIPIENT_MAX_LENGTH,
    REVERSING_KINDS,
    SHIP_TO_MAX_LENGTH,
    SalesInvoiceKind,
)
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)

OPERATION_CODE_INPUT_MAX = 50


class ExtendedDimensionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: int
    value_id: int


class SalesInvoiceLineIn(BaseModel):
    """Một dòng hàng hóa / dịch vụ trên hóa đơn bán.

    `amount_fc` là doanh thu **sau** chiết khấu, tức con số in trên hóa đơn —
    không suy từ `quantity × unit_price_fc − discount_amount_fc`: hóa đơn làm
    tròn theo cách của nó, và cặp số lượng/đơn giá chỉ để bảng kê và báo cáo
    bán hàng đọc lại.
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
    discount_percent: Decimal | None = Field(
        default=None,
        ge=_ZERO,
        le=_HUNDRED,
        max_digits=DISCOUNT_PERCENT_PRECISION,
        decimal_places=DISCOUNT_PERCENT_SCALE,
    )
    discount_amount_fc: Decimal = Field(
        default=_ZERO, ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    """Chiết khấu thương mại đã trừ khỏi `amount_fc` — giữ lại để bảng kê và
    báo cáo bán hàng đọc phần đã giảm (FR-SAL §4.2, FR-SYS-045)."""

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

    account_id: int
    vat_account_id: int | None = None

    cogs_account_id: int | None = None
    inventory_account_id: int | None = None
    unit_cost_fc: Decimal | None = Field(
        default=None, ge=_ZERO, max_digits=UNIT_PRICE_PRECISION, decimal_places=UNIT_PRICE_SCALE
    )
    """Cặp TK giá vốn / TK kho và đơn giá vốn — lát này chỉ lưu, phase 8 tính
    lại và ghi bút toán Nợ 632 / Có 156 (`sales_invoices.cogs_posted`)."""

    price_list_id: int | None = None
    price_source: PriceSource | None = None
    """Bảng giá và tầng giá đã trả lời cho dòng — client chép nguyên từ kết quả
    `/pricing/quote-batch`. Không tham gia phép tính nào."""

    cost_object_id: int | None = None
    project_id: int | None = None
    order_id: int | None = None
    contract_id: int | None = None
    expense_item_id: int | None = None
    extended: tuple[ExtendedDimensionIn, ...] = ()

    @model_validator(mode="after")
    def _line_sane(self) -> SalesInvoiceLineIn:
        if self.vat_amount_fc > _ZERO and self.vat_account_id is None:
            raise ValueError("Dòng có thuế GTGT phải có tài khoản thuế")
        if self.warehouse_id is not None and self.item_id is None:
            raise ValueError("Dòng xuất kho phải có vật tư/hàng hóa")
        return self


class SalesSettlementIn(BaseModel):
    """Đối trừ khoản giảm trừ vào hóa đơn bán gốc — chỉ nhận `amount_fc`, số VND
    và chênh lệch tỷ giá do server tính (FR-SYS-066)."""

    model_config = ConfigDict(extra="forbid")

    target_kind: SettlementTargetKind
    target_id: UUID
    amount_fc: Decimal = Field(gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE)


class SalesInvoiceIn(BaseModel):
    """Thân hóa đơn cho cả tạo mới lẫn sửa (PUT gửi trọn bộ thay thế)."""

    model_config = ConfigDict(extra="forbid")

    kind: int = Field(ge=SalesInvoiceKind.GOODS, le=SalesInvoiceKind.AGENCY)
    operation_code: str = Field(min_length=1, max_length=OPERATION_CODE_INPUT_MAX)
    customer_id: int
    receivable_account_id: int

    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    exchange_rate: Decimal = Field(
        default=Decimal(1), max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )

    salesperson_id: int | None = None
    ship_to: str | None = Field(default=None, max_length=SHIP_TO_MAX_LENGTH)
    recipient_name: str | None = Field(default=None, max_length=RECIPIENT_MAX_LENGTH)

    invoice_form: str | None = Field(default=None, max_length=INVOICE_FORM_MAX_LENGTH)
    invoice_serial: str | None = Field(default=None, max_length=INVOICE_SERIAL_MAX_LENGTH)
    invoice_no: str | None = Field(default=None, max_length=INVOICE_NO_MAX_LENGTH)
    invoice_date: date | None = None

    payment_term_id: int | None = None
    due_date: date | None = None
    price_list_id: int | None = None
    is_stock_issue: bool = False
    description: str | None = Field(default=None, max_length=1000)

    lines: tuple[SalesInvoiceLineIn, ...] = Field(min_length=1)
    settlements: tuple[SalesSettlementIn, ...] = ()

    @model_validator(mode="after")
    def _invoice_sane(self) -> SalesInvoiceIn:
        if self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        if self.kind in REVERSING_KINDS:
            if not self.settlements:
                # Sổ phụ công nợ không có dòng âm: khoản trả lại / giảm giá chỉ
                # tồn tại dưới dạng giảm nợ của hóa đơn gốc — không chọn hóa
                # đơn thì không có chỗ nào để ghi nó. Hóa đơn gốc ĐÃ THU ĐỦ thì
                # không còn gì để đối trừ và chứng từ này bị từ chối ở đây:
                # đường đúng lúc ấy là trả tiền lại khách bằng phiếu chi
                # (quyết định user 2026-09-04, cùng hình dạng với 7B).
                raise ValueError(
                    "Chứng từ trả lại / giảm giá hàng bán phải đối trừ vào hóa đơn gốc"
                )
        elif self.settlements:
            raise ValueError("Chỉ chứng từ trả lại / giảm giá hàng bán mới đối trừ hóa đơn gốc")
        targets = [(row.target_kind, row.target_id) for row in self.settlements]
        if len(targets) != len(set(targets)):
            raise ValueError("Một chứng từ công nợ chỉ đối trừ một dòng trên mỗi hóa đơn")
        return self


class SalesInvoiceUpdate(SalesInvoiceIn):
    """PUT mang thêm `row_version` — khóa lạc quan (FR-NFR-005)."""

    row_version: int


class SalesInvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    description: str | None
    item_id: int | None
    unit_id: int | None
    warehouse_id: int | None
    quantity: Decimal | None
    unit_price_fc: Decimal | None
    discount_percent: Decimal | None
    discount_amount_fc: Decimal
    amount_fc: Decimal
    vat_rate: Decimal | None
    vat_amount_fc: Decimal
    account_id: int
    vat_account_id: int | None
    cogs_account_id: int | None
    inventory_account_id: int | None
    unit_cost_fc: Decimal | None
    price_list_id: int | None
    price_source: str | None
    cost_object_id: int | None
    project_id: int | None
    order_id: int | None
    contract_id: int | None
    expense_item_id: int | None
    extended_dimensions: dict[str, int] | None


class SalesSettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_kind: int
    target_id: UUID
    amount_fc: Decimal
    amount: Decimal
    fx_diff: Decimal


class SalesInvoiceOut(BaseModel):
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
    customer_id: int = 0
    salesperson_id: int | None = None
    ship_to: str | None = None
    recipient_name: str | None = None
    invoice_form: str | None = None
    invoice_serial: str | None = None
    invoice_no: str | None = None
    invoice_date: date | None = None
    payment_term_id: int | None = None
    due_date: date | None = None
    receivable_account_id: int = 0
    price_list_id: int | None = None
    is_stock_issue: bool = False
    cogs_posted: bool = False
    total_before_tax_fc: Decimal = _ZERO
    total_discount_fc: Decimal = _ZERO
    total_vat_fc: Decimal = _ZERO
    total_fc: Decimal = _ZERO

    lines: tuple[SalesInvoiceLineOut, ...] = ()
    settlements: tuple[SalesSettlementOut, ...] = ()
