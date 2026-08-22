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

from collections.abc import Sequence
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


def debit_credit_fields(
    session: Session, pairs: Sequence[tuple[int | None, int | None]]
) -> tuple[PrintField, ...]:
    """Khối "Nợ:/Có:" từ các cặp `(debit_account_id, credit_account_id)`.

    Tờ giấy chỉ có một cặp; phần mềm cho nhiều dòng, nên ô liệt kê các tài
    khoản đã dùng theo thứ tự dòng, bỏ trùng ("1111, 1121" chứ không "1111,
    1111"). Dòng nháp còn thiếu một bên chỉ đơn giản không đóng góp mã nào.
    """
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
    """
    if currency_code == base_currency_of_period(session, period_id):
        return ()
    scale_value = value_of(session, key=MONEY_SCALE_KEY, user_id=user_id)
    scale = scale_value if isinstance(scale_value, int) else 2
    converted = sum(
        (convert_currency(amount, exchange_rate, scale) for amount in amounts_fc), Decimal(0)
    )
    return (
        PrintField("Tỷ giá ngoại tệ", format_quantity(exchange_rate, decimals=RATE_DECIMALS)),
        PrintField("Số tiền quy đổi", format_money(converted, blank_zero=False)),
    )
