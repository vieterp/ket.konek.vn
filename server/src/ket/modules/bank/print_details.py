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

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import (
    DEPOSIT_ACCOUNT_CODE_PREFIX,
    ChartOfAccount,
)
from ket.kernel.config.printing.context import DocumentPrintDetails, PrintField
from ket.kernel.config.printing.voucher_fields import (
    MoneyLine,
    currency_unit,
    debit_credit_fields,
    foreign_currency_notes,
    money_side_amounts,
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
    money_lines = tuple(
        MoneyLine(line.debit_account_id, line.credit_account_id, line.amount_fc) for line in lines
    )
    money_amounts = _money_amounts(session, body, money_lines)
    total_fc = abs(sum(money_amounts, Decimal(0)))
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
        header_fields=debit_credit_fields(session, money_lines),
        fields=tuple(fields),
        amount=f"{format_money(total_fc, blank_zero=False)} {voucher.currency_code}",
        amount_in_words=amount_in_words(total_fc, unit=currency_unit(voucher.currency_code)),
        notes=foreign_currency_notes(
            session,
            currency_code=voucher.currency_code,
            period_id=voucher.period_id,
            exchange_rate=voucher.exchange_rate,
            amounts_fc=money_amounts,
            user_id=user_id,
        ),
    )


def _money_amounts(
    session: Session, body: BankVoucher, lines: Sequence[MoneyLine]
) -> tuple[Decimal, ...]:
    """Số tiền nguyên tệ THẬT vào/ra tài khoản ngân hàng, mang dấu (review H-1).

    Một ủy nhiệm chi kèm dòng phí ngân hàng hạch toán thẳng vào 642 thì số tiền
    chuyển đi vẫn là số của riêng dòng chạm 112x — cộng cả dòng phí vào ô "Số
    tiền" là tờ giấy nói ngân hàng trích nhiều hơn thực tế.

    Phía tiền nhận diện theo **tiền tố số hiệu** `112`, không theo
    `bank_account_id`: cột ấy trỏ danh mục tài khoản ngân hàng, còn dòng định
    khoản mang TK kế toán — hai không gian id khác nhau.

    Chuyển tiền nội bộ chạm 112 ở CẢ HAI bên (tiền không rời doanh nghiệp) nên
    số ròng bằng 0. Số đáng in là số **ĐẾN** tài khoản đích, tức bên **Nợ** —
    cùng luật quy chủ mà `posting_mapper._deposit_owner` khai lúc ghi sổ
    ("chuyển nội bộ thì dòng Nợ thuộc TK đích"). Không lấy bên Có: một lệnh
    chuyển kèm dòng phí ngân hàng (`Nợ 642/Có 1121`) có HAI dòng ghi Có 112x,
    và cộng cả hai là tờ giấy nói đã chuyển đi nhiều hơn số tài khoản đích nhận
    (review pre-landing 6E-2). Bên Nợ cũng là bên đúng khi tiền đến từ tài khoản
    không phải 112 (`Nợ 1121/Có 113`).

    Không dòng nào chạm 112 thì `money_side_amounts` trả toàn bộ dòng — thà in
    tổng người dùng đã gõ còn hơn in `0` trên tờ giấy có chỗ ký.
    """
    account_ids = _deposit_account_ids(session, lines)
    if body.kind == BankVoucherKind.INTERNAL_TRANSFER:
        arriving = tuple(line.amount_fc for line in lines if line.debit_account_id in account_ids)
        return arriving or tuple(line.amount_fc for line in lines)
    return money_side_amounts(lines, account_ids=account_ids)


def _deposit_account_ids(session: Session, lines: Sequence[MoneyLine]) -> frozenset[int]:
    """Những TK trên chứng từ có số hiệu bắt đầu bằng `112`."""
    candidates = {
        account_id
        for line in lines
        for account_id in (line.debit_account_id, line.credit_account_id)
        if account_id is not None
    }
    if not candidates:
        return frozenset()
    rows = session.execute(
        select(ChartOfAccount.id, ChartOfAccount.code).where(ChartOfAccount.id.in_(candidates))
    ).all()
    return frozenset(row.id for row in rows if row.code.startswith(DEPOSIT_ACCOUNT_CODE_PREFIX))


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
