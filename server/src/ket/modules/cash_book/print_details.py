"""Phần riêng của phiếu thu/chi trên bản in — mẫu 01-TT/02-TT (TT99 Phụ lục I).

Cắm vào `PostingDocumentType.print_details` (lát 6E-2): tầng api dựng phần
CHUNG của mọi bản in (số, ngày, dòng định khoản, đơn vị), module điền phần chỉ
nó biết — "Họ và tên người nộp tiền", "Lý do nộp", "Kèm theo … chứng từ gốc".

Đối chiếu biểu mẫu: 01-TT ở PDF Công báo trang 55 (đánh số 77), 02-TT trang 56
(78) — bản quét, không có lớp chữ, đọc bằng mắt như 6E-1 làm với Phụ lục III.
Hai sai khác CÓ CHỦ ĐÍCH so với tờ giấy, ghi ở đây để người sau khỏi tưởng là
sót: (1) bỏ dòng "Quyển số" — phần mềm cấp số theo dãy cấu hình được
(FR-QUY-008), không quản lý quyển giấy, in một ô luôn rỗng là mời người dùng
điền tay vào chỗ hệ thống không đọc lại được; (2) tờ giấy chỉ có MỘT cặp
Nợ/Có, phần mềm cho nhiều dòng (FR-QUY-005) nên khối góc phải liệt kê các tài
khoản đã dùng và bảng chi tiết bên dưới mới là chỗ đọc từng dòng.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.printing.context import (
    DocumentPrintDetails,
    PrintField,
    PrintTable,
    PrintTableColumn,
)
from ket.kernel.config.printing.voucher_fields import (
    MoneyLine,
    currency_unit,
    debit_credit_fields,
    foreign_currency_notes,
    money_side_amounts,
)
from ket.kernel.contracts import PartnerKind
from ket.kernel.formatting import format_date, format_money
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.money_words import amount_in_words
from ket.modules.cash_book.models import (
    CashCountSheet,
    CashCountSheetLine,
    CashVoucher,
    CashVoucherKind,
    CashVoucherLine,
)
from ket.posting.contracts import Voucher

_RECEIPT_LABELS = ("Họ và tên người nộp tiền", "Lý do nộp")
_PAYMENT_LABELS = ("Họ và tên người nhận tiền", "Lý do chi")


def build_print_details(session: Session, voucher_id: UUID, user_id: int) -> DocumentPrintDetails:
    """Trường riêng của một phiếu thu/chi cho mẫu in."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(CashVoucher, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - router đã tra chứng từ
        raise ValueError(f"Phiếu thu/chi {voucher_id} không còn tồn tại")
    lines = list(
        session.execute(
            select(CashVoucherLine)
            .where(CashVoucherLine.voucher_id == voucher_id)
            .order_by(CashVoucherLine.line_no)
        )
        .scalars()
        .all()
    )
    name_label, reason_label = (
        _RECEIPT_LABELS if body.kind == CashVoucherKind.RECEIPT else _PAYMENT_LABELS
    )
    counterpart = _counterpart(session, body)
    money_lines = tuple(
        MoneyLine(line.debit_account_id, line.credit_account_id, line.amount_fc) for line in lines
    )
    # Số tiền của tờ phiếu là số THẬT vào/ra két, không phải tổng mọi dòng
    # (review H-1): FR-QUY-007 cho chiết khấu thanh toán `Nợ 635/Có 131` nằm
    # chung phiếu thu, và dòng đó không phải tiền người nộp đưa.
    cash_amounts = money_side_amounts(money_lines, account_ids={body.cash_account_id})
    total_fc = abs(sum(cash_amounts, Decimal(0)))

    fields = [
        PrintField(name_label, body.payer_receiver_name or counterpart.name),
        PrintField("Địa chỉ", counterpart.address),
        PrintField(reason_label, voucher.description or ""),
    ]
    if body.attachment_count:
        fields.append(PrintField("Kèm theo", f"{body.attachment_count} chứng từ gốc"))
    return DocumentPrintDetails(
        header_fields=debit_credit_fields(session, money_lines),
        fields=tuple(fields),
        amount=f"{format_money(total_fc, blank_zero=False)} {voucher.currency_code}",
        amount_in_words=amount_in_words(total_fc, unit=currency_unit(voucher.currency_code)),
        notes=foreign_currency_notes(
            session,
            currency_code=voucher.currency_code,
            period_id=voucher.period_id,
            exchange_rate=voucher.exchange_rate,
            amounts_fc=cash_amounts,
            user_id=user_id,
        ),
    )


