"""Hợp đồng vào của posting engine — thứ module nghiệp vụ dựng và đưa sang.

Module **không** chạm `gl_postings`; nó dịch chi tiết chứng từ của mình thành
`PostingRequest` rồi gọi `PostingService` (luật phụ thuộc #3). Số tiền ở đây là
**nguyên tệ + tỷ giá** — số quy đổi do engine tự tính bằng `round_money`, không
nhận từ ngoài, để không client hay module nào tự làm tròn theo luật riêng
(FR-NFR-001/002, validator #9 của phase-04).
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.posting.engine.dimensions import PostingDimensions
from ket.posting.engine.models import AMOUNT_PRECISION, AMOUNT_SCALE

_ZERO = Decimal(0)


class PostingLine(BaseModel):
    """Một dòng định khoản mà module đề nghị ghi vào một sổ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: int
    corresponding_account_id: int | None = None

    # Ràng đúng độ chính xác của cột `NUMERIC(18,2)`/`(18,6)`: một số lẻ hơn
    # sẽ bị DB làm tròn **âm thầm** lúc Cất rồi đổ `posting.unbalanced` khó
    # hiểu lúc ghi sổ (review 4A, M1). Chặn ở biên với thông điệp chỉ đúng chỗ.
    debit_fc: Decimal = Field(
        default=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    credit_fc: Decimal = Field(
        default=_ZERO, max_digits=AMOUNT_PRECISION, decimal_places=AMOUNT_SCALE
    )
    currency: str = Field(min_length=CURRENCY_CODE_LENGTH, max_length=CURRENCY_CODE_LENGTH)
    rate: Decimal = Field(max_digits=RATE_PRECISION, decimal_places=RATE_SCALE_DEFAULT)

    dimensions: PostingDimensions = PostingDimensions()
    source_line_id: UUID | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _amounts_sane(self) -> PostingLine:
        """Chặn ở biên những dòng không bao giờ hợp lệ, trước cả validator.

        Validator ghi sổ trả **danh sách** lỗi cho những gì người dùng sửa
        được trên chứng từ; còn số âm hay Nợ-lẫn-Có trên một dòng là lỗi của
        **module** dựng request — nổ ngay tại chỗ dựng dễ truy hơn nổ gộp.
        """
        if self.debit_fc < _ZERO or self.credit_fc < _ZERO:
            raise ValueError("Số tiền trên dòng định khoản không được âm")
        if self.debit_fc > _ZERO and self.credit_fc > _ZERO:
            raise ValueError("Một dòng không được vừa Nợ vừa Có")
        if self.rate <= _ZERO:
            raise ValueError("Tỷ giá phải dương")
        return self


class PostingRequest(BaseModel):
    """Đề nghị ghi sổ cho một chứng từ — đủ cho cả hai hệ thống sổ (N3).

    `management_lines is None` nghĩa là "sổ quản trị ghi y như sổ tài chính" —
    engine tự nhân bản. Đưa danh sách riêng khi hai sổ khác nhau (giá thành,
    lương); đưa danh sách **rỗng** khi chứng từ không lên sổ quản trị.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    voucher_id: UUID
    financial_lines: tuple[PostingLine, ...]
    management_lines: tuple[PostingLine, ...] | None = None
