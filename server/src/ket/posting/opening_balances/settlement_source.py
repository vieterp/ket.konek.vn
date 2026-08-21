"""Nguồn công nợ mở + đích đối trừ từ SỐ DƯ BAN ĐẦU (phase-06, RT-18).

Bản cài đầu tiên của ba Protocol công nợ trong `kernel.protocols`: trước phase
7, "hóa đơn còn nợ" duy nhất của hệ là chi tiết chứng từ trong
`opening_balance_invoices` (nhóm phải thu / phải trả, slice 4C). Phiếu thu/chi
tiền (6B) liệt kê chúng qua `ReceivableProvider`/`PayableProvider` và ghi số đã
trả qua `SettlementTargetSource` — module `cash_book` không đọc bảng này trực
tiếp (luật phụ thuộc #2).

Số đã trả theo dõi ở **hai cột** vì hai đơn vị tiền khác nhau, không phải hai
cách nói một điều: `paid_amount_fc` (nguyên tệ) là trục đối trừ — người dùng
nhập "Số thu" theo nguyên tệ và `remaining_fc = amount_fc - paid_amount_fc`;
`paid_amount` (VND theo tỷ giá **ghi nhận nợ**) là phần giá trị sổ đã được giải
phóng — thứ mà kiểm "chi tiết khớp dòng cha" (BR-OPB-02) và phase 7 đọc. Suy
cột này từ cột kia phải qua một phép nhân + làm tròn, và làm tròn ở hai nơi là
hai con số.

Nhóm tạm ứng nhân viên (kind 4) cố ý đứng ngoài v1: SRS 03 §4 chỉ đặc tả đối
trừ công nợ khách hàng/NCC; "thu hoàn ứng" đi đường nghiệp vụ `thu-hoan-ung`
không đối trừ từng lần ứng.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.protocols import PROVIDERS, OpenInvoice, SettlementTargetKind
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)

SETTLEMENT_TARGET_MISSING_CODE = "settlement.target_missing"
SETTLEMENT_OVERPAID_CODE = "settlement.exceeds_remaining"

_DETAIL_KIND_TO_PARTNER: dict[int, PartnerKind] = {
    OpeningDetailKind.RECEIVABLE: PartnerKind.CUSTOMER,
    OpeningDetailKind.PAYABLE: PartnerKind.VENDOR,
}


def _to_open_invoice(invoice: OpeningBalanceInvoice, parent: OpeningBalance) -> OpenInvoice:
    partner_kind = (
        PartnerKind(parent.partner_kind)
        if parent.partner_kind is not None
        else _DETAIL_KIND_TO_PARTNER[parent.detail_kind]
    )
    if parent.partner_id is None:  # pragma: no cover - 4C bắt buộc đối tác cho nhóm 2/3
        raise RuntimeError(f"Dòng số dư nhóm {parent.detail_kind} thiếu đối tác: {parent.id}")
    return OpenInvoice(
        target_kind=SettlementTargetKind.OPENING_BALANCE,
        target_id=invoice.id,
        partner_kind=partner_kind,
        partner_id=parent.partner_id,
        branch_id=parent.branch_id,
        account_id=parent.account_id,
        # Chi tiết 4C cho phép bỏ trống số/ngày hóa đơn; hình dạng chung thì
        # không — chuỗi rỗng và ngày đầu năm là "không rõ", đủ cho màn chọn.
        invoice_no=invoice.invoice_no or "",
        invoice_date=invoice.invoice_date or date(1970, 1, 1),
        due_date=invoice.due_date,
        currency_code=parent.currency_code,
        exchange_rate=parent.exchange_rate,
        amount_fc=invoice.amount_fc,
        remaining_fc=invoice.amount_fc - invoice.paid_amount_fc,
        remaining=invoice.amount - invoice.paid_amount,
    )


class OpeningBalanceSettlementSource:
    """Cài `SettlementTargetSource` + phần đọc chung cho hai provider công nợ.

    `open_invoices` KHÔNG nằm trên lớp này mà trên hai view khóa-chiều bên
    dưới (`_ReceivableView`/`_PayableView`) — sửa H-1 review 6B: một đối tượng
    đăng ký làm CẢ hai provider sẽ trả lời theo `partner_kind` bất kể nó được
    hỏi qua registry chiều nào, và cổng quyền phía API (kiểm theo chiều) bị đi
    vòng — người chỉ có quyền xem phiếu thu đọc được công nợ phải trả.
    """

    def _open_invoices(
        self,
        session: Session,
        *,
        detail_kind: int,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        """Hóa đơn còn nợ của MỘT nhóm số dư — `detail_kind` do view khóa, nên
        hỏi nhầm chiều (partner_kind không khớp nhóm) chỉ ra danh sách rỗng."""
        year = fiscal_year_covering(session, as_of)
        if year is None:
            return ()
        rows = session.execute(
            select(OpeningBalanceInvoice, OpeningBalance)
            .join(OpeningBalance, OpeningBalance.id == OpeningBalanceInvoice.opening_balance_id)
            .where(
                OpeningBalance.fiscal_year_id == year.id,
                OpeningBalance.ledger == 0,
                OpeningBalance.branch_id == branch_id,
                OpeningBalance.detail_kind == detail_kind,
                OpeningBalance.partner_kind == partner_kind.value,
                OpeningBalance.partner_id == partner_id,
                OpeningBalanceInvoice.amount_fc > OpeningBalanceInvoice.paid_amount_fc,
            )
            .order_by(OpeningBalanceInvoice.invoice_date, OpeningBalanceInvoice.invoice_no)
        ).all()
        return tuple(_to_open_invoice(invoice, parent) for invoice, parent in rows)

    def find(self, session: Session, *, target_ids: Sequence[UUID]) -> Sequence[OpenInvoice]:
        if not target_ids:
            return ()
        rows = session.execute(
            select(OpeningBalanceInvoice, OpeningBalance)
            .join(OpeningBalance, OpeningBalance.id == OpeningBalanceInvoice.opening_balance_id)
            .where(
                OpeningBalanceInvoice.id.in_(set(target_ids)),
                OpeningBalance.detail_kind.in_(tuple(_DETAIL_KIND_TO_PARTNER)),
            )
        ).all()
        return tuple(_to_open_invoice(invoice, parent) for invoice, parent in rows)

    def apply(
        self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
    ) -> None:
        invoice = self._lock(session, target_id)
        remaining_fc = invoice.amount_fc - invoice.paid_amount_fc
        remaining = invoice.amount - invoice.paid_amount
        # Kiểm CẢ HAI phía (sửa H-2 review 6B): nguyên tệ là trục đối trừ
        # (BR-QUY-02), còn phía VND có thể vượt riêng vì phần lẻ làm tròn của
        # các lát trước — để lọt thì CHECK `paid_within_amount` của DB nổ thành
        # IntegrityError 500 thay vì một vi phạm nghiệp vụ 422.
        if amount_fc > remaining_fc or amount > remaining:
            raise PostingValidationError(
                "Số đối trừ vượt số còn nợ của chứng từ công nợ",
                violations=[
                    PostingViolation(
                        SETTLEMENT_OVERPAID_CODE,
                        "Chứng từ công nợ đã được đối trừ bởi phiếu khác — số còn nợ không đủ",
                        target_id=str(target_id),
                        remaining_fc=str(remaining_fc),
                        amount_fc=str(amount_fc),
                        remaining=str(remaining),
                        amount=str(amount),
                    )
                ],
            )
        invoice.paid_amount_fc += amount_fc
        invoice.paid_amount += amount
        session.flush()

    def revert(
        self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
    ) -> None:
        invoice = self._lock(session, target_id)
        if amount_fc > invoice.paid_amount_fc or amount > invoice.paid_amount:
            # Gỡ nhiều hơn đã ghi = đường ghi và đường gỡ đã lệch nhau — phải
            # nổ chứ không kẹp về 0, cùng triết lý nhánh trừ của `record_use`.
            raise RuntimeError(
                f"Gỡ đối trừ {amount_fc} vượt số đã ghi {invoice.paid_amount_fc} "
                f"trên chứng từ {target_id}"
            )
        invoice.paid_amount_fc -= amount_fc
        invoice.paid_amount -= amount
        session.flush()

    def _lock(self, session: Session, target_id: UUID) -> OpeningBalanceInvoice:
        """Khóa dòng đích trước khi cộng/trừ — hai phiếu cùng đối trừ một hóa
        đơn phải nối tiếp nhau, nếu không cả hai cùng đọc một `paid` cũ và
        BR-QUY-02 chỉ còn đúng trên giấy."""
        invoice = session.execute(
            select(OpeningBalanceInvoice)
            .where(OpeningBalanceInvoice.id == target_id)
            .with_for_update()
        ).scalar_one_or_none()
        if invoice is None:
            raise PostingValidationError(
                "Chứng từ công nợ được đối trừ không còn tồn tại",
                violations=[
                    PostingViolation(
                        SETTLEMENT_TARGET_MISSING_CODE,
                        "Chứng từ công nợ đã bị xóa hoặc nhập lại — chọn lại "
                        "chứng từ đối trừ trên phiếu",
                        target_id=str(target_id),
                    )
                ],
            )
        return invoice


class _ReceivableView:
    """`ReceivableProvider` khóa cứng vào nhóm PHẢI THU — bản đăng ký cho
    registry chiều thu (sửa H-1 review 6B)."""

    def __init__(self, source: OpeningBalanceSettlementSource) -> None:
        self._source = source

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        return self._source._open_invoices(
            session,
            detail_kind=OpeningDetailKind.RECEIVABLE,
            partner_kind=partner_kind,
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )


class _PayableView:
    """`PayableProvider` khóa cứng vào nhóm PHẢI TRẢ — đối xứng với trên."""

    def __init__(self, source: OpeningBalanceSettlementSource) -> None:
        self._source = source

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        return self._source._open_invoices(
            session,
            detail_kind=OpeningDetailKind.PAYABLE,
            partner_kind=partner_kind,
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )


SOURCE = OpeningBalanceSettlementSource()

PROVIDERS.register_receivable(_ReceivableView(SOURCE))
PROVIDERS.register_payable(_PayableView(SOURCE))
PROVIDERS.register_settlement_source(SettlementTargetKind.OPENING_BALANCE, SOURCE)
