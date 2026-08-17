"""Số tiền ngoại tệ đi kèm tỷ giá và số quy đổi — ba giá trị, một đối tượng.

Vì sao gói cả ba lại thay vì truyền ba tham số: chúng chỉ đúng khi **nhất quán
với nhau**, và một hàm nhận ba tham số rời sẽ nhận được bộ ba không nhất quán
đúng vào ngày ai đó sửa tỷ giá ở một nhánh mã mà quên tính lại số quy đổi ở
nhánh kia. Ở đây bất biến được kiểm ngay lúc dựng đối tượng, nên một `MoneyFc`
tồn tại là một bộ ba đã đúng — không nơi nào phải kiểm lại.

`frozen=True` vì đây là **giá trị**, không phải bản ghi: sửa tỷ giá của một
chứng từ đã ghi là tạo ra một giá trị mới rồi ghi đè, chứ không phải đổi tại chỗ
một đối tượng mà chỗ khác đang giữ tham chiếu.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ket.kernel.money import MONEY_SCALE_DEFAULT, convert_currency


@dataclass(frozen=True, slots=True)
class MoneyFc:
    """Một số tiền nguyên tệ cùng tỷ giá và số quy đổi tương ứng."""

    currency: str
    amount_fc: Decimal
    rate: Decimal
    amount_base: Decimal
    scale: int = MONEY_SCALE_DEFAULT

    def __post_init__(self) -> None:
        """Bất biến: `amount_base` phải đúng bằng kết quả quy đổi.

        Kiểm ở đây chứ không bằng một hàm `validate()` mà nơi gọi phải nhớ gọi:
        bất biến kiểm theo tùy chọn là bất biến sẽ không được kiểm ở đúng nhánh
        mã hiếm chạy — mà nhánh hiếm là chỗ lệch số sống lâu nhất trước khi bị
        phát hiện.

        `ValueError` chứ không `DomainError`: người dùng không sửa được lỗi này
        và cũng không gây ra được nó từ giao diện. Nó là lỗi lập trình, và phải
        nổ to ở log thay vì thành một thông điệp dịu dàng bằng tiếng Việt.
        """
        expected = convert_currency(self.amount_fc, self.rate, self.scale)
        if expected != self.amount_base:
            raise ValueError(
                f"Số quy đổi không khớp: {self.amount_fc} × {self.rate} làm tròn "
                f"{self.scale} chữ số = {expected}, nhưng amount_base = {self.amount_base}"
            )

    @classmethod
    def convert(
        cls,
        *,
        currency: str,
        amount_fc: Decimal,
        rate: Decimal,
        scale: int = MONEY_SCALE_DEFAULT,
    ) -> MoneyFc:
        """Dựng từ nguyên tệ và tỷ giá — đường gọi thường dùng."""
        return cls(
            currency=currency,
            amount_fc=amount_fc,
            rate=rate,
            amount_base=convert_currency(amount_fc, rate, scale),
            scale=scale,
        )

    # Cố ý **không** có `MoneyFc.base(currency=..., amount=...)` cho "số tiền bằng
    # đồng tiền hạch toán, tỷ giá 1".
    #
    # Một lối tắt như vậy trông vô hại vì phần lớn chứng từ của doanh nghiệp Việt
    # Nam là VND. Nhưng nó nhận **mã tiền nào cũng được** và trả về tỷ giá 1 mà
    # không hỏi ai: `base(currency="USD", amount=5000)` biến 5.000 USD thành 5.000
    # VND, không lỗi, không vết — đúng thảm họa mà `ExchangeRateNotFoundError`
    # được dựng ra để chặn. Muốn an toàn thì lối tắt phải biết đồng tiền hạch
    # toán là gì, mà điều đó chỉ đọc được từ DB — tức là nó thôi không còn là
    # lối tắt nữa.
    #
    # Vì vậy chỉ có **một** đường tới tỷ giá 1: `ExchangeRateService.resolve`, nơi
    # so mã tiền với `currencies.is_base` trước khi trả về. Nó cũng không tra bảng
    # tỷ giá cho đồng hạch toán, nên đường VND vẫn rẻ đúng như mong muốn.
