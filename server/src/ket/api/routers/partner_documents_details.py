"""Phần riêng của ba văn bản gửi đối tác trên bản in (lát 7G-5).

Biên bản đối chiếu (hai chiều) và thông báo công nợ đều dựng từ cùng một
nguồn số — `api/open_items.py`, tức dataset của báo cáo tuổi nợ — và đổ vào
`DocumentPrintDetails` như mọi bản in khác: `fields` là khối đối tác, `tables`
là bảng số, `amount`/`amount_in_words` là con số người ký phải đọc được bằng
chữ, `notes` là phần chân (tài khoản nhận tiền của đơn vị, với thông báo).

Ở tầng api chứ không ở một module: văn bản đọc danh mục đối tác + dataset công
nợ + TK ngân hàng doanh nghiệp — ba chủ khác nhau, và C3 cấm `sales` hỏi hai
chủ kia. Cùng chỗ đứng với `partner_overview.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.api.open_items import OpenItem, PeriodSummary
from ket.kernel.config.printing.context import (
    DocumentPrintDetails,
    PrintField,
    PrintTable,
    PrintTableColumn,
)
from ket.kernel.formatting import format_date, format_money
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.money_words import amount_in_words


def partner_fields(partner: Partner) -> tuple[PrintField, ...]:
    """Khối "bên B" / "kính gửi" — bỏ dòng trống thay vì in nhãn không giá trị."""
    candidates = (
        ("Đơn vị", partner.name),
        ("Địa chỉ", partner.full_address),
        ("Mã số thuế", partner.tax_code),
        ("Người liên hệ", partner.contact_name),
        ("Điện thoại", partner.phone),
    )
    return tuple(PrintField(label, value) for label, value in candidates if value)


def statement_details(
    partner: Partner,
    *,
    summary: PeriodSummary,
    open_items_at_close: Sequence[OpenItem],
    from_date: date,
    to_date: date,
) -> DocumentPrintDetails:
    """Biên bản đối chiếu theo kỳ: bốn số + bảng khoản còn treo cuối kỳ.

    Số dư cuối kỳ đứng ở `amount` (và bằng chữ) vì đó là con số hai bên ký
    xác nhận; bốn số của kỳ đi vào một bảng riêng để đối tác dò được "lệch ở
    đâu": số đầu kỳ hay phát sinh.
    """
    period = PrintTable(
        caption=f"I. Tổng hợp công nợ từ ngày {format_date(from_date)} đến ngày {format_date(to_date)}",
        columns=(
            PrintTableColumn("Chỉ tiêu"),
            PrintTableColumn("Số tiền (VND)", align="money", width="32%"),
        ),
        rows=(
            ("Số dư đầu kỳ", _money(summary.opening)),
            ("Phát sinh tăng trong kỳ", _money(summary.increase)),
            ("Phát sinh giảm trong kỳ (đã thanh toán)", _money(summary.decrease)),
            ("Số dư cuối kỳ", _money(summary.closing)),
        ),
    )
    return DocumentPrintDetails(
        fields=partner_fields(partner),
        amount=_money(summary.closing),
        amount_in_words=amount_in_words(summary.closing),
        tables=(
            period,
            open_items_table(
                open_items_at_close, caption="II. Chi tiết khoản còn nợ tại ngày cuối kỳ"
            ),
        ),
    )


def notice_details(
    session: Session,
    partner: Partner,
    *,
    open_items_now: Sequence[OpenItem],
    total_open: Decimal,
    total_overdue: Decimal,
) -> DocumentPrintDetails:
    """Thông báo công nợ: tổng còn nợ, phần quá hạn, bảng khoản kèm hạn, TK nhận tiền."""
    summary = PrintTable(
        columns=(
            PrintTableColumn("Chỉ tiêu"),
            PrintTableColumn("Số tiền (VND)", align="money", width="32%"),
        ),
        rows=(
            ("Tổng còn phải thanh toán", _money(total_open)),
            ("Trong đó: đã quá hạn", _money(total_overdue)),
        ),
    )
    return DocumentPrintDetails(
        fields=partner_fields(partner),
        amount=_money(total_open),
        amount_in_words=amount_in_words(total_open),
        tables=(
            summary,
            open_items_table(
                open_items_now, caption="Chi tiết các khoản còn nợ", with_overdue=True
            ),
        ),
        notes=company_bank_account_notes(session),
    )


def open_items_table(
    items: Sequence[OpenItem], *, caption: str, with_overdue: bool = False
) -> PrintTable:
    """Bảng khoản còn treo — nguyên tệ khi khác VND, VND ở cột cộng chung.

    Khoản không có số chứng từ (ứng trước đầu kỳ) in nhãn nguồn ở cột chứng từ
    và để trống cột nội dung — một ô trống ở cột số trên tờ giấy gửi đối tác
    đọc như một dòng lỗi, còn in nhãn hai lần thì đọc như lỗi khác.
    """
    columns = [
        PrintTableColumn("STT", align="center", width="5%"),
        PrintTableColumn("Chứng từ", width="19%"),
        PrintTableColumn("Ngày", align="center", width="11%"),
        PrintTableColumn("Hạn TT", align="center", width="11%"),
        PrintTableColumn("Nội dung"),
        PrintTableColumn("Giá trị (VND)", align="money", width="12%"),
        PrintTableColumn("Đã TT (VND)", align="money", width="12%"),
        PrintTableColumn("Còn lại (VND)", align="money", width="12%"),
    ]
    if with_overdue:
        columns.append(PrintTableColumn("Quá hạn", align="center", width="8%"))
    rows: list[tuple[str, ...]] = []
    for index, item in enumerate(items, start=1):
        content = item.source_label if item.document_no else ""
        if item.currency_code != "VND":
            content = f"{content} — {format_money(item.remaining_fc, blank_zero=False)} {item.currency_code}"
        row = [
            str(index),
            item.document_no or item.source_label,
            format_date(item.document_date) if item.document_date is not None else "",
            format_date(item.due_date) if item.due_date is not None else "",
            content,
            _money(item.amount),
            _money(item.settled),
            _money(item.remaining),
        ]
        if with_overdue:
            row.append(str(item.days_overdue) if item.days_overdue else "")
        rows.append(tuple(row))
    total = [
        "",
        "Cộng",
        "",
        "",
        "",
        _money(sum((item.amount for item in items), Decimal(0))),
        _money(sum((item.settled for item in items), Decimal(0))),
        _money(sum((item.remaining for item in items), Decimal(0))),
    ]
    if with_overdue:
        total.append("")
    return PrintTable(
        caption=caption, columns=tuple(columns), rows=tuple(rows), total_row=tuple(total)
    )


def company_bank_account_notes(session: Session) -> tuple[PrintField, ...]:
    """Mọi TK ngân hàng doanh nghiệp đang hoạt động — không có khái niệm "TK mặc
    định" trong danh mục (7A), nên in đủ và để người nhận chọn."""
    rows = session.execute(
        select(CompanyBankAccount, Bank.name)
        .outerjoin(Bank, Bank.id == CompanyBankAccount.bank_id)
        .where(CompanyBankAccount.is_active.is_(True), CompanyBankAccount.is_group.is_(False))
        .order_by(CompanyBankAccount.id)
    ).all()
    notes: list[PrintField] = []
    for account, bank_name in rows:
        parts = [f"Số TK {account.code}"]
        if account.account_holder:
            parts.append(f"chủ TK {account.account_holder}")
        if bank_name:
            parts.append(f"tại {bank_name}")
        if account.bank_branch:
            parts.append(f"chi nhánh {account.bank_branch}")
        notes.append(PrintField(account.name, ", ".join(parts)))
    return tuple(notes)


def _money(value: Decimal) -> str:
    return format_money(value, blank_zero=False)
