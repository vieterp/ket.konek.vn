"""Phần riêng của chứng từ tiền gửi trên bản in — ủy nhiệm chi, giấy báo có.

Khác Quỹ một điểm gốc rễ: **TT99 Phụ lục I không có biểu mẫu cho ủy nhiệm chi
hay giấy báo có**. Ủy nhiệm chi là mẫu của TỪNG NGÂN HÀNG (FR-BNK-004), giấy
báo có do ngân hàng phát hành. Mẫu builtin ở đây vì thế là bản in nội bộ đủ
thông tin để đối chiếu và lưu hồ sơ, dựng theo các trường mà `docs/srs/04` §2
liệt kê; đơn vị cần đúng mẫu ngân hàng mình thì thêm một dòng `print_templates`
(mẫu là DỮ LIỆU — FR-RPT-008), không phải sửa mã nguồn.

Bộ ba `beneficiary_*` là "phía bên kia" của giao dịch, đã chụp thành chữ lúc
lập phiếu (xem `models.py`): với UNC/séc đó là người thụ hưởng, với giấy báo
có đó là người chuyển tiền đến. Nhãn đổi theo loại; dữ liệu là một.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.printing.context import DocumentPrintDetails, PrintField
from ket.kernel.config.printing.voucher_fields import (
    currency_unit,
    debit_credit_fields,
    foreign_currency_notes,
)
from ket.kernel.contracts import PartnerKind
from ket.kernel.formatting import format_date, format_money
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.money_words import amount_in_words
from ket.modules.bank.models import BankVoucher, BankVoucherKind, BankVoucherLine
from ket.posting.contracts import Voucher

_COUNTERPARTY_LABELS: dict[int, tuple[str, str, str]] = {
    BankVoucherKind.CREDIT_ADVICE: (
        "Người chuyển tiền",
        "Số tài khoản người chuyển",
        "Tại ngân hàng",
    ),
    BankVoucherKind.PAYMENT_ORDER: (
        "Đơn vị thụ hưởng",
        "Số tài khoản thụ hưởng",
        "Tại ngân hàng",
    ),
    BankVoucherKind.CHEQUE: ("Người thụ hưởng", "Số tài khoản thụ hưởng", "Tại ngân hàng"),
}
"""Nhãn ba dòng "phía bên kia" theo loại chứng từ. Chuyển tiền nội bộ không có
mặt ở đây: phía bên kia của nó là một tài khoản của CHÍNH đơn vị, in ra dưới
nhãn "thụ hưởng" sẽ đọc như tiền đã ra khỏi doanh nghiệp."""


def build_print_details(session: Session, voucher_id: UUID, user_id: int) -> DocumentPrintDetails:
    """Trường riêng của một chứng từ tiền gửi cho mẫu in."""
    voucher = session.get(Voucher, voucher_id)
    body = session.get(BankVoucher, voucher_id)
    if voucher is None or body is None:  # pragma: no cover - router đã tra chứng từ
        raise ValueError(f"Chứng từ tiền gửi {voucher_id} không còn tồn tại")
    lines = list(
        session.execute(
            select(BankVoucherLine)
            .where(BankVoucherLine.voucher_id == voucher_id)
            .order_by(BankVoucherLine.line_no)
        )
        .scalars()
        .all()
    )
    total_fc = sum((line.amount_fc for line in lines), Decimal(0))
    own = _account_info(session, body.bank_account_id)

    fields = [
        PrintField("Đơn vị trả tiền" if _pays_out(body.kind) else "Đơn vị thụ hưởng", own.holder),
        PrintField("Số tài khoản", own.number),
        PrintField("Tại ngân hàng", own.bank),
        *_counterparty_fields(session, body),
        PrintField("Nội dung", voucher.description or ""),
    ]
    if body.reference_no:
        fields.append(PrintField("Số tham chiếu", body.reference_no))
    return DocumentPrintDetails(
        header_fields=debit_credit_fields(
            session, [(line.debit_account_id, line.credit_account_id) for line in lines]
        ),
        fields=tuple(fields),
        amount=f"{format_money(total_fc, blank_zero=False)} {voucher.currency_code}",
        amount_in_words=amount_in_words(total_fc, unit=currency_unit(voucher.currency_code)),
        notes=foreign_currency_notes(
            session,
            currency_code=voucher.currency_code,
            period_id=voucher.period_id,
            exchange_rate=voucher.exchange_rate,
            amounts_fc=[line.amount_fc for line in lines],
            user_id=user_id,
        ),
    )


def _pays_out(kind: int) -> bool:
    """Tiền RỜI tài khoản của đơn vị — quyết định nhãn khối tài khoản đầu tiên."""
    return kind != BankVoucherKind.CREDIT_ADVICE


@dataclass(frozen=True)
class _AccountInfo:
    """Ba dòng in của một tài khoản ngân hàng: chủ tài khoản, số, ngân hàng."""

    holder: str
    number: str
    bank: str


_UNKNOWN_ACCOUNT = _AccountInfo("", "", "")


def _account_info(session: Session, account_id: int | None) -> _AccountInfo:
    """Tài khoản ngân hàng doanh nghiệp → ba dòng in.

    Số tài khoản là `code` của danh mục (xem `company_bank_account.py`), tên
    ngân hàng lấy từ danh mục `banks` kèm chi nhánh mở tài khoản nếu có khai.
    """
    if account_id is None:
        return _UNKNOWN_ACCOUNT
    account = session.get(CompanyBankAccount, account_id)
    if account is None:  # pragma: no cover - FK RESTRICT giữ dòng luôn còn
        return _UNKNOWN_ACCOUNT
    bank = session.get(Bank, account.bank_id)
    bank_name = bank.name if bank is not None else ""
    if account.bank_branch:
        bank_name = f"{bank_name} — {account.bank_branch}" if bank_name else account.bank_branch
    return _AccountInfo(account.account_holder or account.name, account.code, bank_name)


def _counterparty_fields(session: Session, body: BankVoucher) -> tuple[PrintField, ...]:
    """Khối "phía bên kia": tài khoản đích với chuyển nội bộ, người thụ
    hưởng/người chuyển với ba loại còn lại (FR-SYS-033)."""
    if body.kind == BankVoucherKind.INTERNAL_TRANSFER:
        target = _account_info(session, body.counter_bank_account_id)
        fields = [
            PrintField("Chuyển đến tài khoản", target.number),
            PrintField("Chủ tài khoản", target.holder),
            PrintField("Tại ngân hàng", target.bank),
        ]
    else:
        name_label, account_label, bank_label = _COUNTERPARTY_LABELS[body.kind]
        fields = [
            PrintField(name_label, body.beneficiary_name or _partner_name(session, body)),
            PrintField(account_label, body.beneficiary_account_no or ""),
            PrintField(bank_label, body.beneficiary_bank_name or ""),
        ]
    if body.kind == BankVoucherKind.CHEQUE and body.cheque_no:
        fields.append(
            PrintField("Séc số", f"{body.cheque_no} ngày {format_date(body.cheque_date)}")
        )
    return tuple(fields)


def _partner_name(session: Session, body: BankVoucher) -> str:
    """Tên đối tác — chỉ dùng khi phiếu không chụp tên "phía bên kia" thành chữ.

    Đọc danh mục ở đây (chứ không chụp thêm cột) là chấp nhận được vì đây là
    đường DỰ PHÒNG: phiếu có `beneficiary_name` luôn thắng, nên bản in của một
    ủy nhiệm chi đã gửi ngân hàng không đổi theo danh mục về sau.
    """
    if body.partner_id is None or body.partner_kind is None:
        return ""
    if body.partner_kind == PartnerKind.EMPLOYEE:
        employee = session.get(Employee, body.partner_id)
        return employee.name if employee is not None else ""
    partner = session.get(Partner, body.partner_id)
    return partner.name if partner is not None else ""
