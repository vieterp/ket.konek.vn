"""Hình dạng request/response của sao kê + đối chiếu ngân hàng (lát 6D)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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
    kind: int
    reference_no: str | None
    description: str | None
    net_fc: Decimal

    @classmethod
    def from_candidate(cls, candidate: MatchCandidate) -> MatchCandidateOut:
        return cls(
            voucher_id=candidate.voucher_id,
            voucher_no=candidate.voucher_no,
            posting_date=candidate.posting_date,
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
