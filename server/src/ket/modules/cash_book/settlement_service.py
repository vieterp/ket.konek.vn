"""Đối trừ công nợ của phiếu thu/chi (`docs/srs/03` §4, FR-QUY-003, FR-SYS-066).

Ba việc, ba thời điểm:

* **Lúc cất** — `price_settlements`: tra đích qua `SettlementTargetSource`
  (kernel Protocol — module không biết bảng của nguồn), kiểm khớp đối tác/chi
  nhánh/tiền tệ/số còn nợ (BR-QUY-02) + tổng Số thu bằng tổng phiếu
  (BR-QUY-03), rồi **định giá**: `amount` = VND theo tỷ giá phiếu, `fx_diff` =
  chênh với VND theo tỷ giá ghi nhận nợ. Làm tròn xảy ra đúng một lần ở đây;
  mapper và applier chỉ dùng lại số đã chốt trên dòng.
* **Lúc ghi sổ** — `apply_settlements`: cộng số đã trả vào từng đích (source
  tự khóa `FOR UPDATE` + kiểm vượt lần cuối — hai phiếu đua nhau một hóa đơn
  giải ở đó, không ở đây).
* **Lúc bỏ ghi sổ** — `revert_settlements`: gỡ đúng số đã cộng.

VND giải phóng trên đích = `amount - fx_diff` (tức đúng `round(fc × tỷ giá ghi
nhận)`) — phần chênh không thuộc công nợ mà thuộc 515/635 (FR-SYS-066).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.money import convert_currency
from ket.kernel.protocols import (
    PROVIDERS,
    OpenInvoice,
    SettlementTargetKind,
    SettlementTargetSource,
)
from ket.modules.cash_book.models import CashSettlement
from ket.modules.cash_book.schemas import CashVoucherIn

SETTLEMENT_NO_SOURCE_CODE = "settlement.kind_unavailable"
SETTLEMENT_TARGET_MISSING_CODE = "settlement.target_missing"
SETTLEMENT_PARTNER_MISMATCH_CODE = "settlement.partner_mismatch"
SETTLEMENT_BRANCH_MISMATCH_CODE = "settlement.branch_mismatch"
SETTLEMENT_CURRENCY_MISMATCH_CODE = "settlement.currency_mismatch"
SETTLEMENT_OVER_REMAINING_CODE = "settlement.exceeds_remaining"
SETTLEMENT_PARTNER_REQUIRED_CODE = "settlement.partner_required"
SETTLEMENT_TOTAL_MISMATCH_CODE = "settlement.total_mismatch"


@dataclass(frozen=True)
class PricedSettlement:
    """Một dòng đối trừ đã định giá xong — thứ service ghi vào `cash_settlements`."""

    target_kind: SettlementTargetKind
    target_id: UUID
    amount_fc: Decimal
    amount: Decimal
    fx_diff: Decimal


def price_settlements(
    session: Session, payload: CashVoucherIn, *, scale: int
) -> list[PricedSettlement]:
    """Kiểm + định giá toàn bộ dòng đối trừ của một phiếu. Trả toàn bộ vi phạm
    một lượt (triết lý bộ kiểm phase-04), không nhỏ giọt."""
    if not payload.settlements:
        return []

    violations: list[PostingViolation] = []
    if payload.partner_id is None or payload.partner_kind is None:
        violations.append(
            PostingViolation(
                SETTLEMENT_PARTNER_REQUIRED_CODE,
                "Phiếu có đối trừ công nợ phải chọn đối tác",
            )
        )

    invoices = _find_targets(session, payload, violations)

    total_settled = Decimal(0)
    priced: list[PricedSettlement] = []
    for row in payload.settlements:
        invoice = invoices.get((row.target_kind, row.target_id))
        if invoice is None:
            continue
        _check_target(payload, row.amount_fc, invoice, violations)
        amount = convert_currency(row.amount_fc, payload.exchange_rate, scale)
        recognition = convert_currency(row.amount_fc, invoice.exchange_rate, scale)
        priced.append(
            PricedSettlement(
                target_kind=row.target_kind,
                target_id=row.target_id,
                amount_fc=row.amount_fc,
                amount=amount,
                fx_diff=amount - recognition,
            )
        )
        total_settled += row.amount_fc

    # BR-QUY-03: tổng cột "Số thu" phải bằng tổng tiền trên phiếu — chỉ so khi
    # mọi đích đều tra được, nếu không thông điệp "lệch tổng" chỉ là tiếng vọng
    # của lỗi thiếu đích ở trên.
    if not violations:
        total_lines = sum((line.amount_fc for line in payload.lines), Decimal(0))
        if total_settled != total_lines:
            violations.append(
                PostingViolation(
                    SETTLEMENT_TOTAL_MISMATCH_CODE,
                    "Tổng số đối trừ phải bằng tổng tiền trên phiếu",
                    settled_fc=str(total_settled),
                    voucher_fc=str(total_lines),
                )
            )

    if violations:
        raise PostingValidationError(
            "Dòng đối trừ công nợ trên phiếu chưa hợp lệ", violations=violations
        )
    return priced


def _find_targets(
    session: Session, payload: CashVoucherIn, violations: list[PostingViolation]
) -> dict[tuple[SettlementTargetKind, UUID], OpenInvoice]:
    """Tra mọi đích theo loại; loại chưa có chủ hoặc đích biến mất → vi phạm."""
    by_kind: dict[SettlementTargetKind, list[UUID]] = {}
    for row in payload.settlements:
        by_kind.setdefault(row.target_kind, []).append(row.target_id)

    found: dict[tuple[SettlementTargetKind, UUID], OpenInvoice] = {}
    for kind, target_ids in by_kind.items():
        source = PROVIDERS.settlement_source(kind)
        if source is None:
            violations.append(
                PostingViolation(
                    SETTLEMENT_NO_SOURCE_CODE,
                    "Loại chứng từ công nợ này chưa có phân hệ quản lý trong bản cài",
                    target_kind=kind.value,
                )
            )
            continue
        invoices = {
            invoice.target_id: invoice for invoice in source.find(session, target_ids=target_ids)
        }
        for target_id in target_ids:
            invoice = invoices.get(target_id)
            if invoice is None:
                violations.append(
                    PostingViolation(
                        SETTLEMENT_TARGET_MISSING_CODE,
                        "Chứng từ công nợ được đối trừ không còn tồn tại — chọn lại trên phiếu",
                        target_kind=kind.value,
                        target_id=str(target_id),
                    )
                )
            else:
                found[(kind, target_id)] = invoice
    return found


def _check_target(
    payload: CashVoucherIn,
    amount_fc: Decimal,
    invoice: OpenInvoice,
    violations: list[PostingViolation],
) -> None:
    if (
        payload.partner_kind is not None
        and payload.partner_id is not None
        and (invoice.partner_kind, invoice.partner_id) != (payload.partner_kind, payload.partner_id)
    ):
        violations.append(
            PostingViolation(
                SETTLEMENT_PARTNER_MISMATCH_CODE,
                "Chứng từ công nợ thuộc đối tác khác với đối tác trên phiếu",
                target_id=str(invoice.target_id),
            )
        )
    if invoice.branch_id != payload.branch_id:
        violations.append(
            PostingViolation(
                SETTLEMENT_BRANCH_MISMATCH_CODE,
                "Chứng từ công nợ thuộc chi nhánh khác với chi nhánh của phiếu",
                target_id=str(invoice.target_id),
            )
        )
    if invoice.currency_code != payload.currency_code:
        # Đối trừ chéo tiền tệ (thu VND cho nợ USD) cần tỷ giá thỏa thuận riêng
        # từng dòng — ngoài phạm vi v1, ghi ở báo cáo lát.
        violations.append(
            PostingViolation(
                SETTLEMENT_CURRENCY_MISMATCH_CODE,
                "Chứng từ công nợ ghi bằng loại tiền khác với phiếu",
                target_id=str(invoice.target_id),
                invoice_currency=invoice.currency_code,
                voucher_currency=payload.currency_code,
            )
        )
    if amount_fc > invoice.remaining_fc:
        violations.append(
            PostingViolation(
                SETTLEMENT_OVER_REMAINING_CODE,
                "Số đối trừ vượt số còn nợ của chứng từ công nợ (BR-QUY-02)",
                target_id=str(invoice.target_id),
                remaining_fc=str(invoice.remaining_fc),
                amount_fc=str(amount_fc),
            )
        )


def _settlements_of(session: Session, voucher_id: UUID) -> Sequence[CashSettlement]:
    return (
        session.execute(select(CashSettlement).where(CashSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )


def _require_source(kind: int) -> SettlementTargetSource:
    source = PROVIDERS.settlement_source(SettlementTargetKind(kind))
    if source is None:
        # Dòng đối trừ đã được cất với source còn sống; tới lúc ghi sổ mà source
        # biến mất là cấu hình tiến trình hỏng, không phải lỗi người dùng.
        raise RuntimeError(f"Không có SettlementTargetSource cho target_kind={kind}")
    return source


def apply_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Cộng số đã trả vào từng đích — chạy SAU `PostingService.post`, cùng transaction."""
    for row in _settlements_of(session, voucher_id):
        _require_source(row.target_kind).apply(
            session,
            target_id=row.target_id,
            amount_fc=row.amount_fc,
            amount=row.amount - row.fx_diff,
        )


def revert_settlements(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ số đã trả — chạy SAU `PostingService.unpost`, cùng transaction."""
    for row in _settlements_of(session, voucher_id):
        _require_source(row.target_kind).revert(
            session,
            target_id=row.target_id,
            amount_fc=row.amount_fc,
            amount=row.amount - row.fx_diff,
        )


def open_invoices(
    session: Session,
    *,
    side: str,
    partner_kind: PartnerKind,
    partner_id: int,
    branch_id: int,
    as_of: date,
) -> tuple[OpenInvoice, ...]:
    """Gộp mọi nguồn hóa đơn còn nợ của một phía (`receivable`/`payable`) —
    phase 6 chỉ có nguồn số dư ban đầu, phase 7 nối thêm mà không sửa đây."""
    providers = (
        PROVIDERS.receivable_providers() if side == "receivable" else PROVIDERS.payable_providers()
    )
    merged: list[OpenInvoice] = []
    for provider in providers:
        merged.extend(
            provider.open_invoices(
                session,
                partner_kind=partner_kind,
                partner_id=partner_id,
                branch_id=branch_id,
                as_of=as_of,
            )
        )
    return tuple(merged)