def build_count_sheet_print_details(session: Session, sheet_id: UUID) -> DocumentPrintDetails:
    """Biên bản kiểm kê quỹ (FR-QUY-030) theo mẫu 08a-TT — trả nợ lát 6B.

    Đi cùng đường in với chứng từ dù **không phải chứng từ**: không có dòng nào
    trong `vouchers`, nên toàn bộ nội dung nằm ở một `PrintTable` thay vì ở
    `lines` định khoản.

    **Chiều của dòng III là bẫy có thật, không phải chi tiết văn bản.** Mẫu
    08a-TT (PDF Công báo trang 62/đánh số 84) định nghĩa `III = I − II`, tức
    **số sổ trừ số thực tế** — dương nghĩa là THIẾU. Còn
    `CashCountSheetService.create_adjustment` tính `counted_total −
    book_balance` — dương nghĩa là THỪA, vì nó cần dấu theo chiều bút toán.
    Hai chiều ngược nhau cùng đúng trong ngữ cảnh của mình; in nhầm chiều thì
    biên bản báo thừa đúng lúc két thiếu. Test ghim cả hai chiều.
    """
    sheet = session.get(CashCountSheet, sheet_id)
    if sheet is None:  # pragma: no cover - router đã tra biên bản
        raise ValueError(f"Biên bản kiểm kê {sheet_id} không còn tồn tại")
    lines = list(
        session.execute(
            select(CashCountSheetLine)
            .where(CashCountSheetLine.sheet_id == sheet_id)
            .order_by(CashCountSheetLine.line_no)
        )
        .scalars()
        .all()
    )
    account = session.get(ChartOfAccount, sheet.cash_account_id)
    form_difference = sheet.book_balance - sheet.counted_total

    rows: list[tuple[str, str, str, str]] = [
        ("I", "Số dư theo sổ quỹ", "x", format_money(sheet.book_balance, blank_zero=False)),
        ("II", "Số kiểm kê thực tế", "x", format_money(sheet.counted_total, blank_zero=False)),
    ]
    for index, line in enumerate(lines, start=1):
        rows.append(
            (
                str(index),
                f"{'Trong đó: ' if index == 1 else ''}- Loại "
                f"{format_money(line.denomination, blank_zero=False)}",
                str(line.quantity),
                format_money(line.denomination * line.quantity, blank_zero=False),
            )
        )
    rows.append(
        ("III", "Chênh lệch (III = I - II)", "x", format_money(form_difference, blank_zero=False))
    )

    fields = [
        PrintField(
            "Tài khoản quỹ",
            f"{account.code} - {account.name}" if account is not None else "",
        ),
        PrintField("Thời điểm kiểm kê", format_date(sheet.count_date)),
    ]
    notes = []
    if form_difference > 0:
        notes.append(PrintField("Lý do thiếu", ""))
    elif form_difference < 0:
        notes.append(PrintField("Lý do thừa", ""))
    notes.append(PrintField("Kết luận sau khi kiểm kê quỹ", sheet.note or ""))
    return DocumentPrintDetails(
        fields=tuple(fields),
        tables=(
            PrintTable(
                columns=(
                    PrintTableColumn("STT", align="center", width="8%"),
                    PrintTableColumn("Diễn giải"),
                    PrintTableColumn("Số lượng (tờ)", align="money", width="18%"),
                    PrintTableColumn("Số tiền", align="money", width="22%"),
                ),
                rows=tuple(rows),
            ),
        ),
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class _Counterpart:
    """Tên + địa chỉ người nộp/nhận tiền, đã rơi về chuỗi rỗng khi không có."""

    name: str
    address: str


_NOBODY = _Counterpart("", "")


def _counterpart(session: Session, body: CashVoucher) -> _Counterpart:
    """Đối tác của phiếu → tên + địa chỉ in trên phiếu.

    Nhân viên nằm ở danh mục khác và **không có cột địa chỉ** (danh mục nhân
    sự v1 chỉ giữ bộ phận/chức vụ) — in bộ phận vào ô "Địa chỉ" đúng thói quen
    chứng từ tạm ứng nội bộ, và đó là dữ liệu thật chứ không phải ô bịa.
    """
    if body.partner_id is None or body.partner_kind is None:
        return _NOBODY
    if body.partner_kind == PartnerKind.EMPLOYEE:
        employee = session.get(Employee, body.partner_id)
        return (
            _NOBODY if employee is None else _Counterpart(employee.name, employee.department or "")
        )
    partner = session.get(Partner, body.partner_id)
    return _NOBODY if partner is None else _Counterpart(partner.name, partner.address or "")
