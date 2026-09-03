"""`PartnerDebtGuard` — "Vượt ngưỡng nợ / nợ quá hạn" (FR-SYS-032, ba mức FR-SYS-062).

Bản cài `PostingGuard` thứ hai, cùng khuôn `cash_book.guards.CashBalanceGuard`.
Đặt ở `receivables` vì luật thuộc về **chủ sổ phụ**: nó soi mọi chứng từ làm
tăng công nợ của một đối tác — hóa đơn mua (7B), hóa đơn bán (7C), cả bút toán
GLE tự định khoản vào 131/331 — chứ không riêng phân hệ nào, và `purchase`/
`sales` không cần biết guard này tồn tại (luật C3).

Hai phép kiểm, cả hai chỉ chạy khi chứng từ **tăng** nợ của đối tác (một phiếu
trả tiền không bao giờ bị chặn vì đối tác đang nợ nhiều):

* **Ngưỡng nợ** — `partners.credit_limit` (VND, `NULL` = không đặt) so với
  tổng còn nợ (sổ phụ + công nợ đầu kỳ) cộng phần chứng từ sắp ghi.
* **Nợ quá hạn** — đối tác đang có khoản còn nợ đã quá hạn tính đến ngày hạch
  toán của chứng từ mới. Hạn của một khoản là `due_date` ghi trên khoản; khoản
  không ghi hạn thì lấy ngày chứng từ + `due_days` của điều khoản thanh toán
  gắn với đối tác, và không có điều khoản thì khoản ấy không có hạn (không
  bao giờ quá hạn) — khoản CÓ hạn tường minh vẫn được soi dù đối tác không
  gắn điều khoản nào.

"Tăng nợ" đọc từ chính các dòng sắp ghi: dòng trên TK có `detail_tracking`
`customer`/`vendor` và mang chiều đối tác; phải thu tăng theo Nợ, phải trả tăng
theo Có. Chỉ sổ tài chính — sổ phụ công nợ hiện chỉ có dòng sổ ấy.

**Số còn nợ đọc qua `partner_open_debt(...)` — hàm `SECURITY DEFINER` của
migration 0026, chạy NGOÀI RLS.** Nó gộp cả công nợ đầu kỳ còn treo của niên độ
phủ ngày hạch toán: nợ mang sang từ hệ thống cũ là nợ thật của đối tác.

Hệ quả **cố ý** (quyết định user 2026-09-03): thông điệp "nợ quá hạn" nêu số
chứng từ của khoản sớm nhất, và khoản ấy có thể thuộc chi nhánh khác — thứ RLS
vốn giấu hẳn. Người bị chặn cần một chỗ bấu để xử lý, còn số chứng từ là dữ
kiện yếu; giữ nguyên thay vì che. `ar_ap_ledger` khoanh theo chi nhánh, còn
`credit_limit` là ngưỡng của đối tác trước toàn công ty: đọc dưới RLS của người
ghi sổ thì đối tác nợ 900 ở A + 900 ở B lọt qua ngưỡng 1000 ở cả hai chi nhánh.
Cùng cách vá với guard khớp sao kê (0024) — xem docstring migration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import DetailTracking
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.config.catalog import (
    PARTNER_DEBT_WARNING_KEY,
    WARNING_LEVEL_BLOCK,
    WARNING_LEVEL_NONE,
)
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingViolation
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.posting.contracts import GuardFinding, Voucher
from ket.posting.engine.prepared import PreparedLine

CREDIT_LIMIT_EXCEEDED_CODE = "receivables.credit_limit_exceeded"
OVERDUE_DEBT_CODE = "receivables.overdue_debt"

_FINANCIAL_LEDGER = 0
"""Hàm `partner_open_debt` cũng chỉ lấy sổ này; giữ hằng để docstring và test
nói cùng một số."""
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class _OpenDebt:
    document_no: str
    document_date: date
    due_date: date | None
    remaining: Decimal


def _open_debts(
    session: Session, *, partner_kind: int, partner_id: int, as_of: date
) -> tuple[_OpenDebt, ...]:
    """Khoản còn nợ sổ tài chính của đối tác trên MỌI chi nhánh (ngoài RLS),
    kể cả công nợ đầu kỳ của niên độ phủ `as_of`."""
    rows = session.execute(
        # Chuỗi HẰNG, không f-string — cùng lý do với `bank.reconciliation`.
        text(
            "SELECT document_no, document_date, due_date, remaining"
            " FROM partner_open_debt(:partner_kind, :partner_id, :as_of)"
        ),
        {"partner_kind": partner_kind, "partner_id": partner_id, "as_of": as_of},
    ).all()
    return tuple(
        _OpenDebt(
            document_no=document_no,
            document_date=document_date,
            due_date=due_date,
            remaining=Decimal(remaining),
        )
        for document_no, document_date, due_date, remaining in rows
    )


class PartnerDebtGuard:
    """Soi ngưỡng nợ và nợ quá hạn của đối tác — cắm vào `PostingService`."""

    def check(
        self,
        session: Session,
        *,
        voucher: Voucher,
        lines: Sequence[PreparedLine],
    ) -> Sequence[GuardFinding]:
        level = value_of(session, key=PARTNER_DEBT_WARNING_KEY, user_id=voucher.created_by)
        if level == WARNING_LEVEL_NONE:
            return ()

        increases = self._debt_increases(session, lines)
        if not increases:
            return ()

        findings: list[GuardFinding] = []
        blocking = level == WARNING_LEVEL_BLOCK
        for (partner_kind, partner_id), delta in sorted(increases.items()):
            partner = session.get(Partner, partner_id)
            if partner is None:
                # Đối tác không tra được thì không có ngưỡng lẫn điều khoản để
                # soi; chính lượt ghi sổ sẽ hỏng ở khóa ngoại của dòng sổ phụ,
                # không phải việc guard này báo.
                continue
            debts = _open_debts(
                session,
                partner_kind=partner_kind,
                partner_id=partner_id,
                as_of=voucher.posting_date,
            )
            findings.extend(
                self._check_limit(
                    partner=partner,
                    partner_kind=partner_kind,
                    debts=debts,
                    delta=delta,
                    blocking=blocking,
                )
            )
            findings.extend(
                self._check_overdue(
                    session,
                    partner=partner,
                    partner_kind=partner_kind,
                    debts=debts,
                    as_of=voucher.posting_date,
                    blocking=blocking,
                )
            )
        return findings

    def _debt_increases(
        self, session: Session, lines: Sequence[PreparedLine]
    ) -> dict[tuple[int, int], Decimal]:
        """(loại đối tác, đối tác) → phần nợ (VND) chứng từ sắp cộng thêm, chỉ
        giữ những cặp có phần cộng dương."""
        accounts = accounts_by_id(session, [line.source.account_id for line in lines])
        deltas: dict[tuple[int, int], Decimal] = {}
        for line in lines:
            if line.ledger != _FINANCIAL_LEDGER:
                continue
            dimensions = line.source.dimensions
            if dimensions.partner_id is None or dimensions.partner_kind is None:
                continue
            account = accounts.get(line.source.account_id)
            tracking = set(account.detail_tracking or ()) if account is not None else set()
            if (
                dimensions.partner_kind is PartnerKind.CUSTOMER
                and DetailTracking.CUSTOMER in tracking
            ):
                delta = line.debit - line.credit
            elif (
                dimensions.partner_kind is PartnerKind.VENDOR and DetailTracking.VENDOR in tracking
            ):
                delta = line.credit - line.debit
            else:
                continue
            key = (dimensions.partner_kind.value, dimensions.partner_id)
            deltas[key] = deltas.get(key, _ZERO) + delta
        return {key: delta for key, delta in deltas.items() if delta > _ZERO}

    def _check_limit(
        self,
        *,
        partner: Partner,
        partner_kind: int,
        debts: Sequence[_OpenDebt],
        delta: Decimal,
        blocking: bool,
    ) -> list[GuardFinding]:
        if partner.credit_limit is None:
            return []
        projected = sum((debt.remaining for debt in debts), _ZERO) + delta
        if projected <= partner.credit_limit:
            return []
        return [
            GuardFinding(
                violation=PostingViolation(
                    CREDIT_LIMIT_EXCEEDED_CODE,
                    f"Ghi sổ xong thì {partner.code} nợ {projected:,.0f}, vượt ngưỡng "
                    f"{partner.credit_limit:,.0f} (FR-SYS-032)",
                    partner_id=partner.id,
                    partner_code=partner.code,
                    partner_kind=partner_kind,
                    projected=str(projected),
                    credit_limit=str(partner.credit_limit),
                ),
                blocking=blocking,
            )
        ]

    def _check_overdue(
        self,
        session: Session,
        *,
        partner: Partner,
        partner_kind: int,
        debts: Sequence[_OpenDebt],
        as_of: date,
        blocking: bool,
    ) -> list[GuardFinding]:
        term = (
            session.get(PaymentTerm, partner.payment_term_id)
            if partner.payment_term_id is not None
            else None
        )
        due_days = term.due_days if term is not None else None
        overdue = [
            (debt.document_no, due, debt.remaining)
            for debt in debts
            if (due := _due_of(debt, due_days)) is not None and due < as_of
        ]
        if not overdue:
            return []
        oldest_no, oldest_due, _ = min(overdue, key=lambda row: (row[1], row[0]))
        total = sum((remaining for _, _, remaining in overdue), _ZERO)
        term_note = f" (số ngày được nợ {due_days})" if due_days is not None else ""
        return [
            GuardFinding(
                violation=PostingViolation(
                    OVERDUE_DEBT_CODE,
                    f"{partner.code} đang có {len(overdue)} khoản nợ quá hạn, tổng "
                    f"{total:,.0f} — sớm nhất {oldest_no} hạn {oldest_due:%d/%m/%Y}{term_note}",
                    partner_id=partner.id,
                    partner_code=partner.code,
                    partner_kind=partner_kind,
                    overdue_count=len(overdue),
                    overdue_total=str(total),
                    oldest_document_no=oldest_no,
                    oldest_due_date=oldest_due.isoformat(),
                    due_days=due_days,
                ),
                blocking=blocking,
            )
        ]


def _due_of(debt: _OpenDebt, due_days: int | None) -> date | None:
    if debt.due_date is not None:
        return debt.due_date
    if due_days is None:
        return None
    return debt.document_date + timedelta(days=due_days)
