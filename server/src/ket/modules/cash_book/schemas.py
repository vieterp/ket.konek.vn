"""Hình dạng request/response của phiếu thu/chi tiền mặt (SRS 03, lát 6B).

Pydantic ở mọi ranh giới API (ADR-015). Khác chứng từ nghiệp vụ khác một điểm
cấu trúc: dòng ở đây là **cặp Nợ/Có + một số tiền** (cách kế toán viên đọc một
phiếu — xem docstring `models.py`), và phiếu mang thêm danh sách **đối trừ
công nợ** (`docs/srs/03` §4). Cặp `amount` tùy chọn trên dòng là số quy đổi
client đang hiển thị — server so với `round_money(fc × rate)` rồi từ chối nếu
lệch (validator #9 phase-04), cùng vai `debit`/`credit` bên GLE.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.protocols import OpenInvoice, SettlementTargetKind
from ket.modules.cash_book.models import (
    DESCRIPTION_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    PAYER_RECEIVER_MAX_LENGTH,
    CashVoucherKind,
)
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE, PartnerKind

_ZERO = Decimal(0)

OPERATION_CODE_INPUT_MAX = 50


class ExtendedDimensionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: int
    value_id: int


class CashVoucherLineIn(BaseModel):
    """Một dòng định khoản: cặp Nợ/Có + số tiền nguyên tệ.

    Hai bên đều bỏ trống được **ở bản nháp** (nghiệp vụ "Thu khác" điền sẵn một
    bên); `posting_mapper` từ chối dịch dòng còn thiếu bên khi ghi sổ.
    """

    model_config = ConfigDict(extra="forbid")

    debit_account_id: int | None = None
    credit_account_id: int | None = None

    amount_fc: Decimal = Field(gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE)
    amount: Decimal | None = Field(
        default=None, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    """Số quy đổi client hiển thị — gửi thì server kiểm khớp luật làm tròn."""

    partner_id: int | None = None
    partner_kind: PartnerKind | None = None
    cost_object_id: int | None = None
    project_id: int | None = None
    order_id: int | None = None
    contract_id: int | None = None
    expense_item_id: int | None = None
    item_id: int | None = None
    warehouse_id: int | None = None
    bank_account_id: int | None = None
    extended: tuple[ExtendedDimensionIn, ...] = ()

    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)

    @model_validator(mode="after")
    def _pairs_sane(self) -> CashVoucherLineIn:
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        return self


class CashSettlementIn(BaseModel):
    """Một dòng đối trừ: chứng từ công nợ + số nguyên tệ người dùng nhập ("Số thu").

    Chỉ nhận `amount_fc` — số VND và chênh lệch tỷ giá do server tính từ tỷ giá
    phiếu và tỷ giá ghi nhận nợ (FR-SYS-066), không nhận từ client.
    """

    model_config = ConfigDict(extra="forbid")

    target_kind: SettlementTargetKind
    target_id: UUID
    amount_fc: Decimal = Field(gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE)


class CashVoucherIn(BaseModel):
    """Thân phiếu thu/chi cho cả tạo mới lẫn sửa (PUT gửi trọn bộ thay thế)."""

    model_config = ConfigDict(extra="forbid")

    kind: int = Field(ge=CashVoucherKind.RECEIPT, le=CashVoucherKind.PAYMENT)
    operation_code: str = Field(min_length=1, max_length=OPERATION_CODE_INPUT_MAX)
    cash_account_id: int

    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    exchange_rate: Decimal = Field(
        default=Decimal(1), max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )

    partner_id: int | None = None
    partner_kind: PartnerKind | None = None
    payer_receiver_name: str | None = Field(default=None, max_length=PAYER_RECEIVER_MAX_LENGTH)
    attachment_count: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=1000)
    cashflow_activity: int | None = None

    lines: tuple[CashVoucherLineIn, ...] = Field(min_length=1)
    settlements: tuple[CashSettlementIn, ...] = ()

    @model_validator(mode="after")
    def _voucher_sane(self) -> CashVoucherIn:
        if self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        targets = [(row.target_kind, row.target_id) for row in self.settlements]
        if len(targets) != len(set(targets)):
            # Cùng luật với UNIQUE của bảng, nhưng chặn ở cửa để lỗi nói tiếng
            # nghiệp vụ thay vì IntegrityError.
            raise ValueError("Một chứng từ công nợ chỉ đối trừ một dòng trên mỗi phiếu")
        return self


class CashVoucherUpdate(CashVoucherIn):
    """PUT mang thêm `row_version` — khóa lạc quan (FR-NFR-005)."""

    row_version: int


class CashVoucherLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    debit_account_id: int | None
    credit_account_id: int | None
    amount_fc: Decimal
    partner_id: int | None
    partner_kind: int | None
    cost_object_id: int | None
    project_id: int | None
    order_id: int | None
    contract_id: int | None
    expense_item_id: int | None
    item_id: int | None
    warehouse_id: int | None
    bank_account_id: int | None
    extended_dimensions: dict[str, int] | None
    description: str | None


class CashSettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_kind: int
    target_id: UUID
    amount_fc: Decimal
    amount: Decimal
    fx_diff: Decimal


class CashVoucherOut(BaseModel):
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
    cashflow_activity: int | None
    entry_kind: int
    created_at: datetime
    created_by: int
    posted_at: datetime | None
    posted_by: int | None
    row_version: int

    kind: int = 0
    operation_code: str = ""
    cash_account_id: int = 0
    partner_id: int | None = None
    partner_kind: int | None = None
    payer_receiver_name: str | None = None
    attachment_count: int | None = None
    treasurer_status: int = 0

    lines: tuple[CashVoucherLineOut, ...] = ()
    settlements: tuple[CashSettlementOut, ...] = ()


class OpenInvoiceOut(BaseModel):
    """Một chứng từ công nợ còn nợ cho picker đối trừ — chép phẳng từ
    `kernel.protocols.OpenInvoice` (hợp đồng API tách khỏi hợp đồng nội bộ)."""

    target_kind: int
    target_id: UUID
    partner_kind: int
    partner_id: int
    branch_id: int
    account_id: int
    invoice_no: str
    invoice_date: date
    due_date: date | None
    currency_code: str
    exchange_rate: Decimal
    amount_fc: Decimal
    remaining_fc: Decimal
    remaining: Decimal
    description: str | None

    @classmethod
    def from_invoice(cls, invoice: OpenInvoice) -> OpenInvoiceOut:
        return cls(**invoice.model_dump())


class OpenInvoicesResponse(BaseModel):
    items: tuple[OpenInvoiceOut, ...]


class CountSheetLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    denomination: Decimal = Field(
        gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    quantity: int = Field(gt=0)


class CountSheetIn(BaseModel):
    """Biên bản kiểm kê quỹ (FR-QUY-030): số đếm thật + chi tiết mệnh giá tùy chọn."""

    model_config = ConfigDict(extra="forbid")

    branch_id: int
    cash_account_id: int
    count_date: date
    counted_total: Decimal = Field(
        ge=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    lines: tuple[CountSheetLineIn, ...] = ()


class CountSheetLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    denomination: Decimal
    quantity: int


class CountSheetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    branch_id: int
    cash_account_id: int
    count_date: date
    book_balance: Decimal
    counted_total: Decimal
    difference: Decimal = Decimal(0)
    note: str | None
    adjustment_voucher_id: UUID | None
    created_by: int
    created_at: datetime
    lines: tuple[CountSheetLineOut, ...] = ()


class CountSheetListResponse(BaseModel):
    items: tuple[CountSheetOut, ...]
    total: int
    page: int
    page_size: int
