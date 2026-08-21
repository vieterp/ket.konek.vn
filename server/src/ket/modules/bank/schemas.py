"""Hình dạng request/response của chứng từ tiền gửi (SRS 04, lát 6C).

Cùng khuôn `cash_book/schemas.py`: dòng là **cặp Nợ/Có + một số tiền**, chứng
từ mang danh sách **đối trừ công nợ** (FR-BNK-007 — giống cơ chế phân hệ Quỹ).
Khác phiếu quỹ ba điểm cấu trúc:

* bốn loại chứng từ (`BankVoucherKind`) trên một thân; chuyển tiền nội bộ
  KHÔNG có nghiệp vụ (`operation_code = None`) nhưng bắt buộc TK đích;
* mọi chứng từ gắn **tài khoản ngân hàng doanh nghiệp cụ thể** (FR-BNK-002),
  không chỉ TK kế toán 112;
* ủy nhiệm chi chụp bộ ba người thụ hưởng thành chữ (FR-BNK-003/FR-SYS-033),
  séc mang số séc + ngày séc (`docs/srs/04` §2).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.master_data.models.employee import BANK_ACCOUNT_MAX_LENGTH
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.bank.models import (
    BANK_NAME_MAX_LENGTH,
    BENEFICIARY_NAME_MAX_LENGTH,
    CHEQUE_NO_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    REFERENCE_NO_MAX_LENGTH,
    BankVoucherKind,
)
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE, PartnerKind

_ZERO = Decimal(0)

OPERATION_CODE_INPUT_MAX = 50


class BankExtendedDimensionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: int
    value_id: int


class BankVoucherLineIn(BaseModel):
    """Một dòng định khoản: cặp Nợ/Có + số tiền nguyên tệ — cùng luật dòng
    phiếu quỹ (hai bên bỏ trống được ở bản nháp, mapper chặn lúc ghi sổ)."""

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
    extended: tuple[BankExtendedDimensionIn, ...] = ()

    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)

    @model_validator(mode="after")
    def _pairs_sane(self) -> BankVoucherLineIn:
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        return self


class BankSettlementIn(BaseModel):
    """Một dòng đối trừ — chỉ nhận `amount_fc`, VND và chênh lệch tỷ giá do
    server tính (cùng luật `CashSettlementIn`)."""

    model_config = ConfigDict(extra="forbid")

    target_kind: SettlementTargetKind
    target_id: UUID
    amount_fc: Decimal = Field(gt=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE)


class BankVoucherIn(BaseModel):
    """Thân chứng từ tiền gửi cho cả tạo mới lẫn sửa (PUT gửi trọn bộ thay thế)."""

    model_config = ConfigDict(extra="forbid")

    kind: int = Field(ge=BankVoucherKind.CREDIT_ADVICE, le=BankVoucherKind.INTERNAL_TRANSFER)
    operation_code: str | None = Field(
        default=None, min_length=1, max_length=OPERATION_CODE_INPUT_MAX
    )
    bank_account_id: int
    counter_bank_account_id: int | None = None

    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    exchange_rate: Decimal = Field(
        default=Decimal(1), max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )

    partner_id: int | None = None
    partner_kind: PartnerKind | None = None

    beneficiary_name: str | None = Field(default=None, max_length=BENEFICIARY_NAME_MAX_LENGTH)
    beneficiary_account_no: str | None = Field(default=None, max_length=BANK_ACCOUNT_MAX_LENGTH)
    beneficiary_bank_name: str | None = Field(default=None, max_length=BANK_NAME_MAX_LENGTH)

    cheque_no: str | None = Field(default=None, max_length=CHEQUE_NO_MAX_LENGTH)
    cheque_date: date | None = None

    reference_no: str | None = Field(default=None, max_length=REFERENCE_NO_MAX_LENGTH)
    description: str | None = Field(default=None, max_length=1000)
    cashflow_activity: int | None = None

    lines: tuple[BankVoucherLineIn, ...] = Field(min_length=1)
    settlements: tuple[BankSettlementIn, ...] = ()

    @model_validator(mode="after")
    def _voucher_sane(self) -> BankVoucherIn:
        if self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        transfer = self.kind == BankVoucherKind.INTERNAL_TRANSFER
        # Cùng luật với CHECK `operation_iff_not_transfer`/`counter_account_iff_transfer`
        # của bảng — chặn ở cửa để lỗi nói tiếng nghiệp vụ thay vì IntegrityError.
        if transfer and self.operation_code is not None:
            raise ValueError("Chuyển tiền nội bộ không có nghiệp vụ định khoản")
        if not transfer and self.operation_code is None:
            raise ValueError("Chứng từ tiền gửi phải chọn nghiệp vụ định khoản")
        if transfer != (self.counter_bank_account_id is not None):
            raise ValueError("Chỉ chuyển tiền nội bộ mới có (và phải có) TK ngân hàng đích")
        if transfer and self.counter_bank_account_id == self.bank_account_id:
            raise ValueError("TK ngân hàng đích phải khác TK nguồn")
        if transfer and self.settlements:
            raise ValueError("Chuyển tiền nội bộ không đối trừ công nợ")
        if self.kind != BankVoucherKind.CHEQUE and self.cheque_no is not None:
            raise ValueError("Số séc chỉ có trên chứng từ séc")
        if (self.cheque_no is None) != (self.cheque_date is None):
            raise ValueError("Số séc và ngày séc phải cùng có hoặc cùng vắng")
        targets = [(row.target_kind, row.target_id) for row in self.settlements]
        if len(targets) != len(set(targets)):
            raise ValueError("Một chứng từ công nợ chỉ đối trừ một dòng trên mỗi chứng từ")
        return self


class BankVoucherUpdate(BankVoucherIn):
    """PUT mang thêm `row_version` — khóa lạc quan (FR-NFR-005)."""

    row_version: int


class BankVoucherLineOut(BaseModel):
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
    extended_dimensions: dict[str, int] | None
    description: str | None


class BankSettlementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    target_kind: int
    target_id: UUID
    amount_fc: Decimal
    amount: Decimal
    fx_diff: Decimal


class BankVoucherOut(BaseModel):
    """Header chứng từ + thân tiền gửi — client cần cả hai để vẽ lại form."""

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
    operation_code: str | None = None
    bank_account_id: int = 0
    counter_bank_account_id: int | None = None
    partner_id: int | None = None
    partner_kind: int | None = None
    beneficiary_name: str | None = None
    beneficiary_account_no: str | None = None
    beneficiary_bank_name: str | None = None
    cheque_no: str | None = None
    cheque_date: date | None = None
    reference_no: str | None = None

    lines: tuple[BankVoucherLineOut, ...] = ()
    settlements: tuple[BankSettlementOut, ...] = ()
