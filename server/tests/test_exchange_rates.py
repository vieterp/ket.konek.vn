"""Tra tỷ giá — và từ chối khi không có (N5, FR-NFR-032).

Bài kiểm quan trọng nhất ở đây là bài **không** có kết quả: thiếu tỷ giá phải
thành lỗi nghiệp vụ, không thành tỷ giá 1. Đây là kiểu sai chỉ lộ ra khi đối
chiếu công nợ vài tháng sau, lúc chứng từ đã khóa kỳ.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.currency.exchange_rate_service import ExchangeRateService
from ket.kernel.currency.models import Currency, ExchangeRate, RateType
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import BaseCurrencyMisconfiguredError, ExchangeRateNotFoundError
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


@pytest.fixture(scope="module")
def money_dataset(session_factory: sessionmaker[Session], dataset_beta: DatasetRef) -> DatasetRef:
    """Dữ liệu kế toán có đồng tiền hạch toán VND và một dải tỷ giá USD.

    Dùng `dataset_beta` để không đụng vào `dataset_alpha` — chỉ mục "đúng một
    đồng tiền hạch toán" là ràng buộc **toàn schema**, nên hai bộ test cùng
    dựng đồng tiền gốc trên một schema sẽ chặn nhau.

    Phạm vi `module` vì cùng lý do: gieo lại đồng tiền gốc cho mỗi test sẽ đụng
    chính khóa chính của nó. Hệ quả là các test ở đây **dùng chung** dữ liệu
    này, nên test nào ghi thì phải ghi vào một ngày riêng của nó.
    """
    with unit_of_work(session_factory, _scope(dataset_beta)) as session:
        session.add(Currency(code="VND", name="Việt Nam đồng", decimal_places=0, is_base=True))
        session.add(Currency(code="USD", name="Đô la Mỹ", decimal_places=2))
        session.flush()

        service = ExchangeRateService(session)
        service.upsert_rate(
            currency_code="USD", on_date=date(2026, 8, 10), rate=Decimal("25000.000000")
        )
        service.upsert_rate(
            currency_code="USD", on_date=date(2026, 8, 14), rate=Decimal("25400.500000")
        )
        service.upsert_rate(
            currency_code="USD",
            on_date=date(2026, 8, 14),
            rate=Decimal("25600.000000"),
            rate_type=RateType.BANK_SELL,
        )
    return dataset_beta


def test_the_rate_of_the_exact_day_is_used(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        rate = ExchangeRateService(session).resolve(currency_code="USD", on_date=date(2026, 8, 14))
        assert rate == Decimal("25400.500000")


def test_a_day_without_a_rate_falls_back_to_the_most_recent_earlier_one(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    """Ngân hàng không công bố tỷ giá Chủ nhật; kế toán vẫn ghi sổ ngày đó."""
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        rate = ExchangeRateService(session).resolve(currency_code="USD", on_date=date(2026, 8, 12))
        assert rate == Decimal("25000.000000")


def test_a_later_rate_is_never_used_for_an_earlier_date(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    """Dùng tỷ giá của ngày sau là dùng thông tin chưa tồn tại lúc phát sinh."""
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        with pytest.raises(ExchangeRateNotFoundError):
            ExchangeRateService(session).resolve(currency_code="USD", on_date=date(2026, 8, 9))


def test_each_rate_type_is_looked_up_separately(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        service = ExchangeRateService(session)
        assert service.resolve(
            currency_code="USD", on_date=date(2026, 8, 14), rate_type=RateType.BANK_SELL
        ) == Decimal("25600.000000")
        with pytest.raises(ExchangeRateNotFoundError):
            service.resolve(
                currency_code="USD", on_date=date(2026, 8, 14), rate_type=RateType.BANK_BUY
            )


def test_a_missing_rate_is_a_business_error_not_a_rate_of_one(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    """Tiêu chí phase 3: thiếu tỷ giá → lỗi rõ ràng, **không** tự dùng 1."""
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        session.add(Currency(code="EUR", name="Euro"))
        session.flush()

        with pytest.raises(ExchangeRateNotFoundError) as failure:
            ExchangeRateService(session).resolve(currency_code="EUR", on_date=date(2026, 8, 14))
        assert failure.value.details["currency_code"] == "EUR"
        assert failure.value.error_code == "currency.exchange_rate_not_found"


def test_the_base_currency_converts_at_one_without_a_lookup(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        assert ExchangeRateService(session).resolve(
            currency_code="VND", on_date=date(2026, 8, 14)
        ) == Decimal(1)


def test_updating_a_rate_replaces_the_row_for_that_day(
    session_factory: sessionmaker[Session], money_dataset: DatasetRef
) -> None:
    """Sửa tại chỗ, không chèn dòng thứ hai: khóa chính đã là bộ ba.

    Dùng một ngày riêng (20/08) chứ không sửa tỷ giá mà các test khác đang đọc —
    dữ liệu gieo là của cả module.
    """
    on_date = date(2026, 8, 20)
    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        service = ExchangeRateService(session)
        service.upsert_rate(currency_code="USD", on_date=on_date, rate=Decimal("25500.000000"))
        service.upsert_rate(currency_code="USD", on_date=on_date, rate=Decimal("25450.000000"))

    with unit_of_work(session_factory, _scope(money_dataset)) as session:
        assert ExchangeRateService(session).resolve(
            currency_code="USD", on_date=on_date
        ) == Decimal("25450.000000")
        rows = session.execute(
            select(ExchangeRate).where(ExchangeRate.rate_date == on_date)
        ).scalars()
        assert len(list(rows)) == 1


def test_a_dataset_without_a_base_currency_refuses_to_convert(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(BaseCurrencyMisconfiguredError):
            ExchangeRateService(session).base_currency()
