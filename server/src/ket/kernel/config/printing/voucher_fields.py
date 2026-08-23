"""Mảnh dùng chung khi module dựng `DocumentPrintDetails` cho một chứng từ.

Ba mảnh dưới đây có mặt trên **mọi** biểu mẫu chứng từ tiền tệ (01-TT, 02-TT,
ủy nhiệm chi, giấy báo có…): khối "Nợ:/Có:" góc phải, tên đơn vị tiền đọc
trong câu "(Viết bằng chữ)", và khối chân trang "+ Tỷ giá ngoại tệ / + Số tiền
quy đổi". Đặt ở kernel vì phân hệ Quỹ và phân hệ Ngân hàng đều cần — mà
`import-linter` C3 cấm hai module nghiệp vụ import nhau, nên bản chép tay thứ
hai là kết cục duy nhất còn lại nếu không tách ra đây.

Hàm nhận **giá trị trần** (mã tiền tệ, `period_id`, tỷ giá) chứ không nhận
`Voucher`: kernel không được import `posting` (C1).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.catalog import MONEY_SCALE_KEY
from ket.kernel.config.printing.context import PrintField
from ket.kernel.config.settings_service import value_of
from ket.kernel.formatting import format_money, format_quantity
from ket.kernel.money import convert_currency
from ket.kernel.periods.service import base_currency_of_period

RATE_DECIMALS = 4
"""Số chữ số thập phân của tỷ giá trên bản in — đủ cho tỷ giá ngân hàng công
bố, và giữ nguyên phần mà bút toán đã dùng thay vì làm tròn cho gọn mắt."""


@dataclass(frozen=True)
class MoneyLine:
    """Một dòng định khoản rút gọn, đủ cho mọi phép tính của bản in.

    Module có kiểu dòng riêng (`CashVoucherLine`, `BankVoucherLine`) mà kernel
    không được biết; ba trường này là tất cả những gì tờ giấy cần. Đưa CÙNG một
    danh sách vào mọi hàm dưới đây để khối "Nợ/Có", dòng "Số tiền" và khối
    ngoại tệ không bao giờ nói về hai tập dòng khác nhau.
    """

    debit_account_id: int | None
    credit_account_id: int | None
    amount_fc: Decimal


def money_side_amounts(
    lines: Sequence[MoneyLine], *, account_ids: Collection[int]
) -> tuple[Decimal, ...]:
    """Số tiền nguyên tệ của những dòng CHẠM tài khoản tiền, mang dấu.

    Vào quỹ/tài khoản (`debit`) là dương, ra là âm. Dòng không chạm tài khoản
    tiền **không góp gì** — và đó là toàn bộ lý do hàm này tồn tại (review 6E-2,
    H-1): `posting_mapper` cố ý cho phép dòng như vậy, còn FR-QUY-007 khai đúng
    một nghiệp vụ chính thống dùng nó (chiết khấu thanh toán `Nợ 635/Có 131`
    nằm chung phiếu thu). Cộng cả những dòng ấy vào ô "Số tiền" thì tờ phiếu có
    chữ ký đọc số lớn hơn số thật vào két, trong khi sổ quỹ
    (`treasurer_source._cash_side_totals`) ghi đúng — hai con số cho một sự
    việc, và bản sai là bản có chữ ký người nộp tiền.

    `account_ids` là **tập** vì hai phân hệ trả lời câu "tài khoản tiền là cái
    nào" theo hai cách: phiếu thu/chi có đúng một TK quỹ trên thân phiếu, còn
    chứng từ tiền gửi nhận diện theo tiền tố số hiệu 112x (một chứng từ có thể
    chạm hai tài khoản ngân hàng). Dòng chạm cả hai bên trong tập tự triệt
    tiêu — đúng như sổ quỹ ghi số RÒNG cho phiếu hai chiều (lát 6C).

    **Không dòng nào chạm tài khoản tiền thì trả về TOÀN BỘ dòng**, không trả
    về rỗng (review pre-landing 6E-2): rỗng nghĩa là ô "Số tiền" in ra `0` trên
    một tờ giấy có chỗ ký — im lặng và sai, tệ hơn hẳn con số thừa mà H-1 sinh
    ra. Hình dạng hợp lệ rơi vào nhánh này có thật: bản nháp chưa điền bên quỹ,
    dòng ghi tiểu khoản `1111` trong khi thân phiếu khai `111`, chứng từ tiền
    gửi không dòng nào chạm 112. Ở đó không có câu trả lời "phía tiền" nào để
    nói, nên tổng số tiền người dùng đã gõ là câu trả lời trung thực nhất.
    """
    amounts: list[Decimal] = []
    for line in lines:
        signed = Decimal(0)
        if line.debit_account_id in account_ids:
            signed += line.amount_fc
        if line.credit_account_id in account_ids:
            signed -= line.amount_fc
        if signed != 0:
            amounts.append(signed)
    return tuple(amounts) or tuple(line.amount_fc for line in lines)


def debit_credit_fields(session: Session, lines: Sequence[MoneyLine]) -> tuple[PrintField, ...]:
    """Khối "Nợ:/Có:" từ các dòng định khoản.

    Tờ giấy chỉ có một cặp; phần mềm cho nhiều dòng, nên ô liệt kê các tài
    khoản đã dùng theo thứ tự dòng, bỏ trùng ("1111, 1121" chứ không "1111,
    1111"). Dòng nháp còn thiếu một bên chỉ đơn giản không đóng góp mã nào.
    """
    pairs = [(line.debit_account_id, line.credit_account_id) for line in lines]
    account_ids = {account_id for pair in pairs for account_id in pair if account_id is not None}
    if not account_ids:
        return ()
    codes: dict[int, str] = {
        row.id: row.code
        for row in session.execute(
            select(ChartOfAccount.id, ChartOfAccount.code).where(ChartOfAccount.id.in_(account_ids))
        ).all()
    }
    return (
        PrintField("Nợ", _distinct_codes(codes, [pair[0] for pair in pairs])),
        PrintField("Có", _distinct_codes(codes, [pair[1] for pair in pairs])),
    )


def _distinct_codes(codes: dict[int, str], account_ids: Sequence[int | None]) -> str:
    seen: list[str] = []
    for account_id in account_ids:
        if account_id is None:
            continue
        code = codes.get(account_id)
        if code is not None and code not in seen:
            seen.append(code)
    return ", ".join(seen)


def currency_unit(currency_code: str) -> str:
    """Tên đơn vị tiền đọc trong câu "(Viết bằng chữ)".

    Chỉ VND có tên tiếng Việt quen thuộc; ngoại tệ đọc thẳng mã ISO ("… một
    nghìn hai trăm USD") thay vì đoán tên tiếng Việt cho từng đồng tiền — danh
    mục tiền tệ là dữ liệu người dùng tự khai, không có cột tên đọc.
    """
    return "đồng" if currency_code == "VND" else currency_code


def foreign_currency_notes(
    session: Session,
    *,
    currency_code: str,
    period_id: int,
    exchange_rate: Decimal,
    amounts_fc: Sequence[Decimal],
    user_id: int,
) -> tuple[PrintField, ...]:
    """Khối chân trang "+ Tỷ giá ngoại tệ", "+ Số tiền quy đổi".

    Chỉ in khi chứng từ THỰC SỰ là ngoại tệ (so với đồng hạch toán của năm),
    không phải khi tỷ giá khác 1 — một dữ liệu hạch toán bằng USD có tỷ giá
    USD→USD bằng 1 và vẫn không cần hai dòng này.

    "Số tiền quy đổi" cộng theo TỪNG DÒNG đã quy đổi, đúng như `PostingService`
    làm (`convert_currency` mỗi dòng rồi mới cộng): quy đổi trên số tổng lệch
    số đã ghi sổ đúng bằng phần làm tròn, và bản in nói khác sổ là bản in sai.

    `amounts_fc` là kết quả của `money_side_amounts` — mang dấu, và chỉ gồm
    dòng chạm tài khoản tiền. In trị tuyệt đối vì tờ phiếu đã nói chiều bằng
    chính tên của nó (phiếu THU hay phiếu CHI).
    """
    if currency_code == base_currency_of_period(session, period_id):
        return ()
    scale_value = value_of(session, key=MONEY_SCALE_KEY, user_id=user_id)
    scale = scale_value if isinstance(scale_value, int) else 2
    converted = abs(
        sum((convert_currency(amount, exchange_rate, scale) for amount in amounts_fc), Decimal(0))
    )
    return (
        PrintField("Tỷ giá ngoại tệ", format_quantity(exchange_rate, decimals=RATE_DECIMALS)),
        PrintField("Số tiền quy đổi", format_money(converted, blank_zero=False)),
    )
