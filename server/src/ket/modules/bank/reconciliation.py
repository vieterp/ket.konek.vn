"""Đối chiếu sao kê ↔ sổ tiền gửi (bước 14, lát 6D — `docs/srs/04` §4.3, U5).

Đơn vị so khớp: **số tiền qua tài khoản** của một chứng từ = tổng phát sinh
ròng nguyên tệ trên các TK nhóm 112 của chứng từ đó, quy về TK ngân hàng đúng
như `movement_source` (BC/UNC/SEC → TK của thân chứng từ; CTNB → dòng Nợ thuộc
TK đích, dòng Có thuộc TK nguồn). Đọc từ `gl_postings` chứ không cộng dòng
định khoản: chứng từ có dòng phí lẫn bên trong thì con số qua tài khoản là số
ròng — đúng số ngân hàng in trên sao kê. Nguyên tệ (`debit_fc`/`credit_fc`) vì
sao kê viết bằng tiền tệ của tài khoản, và tiền tệ chứng từ tiền gửi đã bị 6C
ép khớp tiền tệ tài khoản.

Chỉ chứng từ **đã ghi sổ** vào được bàn khớp: "có trên sổ" của FR-BNK-030/031
nghĩa là sổ kế toán, và một phiếu nháp khớp sao kê xong bị xóa là một lỗ hổng
đối chiếu. Khớp — tự động lẫn tay — đòi số tiền **bằng nhau tuyệt đối**
(quyết định 6D): dòng ngân hàng gộp phí thì xử bằng chứng từ bổ sung cho khớp
số, không khớp cưỡng bức để báo cáo chênh lệch câm.

Khớp tự động (FR-BNK-030): cùng chiều + cùng số tiền + ngày hạch toán trong
±3 ngày quanh ngày giao dịch; trùng `reference_no` được ưu tiên; ứng viên
không phân định được (hai chứng từ giống hệt) thì ĐỂ LẠI cho người — khớp
nhầm đắt hơn chưa khớp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import ColumnElement, exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.errors import (
    BankStatementMatchInvalidError,
    BankStatementMatchStateError,
    BankStatementNotFoundError,
)
from ket.kernel.money import ZERO
from ket.modules.bank.models import (
    BankStatement,
    BankStatementLine,
    BankVoucher,
    StatementMatchKind,
)
from ket.modules.bank.posting_mapper import MONEY_ACCOUNT_CODE_PREFIXES
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.engine.models import GlPosting, Ledger

AUTO_MATCH_DATE_WINDOW_DAYS: Final[int] = 3
"""±3 ngày (`docs/srs/04` §4.3): lệnh chi lập cuối ngày ngân hàng xử lý hôm
sau, giao dịch cuối tuần vào sổ thứ hai — cùng con số phase file bước 14."""

_DEPOSIT_PREFIX: Final[str] = MONEY_ACCOUNT_CODE_PREFIXES[1]


@dataclass(frozen=True)
class MatchCandidate:
    """Một chứng từ chưa khớp, kèm số tiền ròng qua tài khoản đang đối chiếu."""

    voucher_id: UUID
    voucher_no: str
    posting_date: date
    kind: int
    reference_no: str | None
    description: str | None
    net_fc: Decimal
    """Dương = tiền vào tài khoản (khớp cột Có của sao kê), âm = tiền ra."""


@dataclass(frozen=True)
class AutoMatchOutcome:
    matched: int
    unmatched_lines: int
    ambiguous_lines: int
    """Dòng có ≥2 ứng viên không phân định được — chờ khớp tay (U5)."""


@dataclass(frozen=True)
class ReconciliationSummary:
    """Hai phía lệch của FR-BNK-031, tính đến hết `as_of`."""

    bank_account_id: int
    as_of: date
    unmatched_statement_lines: tuple[BankStatementLine, ...]
    unmatched_vouchers: tuple[MatchCandidate, ...]
    statement_total_unmatched_in: Decimal
    statement_total_unmatched_out: Decimal


def _attribution_filter(bank_account_id: int) -> ColumnElement[bool]:
    """Điều kiện "dòng sổ này thuộc TK ngân hàng X".

    Từ lát 6G-1 chỉ là phép so cột: chủ sở hữu được quy ngay lúc GHI sổ
    (`posting_mapper._deposit_owner`) và nằm ở `gl_postings.bank_account_id`.
    Bản cũ dựng lại luật CTNB ngay tại đây bằng `or_/and_` trên
    `bank_vouchers` — bản chép thứ tư của cùng một luật.
    """
    return GlPosting.bank_account_id == bank_account_id


def unmatched_vouchers(
    session: Session,
    *,
    bank_account_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[MatchCandidate, ...]:
    """Chứng từ tiền gửi đã ghi sổ, chưa khớp dòng sao kê nào, có tiền ròng
    qua tài khoản này — dưới phạm vi RLS của session (chi nhánh ngoài phạm vi
    không hiện ra, cùng bài 6C H-1)."""
    already_matched = exists(
        select(BankStatementLine.id).where(
            BankStatementLine.matched_voucher_id == Voucher.id,
            # Theo TÀI KHOẢN: chuyển nội bộ đã khớp bên sao kê nguồn vẫn phải
            # hiện là ứng viên bên sao kê đích (unique index cùng chiều này).
            BankStatementLine.bank_account_id == bank_account_id,
        )
    )
    query = (
        select(
            Voucher.id,
            Voucher.voucher_no,
            Voucher.posting_date,
            Voucher.description,
            BankVoucher.kind,
            BankVoucher.reference_no,
            func.sum(GlPosting.debit_fc - GlPosting.credit_fc).label("net_fc"),
        )
        .join(BankVoucher, BankVoucher.id == Voucher.id)
        .join(GlPosting, GlPosting.voucher_id == Voucher.id)
        .join(ChartOfAccount, ChartOfAccount.id == GlPosting.account_id)
        .where(
            Voucher.status == VoucherStatus.DA_GHI_SO,
            # Sao kê đối chiếu với SỔ TÀI CHÍNH: hai sổ ghi cùng dòng tiền
            # (LD-07), cộng cả hai là nhân đôi số tiền qua tài khoản.
            GlPosting.ledger == Ledger.FINANCIAL,
            ChartOfAccount.code.like(f"{_DEPOSIT_PREFIX}%"),
            _attribution_filter(bank_account_id),
            ~already_matched,
        )
        .group_by(
            Voucher.id,
            Voucher.voucher_no,
            Voucher.posting_date,
            Voucher.description,
            BankVoucher.kind,
            BankVoucher.reference_no,
        )
        .order_by(Voucher.posting_date, Voucher.voucher_no)
    )
    if date_from is not None:
        query = query.where(Voucher.posting_date >= date_from)
    if date_to is not None:
        query = query.where(Voucher.posting_date <= date_to)
    rows = session.execute(query).all()
    return tuple(
        MatchCandidate(
            voucher_id=row.id,
            voucher_no=row.voucher_no,
            posting_date=row.posting_date,
            kind=row.kind,
            reference_no=row.reference_no,
            description=row.description,
            net_fc=row.net_fc,
        )
        for row in rows
        if row.net_fc != ZERO
    )


def _line_amount(line: BankStatementLine) -> Decimal:
    """Số tiền có dấu của một dòng sao kê — dương = tiền vào, như `net_fc`."""
    return line.credit - line.debit


def auto_match(session: Session, *, statement_id: UUID) -> AutoMatchOutcome:
    """Khớp tự động mọi dòng chưa khớp của một sao kê (FR-BNK-030)."""
    statement = session.get(BankStatement, statement_id)
    if statement is None:
        raise BankStatementNotFoundError("Không tìm thấy sao kê", statement_id=str(statement_id))
    lines = (
        session.execute(
            select(BankStatementLine)
            .where(
                BankStatementLine.statement_id == statement_id,
                BankStatementLine.match_kind == StatementMatchKind.UNMATCHED,
            )
            .order_by(BankStatementLine.line_no)
            # Hai người cùng bấm "Khớp tự động": khóa dòng để lượt sau chờ lượt
            # trước, thay vì hai lượt cùng gán một chứng từ cho hai dòng và một
            # bên đổ vì unique index — 409 khó hiểu hơn một giây chờ.
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if not lines:
        return AutoMatchOutcome(matched=0, unmatched_lines=0, ambiguous_lines=0)

    window = timedelta(days=AUTO_MATCH_DATE_WINDOW_DAYS)
    candidates = unmatched_vouchers(
        session,
        bank_account_id=statement.bank_account_id,
        date_from=min(line.txn_date for line in lines) - window,
        date_to=max(line.txn_date for line in lines) + window,
    )

    consumed: set[UUID] = set()
    matched = 0
    ambiguous = 0
    for line in lines:
        amount = _line_amount(line)
        if amount == ZERO:
            continue
        pool = [
            candidate
            for candidate in candidates
            if candidate.voucher_id not in consumed
            and candidate.net_fc == amount
            and abs((candidate.posting_date - line.txn_date).days) <= AUTO_MATCH_DATE_WINDOW_DAYS
        ]
        by_reference = [
            candidate
            for candidate in pool
            if line.reference_no
            and candidate.reference_no
            and candidate.reference_no == line.reference_no
        ]
        chosen: MatchCandidate | None = None
        if len(by_reference) == 1:
            chosen = by_reference[0]
        elif not by_reference and len(pool) == 1:
            chosen = pool[0]
        elif pool:
            ambiguous += 1
        if chosen is None:
            continue
        line.matched_voucher_id = chosen.voucher_id
        line.match_kind = StatementMatchKind.AUTO
        consumed.add(chosen.voucher_id)
        matched += 1
    _flush_matches(session)
    return AutoMatchOutcome(
        matched=matched,
        unmatched_lines=len(lines) - matched - ambiguous,
        ambiguous_lines=ambiguous,
    )


def ensure_not_matched_to_statement(session: Session, *, voucher_id: UUID) -> None:
    """Chặn bỏ ghi sổ chứng từ đã khớp sao kê (review 6D, H-3).

    Đối xứng với chiều xóa (FK RESTRICT): dòng sao kê "đã khớp" trỏ phiếu nháp
    làm báo cáo FR-BNK-031 câm ở CẢ HAI phía, và phiếu nháp sửa được số tiền
    rồi ghi sổ lại — vòng qua trọn luật khớp-bằng-tuyệt-đối. Gọi từ CẢ HAI
    cửa: hook `after_unpost` của endpoint hành động chung, và
    `BankVoucherService.unpost` (đường dịch vụ).
    """
    matched = session.execute(
        select(BankStatementLine.id)
        .where(BankStatementLine.matched_voucher_id == voucher_id)
        .limit(1)
    ).first()
    if matched is not None:
        raise BankStatementMatchStateError(
            "Chứng từ đã khớp với dòng sao kê — gỡ khớp ở màn đối chiếu trước khi bỏ ghi sổ",
            voucher_id=str(voucher_id),
        )


def _require_line(session: Session, line_id: UUID) -> BankStatementLine:
    line = session.execute(
        select(BankStatementLine).where(BankStatementLine.id == line_id).with_for_update()
    ).scalar_one_or_none()
    if line is None:
        raise BankStatementNotFoundError("Không tìm thấy dòng sao kê", line_id=str(line_id))
    return line


def match_line(session: Session, *, line_id: UUID, voucher_id: UUID) -> None:
    """Khớp tay một dòng sao kê với một chứng từ (FR-BNK-030, U5).

    Vẫn đòi đúng TK ngân hàng, đúng chiều, đúng số tiền — khớp tay chỉ nới
    **cửa sổ ngày** (giao dịch treo lâu hơn ±3 ngày là có thật); số tiền lệch
    thì đường đúng là lập chứng từ bổ sung, không phải ép khớp.
    """
    line = _require_line(session, line_id)
    if line.match_kind != StatementMatchKind.UNMATCHED:
        raise BankStatementMatchStateError("Dòng sao kê này đã khớp rồi — gỡ khớp trước nếu nhầm")
    statement = session.get(BankStatement, line.statement_id)
    if statement is None:  # pragma: no cover - FK bảo đảm
        raise BankStatementNotFoundError("Sao kê của dòng không còn")

    amount = _line_amount(line)
    if amount == ZERO:
        raise BankStatementMatchInvalidError("Dòng sao kê không có số tiền để khớp")

    already = session.execute(
        select(func.count())
        .select_from(BankStatementLine)
        .where(
            BankStatementLine.matched_voucher_id == voucher_id,
            BankStatementLine.bank_account_id == statement.bank_account_id,
        )
    ).scalar_one()
    if already:
        raise BankStatementMatchStateError(
            "Chứng từ này đã khớp vào một dòng sao kê khác của cùng tài khoản",
            voucher_id=str(voucher_id),
        )

    candidate = _candidate_of(
        session, bank_account_id=statement.bank_account_id, voucher_id=voucher_id
    )
    if candidate is None:
        raise BankStatementMatchInvalidError(
            "Chứng từ không phải chứng từ tiền gửi đã ghi sổ của tài khoản này",
            voucher_id=str(voucher_id),
        )
    if candidate.net_fc != amount:
        raise BankStatementMatchInvalidError(
            "Số tiền chứng từ không bằng số tiền dòng sao kê — lập chứng từ bổ sung "
            "cho phần lệch thay vì ép khớp",
            voucher_amount=str(candidate.net_fc),
            statement_amount=str(amount),
        )
    line.matched_voucher_id = voucher_id
    line.match_kind = StatementMatchKind.MANUAL
    _flush_matches(session)


MATCHED_VOUCHER_UNIQUE_INDEX = "uq_bank_statement_lines_matched_voucher"


def _flush_matches(session: Session) -> None:
    """Đẩy khớp xuống DB, đổi đua unique-index thành 409 đọc được.

    Khóa `FOR UPDATE` chỉ phủ dòng sao kê của MỘT sao kê — hai lượt khớp trên
    hai sao kê khác nhau vẫn giành được cùng một chứng từ, và bên thua đâm vào
    `uq_bank_statement_lines_matched_voucher`. Đó là kết quả đúng (một chứng
    từ một dòng mỗi tài khoản), chỉ cần kể bằng lỗi nghiệp vụ thay vì 500.

    Chỉ dịch ĐÚNG ràng buộc ấy (nợ L-2 của 6D): bản cũ nuốt mọi
    `IntegrityError`, nên một FK gãy hay một CHECK vi phạm — lỗi lập trình, phải
    nổ to — cũng được kể thành "ai đó vừa lấy mất chứng từ" và người dùng bấm
    lại mãi không hiểu. Nhận diện qua tên ràng buộc của psycopg chứ không qua
    chuỗi thông báo: thông báo đổi theo locale của máy chủ.
    """
    try:
        session.flush()
    except IntegrityError as error:
        if _violated_constraint(error) != MATCHED_VOUCHER_UNIQUE_INDEX:
            raise
        raise BankStatementMatchStateError(
            "Một lượt khớp khác vừa lấy mất chứng từ — tải lại màn hình đối chiếu rồi thử lại"
        ) from error


def _violated_constraint(error: IntegrityError) -> str | None:
    """Tên ràng buộc bị vi phạm, `None` khi driver không nói (không phải psycopg,
    hoặc lỗi không mang `diag`)."""
    diagnostic = getattr(error.orig, "diag", None)
    name = getattr(diagnostic, "constraint_name", None)
    return name if isinstance(name, str) else None


def _candidate_of(
    session: Session, *, bank_account_id: int, voucher_id: UUID
) -> MatchCandidate | None:
    """Ứng viên hóa MỘT chứng từ — cùng truy vấn với `unmatched_vouchers` nhưng
    không loại trừ đã-khớp (người gọi tự kiểm để trả 409 thay vì 422)."""
    row = session.execute(
        select(
            Voucher.id,
            Voucher.voucher_no,
            Voucher.posting_date,
            Voucher.description,
            BankVoucher.kind,
            BankVoucher.reference_no,
            func.sum(GlPosting.debit_fc - GlPosting.credit_fc).label("net_fc"),
        )
        .join(BankVoucher, BankVoucher.id == Voucher.id)
        .join(GlPosting, GlPosting.voucher_id == Voucher.id)
        .join(ChartOfAccount, ChartOfAccount.id == GlPosting.account_id)
        .where(
            Voucher.id == voucher_id,
            Voucher.status == VoucherStatus.DA_GHI_SO,
            GlPosting.ledger == Ledger.FINANCIAL,
            ChartOfAccount.code.like(f"{_DEPOSIT_PREFIX}%"),
            _attribution_filter(bank_account_id),
        )
        .group_by(
            Voucher.id,
            Voucher.voucher_no,
            Voucher.posting_date,
            Voucher.description,
            BankVoucher.kind,
            BankVoucher.reference_no,
        )
    ).one_or_none()
    if row is None or row.net_fc == ZERO:
        return None
    return MatchCandidate(
        voucher_id=row.id,
        voucher_no=row.voucher_no,
        posting_date=row.posting_date,
        kind=row.kind,
        reference_no=row.reference_no,
        description=row.description,
        net_fc=row.net_fc,
    )


def unmatch_line(session: Session, *, line_id: UUID) -> None:
    """Gỡ khớp một dòng — thao tác có nhật ký (listener ghi diff hai cột khớp)."""
    line = _require_line(session, line_id)
    if line.match_kind == StatementMatchKind.UNMATCHED:
        raise BankStatementMatchStateError("Dòng sao kê này chưa khớp — không có gì để gỡ")
    line.matched_voucher_id = None
    line.match_kind = StatementMatchKind.UNMATCHED
    session.flush()


def candidates_for_line(
    session: Session, *, line_id: UUID, limit: int = 20
) -> tuple[MatchCandidate, ...]:
    """Gợi ý ghép cho một dòng chưa khớp (U5): đúng chiều + đúng số tiền, xếp
    theo khoảng cách ngày — cửa sổ ngày KHÔNG chặn ở đây, vì đây chính là chỗ
    người dùng xử các giao dịch treo lâu."""
    line = session.get(BankStatementLine, line_id)
    if line is None:
        raise BankStatementNotFoundError("Không tìm thấy dòng sao kê", line_id=str(line_id))
    statement = session.get(BankStatement, line.statement_id)
    if statement is None:  # pragma: no cover - FK bảo đảm
        raise BankStatementNotFoundError("Sao kê của dòng không còn")
    amount = _line_amount(line)
    if amount == ZERO:
        return ()
    pool = [
        candidate
        for candidate in unmatched_vouchers(session, bank_account_id=statement.bank_account_id)
        if candidate.net_fc == amount
    ]
    pool.sort(
        key=lambda candidate: (
            abs((candidate.posting_date - line.txn_date).days),
            candidate.voucher_no,
        )
    )
    return tuple(pool[:limit])


def reconciliation_summary(
    session: Session, *, bank_account_id: int, as_of: date
) -> ReconciliationSummary:
    """Hai phía lệch (FR-BNK-031): trên sao kê chưa có trên sổ, và ngược lại."""
    lines = (
        session.execute(
            select(BankStatementLine)
            .join(BankStatement, BankStatement.id == BankStatementLine.statement_id)
            .where(
                BankStatement.bank_account_id == bank_account_id,
                BankStatementLine.txn_date <= as_of,
                BankStatementLine.match_kind == StatementMatchKind.UNMATCHED,
            )
            .order_by(BankStatementLine.txn_date, BankStatementLine.line_no)
        )
        .scalars()
        .all()
    )
    vouchers = unmatched_vouchers(session, bank_account_id=bank_account_id, date_to=as_of)
    total_in = sum((line.credit for line in lines), ZERO)
    total_out = sum((line.debit for line in lines), ZERO)
    return ReconciliationSummary(
        bank_account_id=bank_account_id,
        as_of=as_of,
        unmatched_statement_lines=tuple(lines),
        unmatched_vouchers=vouchers,
        statement_total_unmatched_in=total_in,
        statement_total_unmatched_out=total_out,
    )
