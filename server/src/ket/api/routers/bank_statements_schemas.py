"""Hình dạng request/response của sao kê + đối chiếu ngân hàng (lát 6D)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ket.kernel.bank_import.profile_models import (
    COLUMN_NAME_MAX_LENGTH as PROFILE_COLUMN_MAX_LENGTH,
)
from ket.kernel.bank_import.profile_models import (
    DATE_FORMAT_MAX_LENGTH as PROFILE_DATE_FORMAT_MAX_LENGTH,
)
from ket.kernel.bank_import.profile_models import (
    NAME_MAX_LENGTH as PROFILE_NAME_MAX_LENGTH,
)
from ket.kernel.bank_import.profile_models import (
    AmountSignRule,
    StatementFileKind,
)
from ket.modules.bank.models import BankStatement, BankStatementLine
from ket.modules.bank.reconciliation import (
    AutoMatchOutcome,
    MatchCandidate,
    ReconciliationSummary,
)
from ket.modules.bank.statement_import import ImportedStatement


class BankStatementOut(BaseModel):
    """Header một sao kê đã nhập."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    bank_account_id: int
    statement_date: date
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    profile_id: int | None
    content_hash: str | None
    imported_by: int
    imported_at: datetime


class BankStatementImportOut(BaseModel):
    """Kết quả một lượt nhập — con số màn hình đối chiếu hiện ngay."""

    statement: BankStatementOut
    line_count: int
    total_credit: Decimal
    total_debit: Decimal

    @classmethod
    def from_result(cls, result: ImportedStatement) -> BankStatementImportOut:
        return cls(
            statement=BankStatementOut.model_validate(result.statement),
            line_count=result.line_count,
            total_credit=result.total_credit,
            total_debit=result.total_debit,
        )


class BankStatementListResponse(BaseModel):
    items: tuple[BankStatementOut, ...]


class BankStatementProfileOut(BaseModel):
    """Một hồ sơ định dạng sao kê — chỉ phần ô chọn cần: cách đọc cột là việc
    của server lúc nhập, client không diễn giải."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bank_id: int
    name: str


class BankStatementProfileDetailOut(BaseModel):
    """Trọn hồ sơ — thân của màn KHAI hồ sơ (lát 6G-2).

    Tách khỏi `BankStatementProfileOut` chứ không mở rộng nó: ô chọn hồ sơ trên
    màn nhập sao kê chỉ cần `id`/`name`, và bơm hai chục cột cách-đọc-tệp vào
    mọi lượt tải màn ấy là trả tiền cho thứ không ai đọc."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    bank_id: int
    name: str
    file_kind: str
    header_row: int
    date_col: str
    date_format: str
    debit_col: str | None
    credit_col: str | None
    amount_col: str | None
    sign_rule: str | None
    ref_col: str | None
    description_col: str | None
    balance_col: str | None
    decimal_sep: str
    thousand_sep: str | None
    csv_delimiter: str | None
    row_version: int


class BankStatementProfileIn(BaseModel):
    """Thân khai/sửa một hồ sơ.

    Không kiểm chéo ở đây (một-trong-hai hình dạng cột tiền, ba dấu phải khác
    nhau đôi một): những luật ấy là `CHECK` trên bảng từ lát 3C-2, và nhân đôi
    chúng ở tầng schema là nhận hai bản có thể trôi khỏi nhau. Tầng này chỉ
    canh kiểu và độ dài — thứ DB trả về dưới dạng lỗi khó đọc."""

    model_config = ConfigDict(extra="forbid")

    bank_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=PROFILE_NAME_MAX_LENGTH)
    file_kind: StatementFileKind
    header_row: int = Field(ge=1)
    """Chỉ `ge=1` — đúng bằng `CHECK (header_row >= 1)` của bảng. Thêm một trần
    trên ở đây sẽ là một luật CHỈ tầng API biết, và tệp sao kê mở đầu bằng hai
    chục dòng tiêu đề là chuyện có thật."""
    date_col: str = Field(min_length=1, max_length=PROFILE_COLUMN_MAX_LENGTH)
    date_format: str = Field(min_length=1, max_length=PROFILE_DATE_FORMAT_MAX_LENGTH)
    debit_col: str | None = Field(default=None, max_length=PROFILE_COLUMN_MAX_LENGTH)
    credit_col: str | None = Field(default=None, max_length=PROFILE_COLUMN_MAX_LENGTH)
    amount_col: str | None = Field(default=None, max_length=PROFILE_COLUMN_MAX_LENGTH)
    sign_rule: AmountSignRule | None = None
    ref_col: str | None = Field(default=None, max_length=PROFILE_COLUMN_MAX_LENGTH)
    description_col: str | None = Field(default=None, max_length=PROFILE_COLUMN_MAX_LENGTH)
    balance_col: str | None = Field(default=None, max_length=PROFILE_COLUMN_MAX_LENGTH)
    decimal_sep: str = Field(min_length=1, max_length=1)
    thousand_sep: str | None = Field(default=None, min_length=1, max_length=1)
    csv_delimiter: str | None = Field(default=None, min_length=1, max_length=1)


