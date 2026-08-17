"""Tra tỷ giá cho một ngày — và **từ chối** khi không có (N5, FR-NFR-032).

Luật duy nhất đáng nhớ ở module này: không có tỷ giá thì báo lỗi, không bao giờ
lấy 1. Tỷ giá mặc định 1 biến một hóa đơn 5.000 USD thành 5.000 VND — sai đúng
ba bậc, và sai một cách không để lại dấu vết nào trong sổ. Kế toán chỉ phát hiện
ra khi đối chiếu công nợ vài tháng sau, lúc chứng từ đã khóa kỳ.

Quy tắc tra: lấy tỷ giá **gần nhất không muộn hơn** ngày hạch toán. Ngân hàng
không công bố tỷ giá vào Chủ nhật và ngày lễ, nên đòi đúng ngày sẽ chặn việc ghi
sổ những ngày đó; lấy tỷ giá gần nhất trước đó là đúng cách kế toán làm bằng tay.
Chiều ngược lại thì **không** được nới: dùng tỷ giá của ngày sau ngày hạch toán
nghĩa là dùng một thông tin chưa tồn tại lúc nghiệp vụ phát sinh.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.currency.models import Currency, ExchangeRate, RateType
from ket.kernel.errors import BaseCurrencyMisconfiguredError, ExchangeRateNotFoundError


class ExchangeRateService:
    """Tra tỷ giá và đồng tiền hạch toán trong transaction đang mở."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def base_currency(self) -> Currency:
        """Đồng tiền hạch toán của dữ liệu kế toán này (LD-12).

        Không có dòng nào `is_base` là **lỗi cấu hình chặn mọi việc**, không phải
        một trường hợp trả `None` để nơi gọi tự xoay: mọi phép ghi sổ đều cần
        biết quy đổi về đâu, nên để `None` chảy tiếp chỉ dời chỗ nổ sang một
        `AttributeError` khó hiểu ở giữa luồng ghi chứng từ.
        """
        currency = self._session.execute(
            select(Currency).where(Currency.is_base)
        ).scalar_one_or_none()
        if currency is None:
            raise BaseCurrencyMisconfiguredError(
                "Chưa khai đồng tiền hạch toán — hãy đặt một loại tiền làm đồng tiền ghi sổ"
            )
        return currency

    def resolve(
        self,
        *,
        currency_code: str,
        on_date: date,
        rate_type: RateType = RateType.ACCOUNTING,
    ) -> Decimal:
        """Tỷ giá áp dụng cho `currency_code` tại `on_date`.

        Đồng tiền hạch toán trả 1 **không tra bảng**: quy đổi VND sang VND luôn
        là 1, và đòi người dùng nhập một dòng tỷ giá VND→VND mỗi ngày là một
        công việc vô nghĩa mà quên làm thì cả hệ thống dừng.
        """
        if currency_code == self.base_currency().code:
            return Decimal(1)

        rate = self._session.execute(
            select(ExchangeRate.rate)
            .where(ExchangeRate.currency_code == currency_code)
            .where(ExchangeRate.rate_type == rate_type.value)
            .where(ExchangeRate.rate_date <= on_date)
            .order_by(ExchangeRate.rate_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if rate is None:
            raise ExchangeRateNotFoundError(
                "Chưa có tỷ giá cho loại tiền này tại ngày hạch toán — hãy nhập tỷ giá "
                "rồi ghi sổ lại",
                currency_code=currency_code,
                rate_type=rate_type.value,
                on_date=on_date.isoformat(),
            )
        return rate

    def upsert_rate(
        self,
        *,
        currency_code: str,
        on_date: date,
        rate: Decimal,
        rate_type: RateType = RateType.ACCOUNTING,
    ) -> ExchangeRate:
        """Ghi hoặc sửa tỷ giá của đúng một ngày.

        Sửa tại chỗ chứ không chèn thêm dòng: khóa chính đã là bộ ba (tiền, ngày,
        loại), nên "sửa tỷ giá ngày 31/12" phải là một dòng đổi giá trị — và
        `Audited` giữ lại giá trị cũ để giải trình.
        """
        existing = self._session.get(ExchangeRate, (currency_code, on_date, rate_type.value))
        if existing is not None:
            existing.rate = rate
            self._session.flush()
            return existing

        record = ExchangeRate(
            currency_code=currency_code,
            rate_date=on_date,
            rate_type=rate_type.value,
            rate=rate,
        )
        self._session.add(record)
        self._session.flush()
        return record
