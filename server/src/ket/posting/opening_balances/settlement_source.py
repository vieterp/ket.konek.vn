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

_DETAIL_KIND_BY_PARTNER: dict[PartnerKind, int] = {
    partner: kind for kind, partner in _DETAIL_KIND_TO_PARTNER.items()
}
"""Nhóm số dư mà một loại đối tác treo công nợ. Nhân viên cố ý vắng mặt — xem
docstring đầu tệp."""


def _to_open_invoice(invoice: OpeningBalanceInvoice, parent: OpeningBalance) -> OpenInvoice:
    partner_kind = (
        PartnerKind(parent.partner_kind)
        if parent.partner_kind is not None
        else _DETAIL_KIND_TO_PARTNER[parent.detail_kind]
    )
    if parent.partner_id is None:  # pragma: no cover - 4C bắt buộc đối tác cho nhóm 2/3
        raise RuntimeError(f"Dòng số dư nhóm {parent.detail_kind} thiếu đối tác: {parent.id}")
    return OpenInvoice(
        # Loại đích mang CHIỀU, và chiều của khoản ứng trước ngược với chiều nợ
        # của dòng cha (lát 7C-5): phép kiểm chiều ở `posting.settlements` đọc
        # loại đích chứ không đọc bảng nguồn, nên dùng chung một giá trị cho cả
        # hai chiều là cho phiếu thu tất toán đúng khoản khách vừa ứng trước.
        target_kind=(
            SettlementTargetKind.OPENING_ADVANCE
            if invoice.is_advance
            else SettlementTargetKind.OPENING_BALANCE
        ),
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
        receivable_view: bool,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        """Chứng từ đầu kỳ còn treo của MỘT chiều, cho MỘT đối tác.

        Nhóm số dư suy từ loại đối tác (khách hàng treo ở nhóm 2, nhà cung cấp
        ở nhóm 3), còn **chiều** thì suy từ cột `is_advance`: chiều nợ tự nhiên
        của đối tác nằm ở dòng con thường, chiều ngược nằm ở dòng ứng trước
        (lát 7C-5). Trước lát ấy view khóa cứng `detail_kind` và dựa vào việc
        hỏi nhầm chiều trả danh sách rỗng — cách ấy không phân biệt được hai
        chiều CÙNG sống trên một dòng cha lưỡng tính (BR-OPB-03).

        Nhân viên không có mặt trong bảng nhóm nên mọi câu hỏi về họ ra rỗng,
        đúng phạm vi v1 nêu ở đầu tệp.
        """
        detail_kind = _DETAIL_KIND_BY_PARTNER.get(partner_kind)
        if detail_kind is None:
            return ()
        year = fiscal_year_covering(session, as_of)
        if year is None:
            return ()
        natural_is_receivable = partner_kind is PartnerKind.CUSTOMER
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
                OpeningBalanceInvoice.is_advance.is_(natural_is_receivable != receivable_view),
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
    """`ReceivableProvider` khóa cứng CHIỀU THU — bản đăng ký cho registry
    chiều thu (sửa H-1 review 6B).

    Chiều chứ không nhóm số dư từ lát 7C-5: một khoản ta đã trả trước cho nhà
    cung cấp treo ở nhóm 3 nhưng là thứ **họ nợ ta**, nên nó đứng cùng hàng với
    hóa đơn bán trên màn thu tiền."""

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
            receivable_view=True,
            partner_kind=partner_kind,
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )


class _PayableView:
    """`PayableProvider` khóa cứng CHIỀU TRẢ — đối xứng với trên: hóa đơn còn
    nợ nhà cung cấp, cộng khoản khách hàng đã ứng trước từ đầu kỳ."""

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
            receivable_view=False,
            partner_kind=partner_kind,
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )


SOURCE = OpeningBalanceSettlementSource()

PROVIDERS.register_receivable(_ReceivableView(SOURCE))
PROVIDERS.register_payable(_PayableView(SOURCE))
PROVIDERS.register_settlement_source(SettlementTargetKind.OPENING_BALANCE, SOURCE)
PROVIDERS.register_settlement_source(SettlementTargetKind.OPENING_ADVANCE, SOURCE)