class BankStatementProfileUpdateIn(BankStatementProfileIn):
    """Sửa = khai + số phiên bản dòng (khóa lạc quan, cùng khuôn danh mục)."""

    row_version: int = Field(ge=1)


class BankStatementProfileListResponse(BaseModel):
    items: tuple[BankStatementProfileOut, ...]


class BankStatementProfileDetailListResponse(BaseModel):
    items: tuple[BankStatementProfileDetailOut, ...]


class BankStatementLineOut(BaseModel):
    """Một dòng sao kê + trạng thái khớp (U5: dòng đã khớp mờ đi)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    line_no: int
    txn_date: date
    reference_no: str | None
    description: str | None
    debit: Decimal
    credit: Decimal
    matched_voucher_id: UUID | None
    match_kind: int


class BankStatementDetailResponse(BaseModel):
    statement: BankStatementOut
    lines: tuple[BankStatementLineOut, ...]

    @classmethod
    def from_rows(
        cls, statement: BankStatement, lines: list[BankStatementLine]
    ) -> BankStatementDetailResponse:
        return cls(
            statement=BankStatementOut.model_validate(statement),
            lines=tuple(BankStatementLineOut.model_validate(line) for line in lines),
        )


class AutoMatchResponse(BaseModel):
    matched: int
    unmatched_lines: int
    ambiguous_lines: int

    @classmethod
    def from_outcome(cls, outcome: AutoMatchOutcome) -> AutoMatchResponse:
        return cls(
            matched=outcome.matched,
            unmatched_lines=outcome.unmatched_lines,
            ambiguous_lines=outcome.ambiguous_lines,
        )


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voucher_id: UUID


class MatchCandidateOut(BaseModel):
    """Một chứng từ gợi ý ghép — `net_fc` dương = tiền vào tài khoản."""

    voucher_id: UUID
    voucher_no: str
    posting_date: date
    document_type: str
    """Mã loại chứng từ — bàn khớp nhận mọi loại chạm 112x từ 6G-2 (M-3), nên
    client dựng nhãn dòng từ đây chứ không từ `kind`."""
    kind: int | None
    """Loại chứng từ tiền gửi; `None` với phiếu quỹ / bút toán GLE."""
    reference_no: str | None
    description: str | None
    net_fc: Decimal

    @classmethod
    def from_candidate(cls, candidate: MatchCandidate) -> MatchCandidateOut:
        return cls(
            voucher_id=candidate.voucher_id,
            voucher_no=candidate.voucher_no,
            posting_date=candidate.posting_date,
            document_type=candidate.document_type,
            kind=candidate.kind,
            reference_no=candidate.reference_no,
            description=candidate.description,
            net_fc=candidate.net_fc,
        )


class MatchCandidatesResponse(BaseModel):
    items: tuple[MatchCandidateOut, ...]


class ReconciliationResponse(BaseModel):
    """Hai phía lệch của FR-BNK-031 tính đến hết `as_of`."""

    bank_account_id: int
    as_of: date
    unmatched_statement_lines: tuple[BankStatementLineOut, ...]
    unmatched_vouchers: tuple[MatchCandidateOut, ...]
    statement_total_unmatched_in: Decimal
    statement_total_unmatched_out: Decimal

    @classmethod
    def from_summary(cls, summary: ReconciliationSummary) -> ReconciliationResponse:
        return cls(
            bank_account_id=summary.bank_account_id,
            as_of=summary.as_of,
            unmatched_statement_lines=tuple(
                BankStatementLineOut.model_validate(line)
                for line in summary.unmatched_statement_lines
            ),
            unmatched_vouchers=tuple(
                MatchCandidateOut.from_candidate(candidate)
                for candidate in summary.unmatched_vouchers
            ),
            statement_total_unmatched_in=summary.statement_total_unmatched_in,
            statement_total_unmatched_out=summary.statement_total_unmatched_out,
        )
