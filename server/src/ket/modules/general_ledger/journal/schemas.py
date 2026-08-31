"""Hình dạng request/response của chứng từ nghiệp vụ khác (FR-GLE-001).

Pydantic ở mọi ranh giới API (ADR-015). Điểm đáng nói duy nhất là cặp
`debit`/`credit` **tùy chọn**: client có thể gửi số quy đổi nó đang hiển thị,
và server so với `round_money(fc × rate)` rồi từ chối nếu lệch (validator #9
phase-04 — chống client tự tính theo luật làm tròn riêng). Không gửi thì
server tự tính; sự thật ghi sổ luôn là số server tính.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE, EntryKind, PartnerKind

_ZERO = Decimal(0)


class ExtendedDimensionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: int
    value_id: int


class JournalLineIn(BaseModel):
    """Một dòng định khoản gửi lên — nguyên tệ + tỷ giá là bắt buộc."""

    model_config = ConfigDict(extra="forbid")

    account_id: int
    corresponding_account_id: int | None = None

    currency_code: str | None = Field(
        default=None, min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH
    )
    """Bỏ trống = dùng đồng tiền của chứng từ."""
    exchange_rate: Decimal | None = Field(
        default=None, max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )
    """Bỏ trống = dùng tỷ giá của chứng từ."""

    # Ràng scale theo cột `NUMERIC(18,2)` — số lẻ hơn bị DB làm tròn âm thầm
    # lúc Cất rồi lệch cân đối lúc ghi sổ (review 4A, M1).
    debit_fc: Decimal = Field(
        default=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    credit_fc: Decimal = Field(
        default=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    debit: Decimal | None = Field(
        default=None, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    credit: Decimal | None = Field(
        default=None, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )

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

    description: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _amounts_sane(self) -> JournalLineIn:
        if self.debit_fc < _ZERO or self.credit_fc < _ZERO:
            raise ValueError("Số tiền không được âm")
        if self.debit_fc > _ZERO and self.credit_fc > _ZERO:
            raise ValueError("Một dòng không được vừa Nợ vừa Có")
        if self.exchange_rate is not None and self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        return self


class JournalVoucherIn(BaseModel):
    """Thân chứng từ cho cả tạo mới lẫn sửa (PUT gửi trọn bộ dòng thay thế)."""

    model_config = ConfigDict(extra="forbid")

    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    exchange_rate: Decimal = Field(
        default=Decimal(1), max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT
    )
    description: str | None = Field(default=None, max_length=1000)
    cashflow_activity: int | None = None
    entry_kind: int = Field(default=EntryKind.NGHIEP_VU, ge=0, le=int(EntryKind.KET_CHUYEN))
    """Bản chất bút toán (LD-17). Chứng từ nghiệp vụ khác (`GLE`) là nơi kế
    toán ghi **kết chuyển thủ công** trước khi có engine kết chuyển (10a), nên
    đây là màn hình đầu tiên cần khai cờ này — B02 lọc theo nó."""

    lines: tuple[JournalLineIn, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _rate_positive(self) -> JournalVoucherIn:
        if self.exchange_rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        return self


class JournalVoucherUpdate(JournalVoucherIn):
    """PUT mang thêm `row_version` — khóa lạc quan (FR-NFR-005)."""

    row_version: int


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    account_id: int
    corresponding_account_id: int | None
    currency_code: str
    exchange_rate: Decimal
    debit_fc: Decimal
    credit_fc: Decimal
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


class JournalVoucherOut(BaseModel):
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
    lines: tuple[JournalLineOut, ...] = ()
