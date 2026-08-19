"""Năm tài chính và kỳ kế toán (LD-12, FR-NFR-031/033, FR-SYS-004).

Ba nhóm bất biến:

* Tạo năm sinh **đủ** 12 kỳ liền mạch — một năm thiếu tháng chỉ lộ ra vào ngày
  đầu tiên có người nhập chứng từ tháng đó.
* Kỳ đã khóa chặn mọi đường ghi, và vết khóa nêu **ai** khóa (FR-NFR-013).
* Nhiều năm song song thì được, chồng lấn ngày thì không.
"""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    AccountingSchemeLockedError,
    FiscalYearOverlapError,
    PeriodClosedError,
    PeriodNotFoundError,
)
from ket.kernel.periods.models import (
    ADJUSTMENT_PERIOD_NO,
    PERIODS_PER_YEAR,
    AccountingPeriod,
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

ACTOR_ID = 1


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=ACTOR_ID, branch_ids=())


def _create_year(service: PeriodService, *, code: str, start: date) -> int:
    year = service.create_fiscal_year(
        code=code,
        start_date=start,
        accounting_scheme=AccountingScheme.TT99,
        base_currency="VND",
        inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
        vat_method=VatMethod.DEDUCTION,
    )
    return year.id


def test_creating_a_year_generates_twelve_contiguous_periods(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        year_id = _create_year(service, code="KY-2031", start=date(2031, 1, 1))
        periods = service.periods_of(year_id)

        assert len(periods) == PERIODS_PER_YEAR
        assert periods[0].start_date == date(2031, 1, 1)
        assert periods[-1].end_date == date(2031, 12, 31)
        # Không kẽ hở, không chồng lấn: mọi ngày trong năm thuộc đúng một kỳ.
        for earlier, later in pairwise(periods):
            assert (later.start_date - earlier.end_date).days == 1


def test_a_year_may_start_in_any_month(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Niên độ lệch năm dương lịch (01/04–31/03) là chuyện có thật ở doanh nghiệp FDI."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        year_id = _create_year(service, code="KY-LECH", start=date(2032, 4, 1))
        periods = service.periods_of(year_id)

        assert periods[0].start_date == date(2032, 4, 1)
        assert periods[-1].end_date == date(2033, 3, 31)


def test_a_year_must_start_on_the_first_of_a_month(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(ValueError, match="ngày đầu tháng"):
            _create_year(PeriodService(session), code="KY-SAI", start=date(2033, 5, 15))


def test_two_years_may_not_overlap(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        _create_year(service, code="KY-2034", start=date(2034, 1, 1))

        with pytest.raises(FiscalYearOverlapError):
            _create_year(service, code="KY-2034-B", start=date(2034, 7, 1))


def test_the_database_itself_refuses_an_overlapping_year(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chồng lấn bị chặn ở **DB**, không chỉ ở phép kiểm của ứng dụng (H1).

    Ghi thẳng qua ORM để đi vòng qua `_ensure_no_overlap` — mô phỏng đúng thứ mà
    phép kiểm ở tầng ứng dụng không thấy: hai phiên song song cùng đọc "chưa có
    gì chồng" rồi cùng chèn. Ràng buộc `EXCLUDE` là thứ duy nhất đứng giữa.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        _create_year(PeriodService(session), code="KY-EXCL", start=date(2055, 1, 1))

    with pytest.raises(IntegrityError):
        with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
            session.add(
                FiscalYear(
                    code="KY-EXCL-CHONG",
                    start_date=date(2055, 7, 1),
                    end_date=date(2056, 6, 30),
                    accounting_scheme=AccountingScheme.TT99.value,
                    base_currency="VND",
                    inventory_valuation_method=InventoryValuationMethod.FIFO.value,
                    vat_method=VatMethod.DEDUCTION.value,
                )
            )
            session.flush()


def test_a_posting_date_resolves_to_its_period(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        _create_year(service, code="KY-2035", start=date(2035, 1, 1))

        period = service.resolve_period(date(2035, 3, 17))
        assert period.period_no == 3
        assert period.start_date == date(2035, 3, 1)


def test_the_adjustment_period_is_never_picked_automatically(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Kỳ 13 trùng ngày với kỳ 12 nhưng chỉ nhận bút toán do người khóa sổ chọn.

    Tạo tay vì kỳ 13 ra đời ở phase 10a (H42) — nhưng bộ lọc trong
    `resolve_period` phải có cổng canh **từ bây giờ**, nếu không tới lúc kỳ 13
    tồn tại thật sẽ không có gì phát hiện bút toán thường rơi nhầm vào đó.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        year_id = _create_year(service, code="KY-2039", start=date(2039, 1, 1))
        session.add(
            AccountingPeriod(
                fiscal_year_id=year_id,
                period_no=ADJUSTMENT_PERIOD_NO,
                start_date=date(2039, 12, 1),
                end_date=date(2039, 12, 31),
            )
        )
        session.flush()

        assert service.resolve_period(date(2039, 12, 15)).period_no == 12


def test_a_date_outside_every_year_is_a_business_error(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        with pytest.raises(PeriodNotFoundError):
            PeriodService(session).resolve_period(date(1999, 1, 1))


def test_locking_a_period_records_who_locked_it_and_blocks_writing(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        _create_year(service, code="KY-2036", start=date(2036, 1, 1))
        period = service.resolve_period(date(2036, 2, 10))
        period_id = period.id

        service.ensure_open(period)  # còn mở thì không ném gì

        locked = service.lock_period(period_id, user_id=ACTOR_ID)
        assert locked.locked_by == ACTOR_ID
        assert locked.locked_at is not None

        with pytest.raises(PeriodClosedError):
            service.ensure_open(locked)

        reopened = service.unlock_period(period_id)
        assert reopened.locked_at is None and reopened.locked_by is None
        service.ensure_open(reopened)


def test_closing_the_year_blocks_writing_even_into_an_unlocked_period(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        year_id = _create_year(service, code="KY-2037", start=date(2037, 1, 1))
        period = service.resolve_period(date(2037, 6, 1))
        service.ensure_open(period)  # kỳ chưa khóa riêng

        session.get_one(FiscalYear, year_id).is_closed = True
        session.flush()

        with pytest.raises(PeriodClosedError, match="quyết toán"):
            service.ensure_open(period)


def test_locking_a_period_twice_never_overwrites_who_locked_it(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Vết khóa là câu trả lời "ai khóa" — khóa đè lên nó là xóa bằng chứng (C2)."""
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        _create_year(service, code="KY-2040", start=date(2040, 1, 1))
        period = service.resolve_period(date(2040, 4, 1))
        locked = service.lock_period(period.id, user_id=ACTOR_ID)

        with pytest.raises(PeriodClosedError, match="đã khóa rồi"):
            service.lock_period(period.id, user_id=99)

        assert service.periods_of(locked.fiscal_year_id)[3].locked_by == ACTOR_ID


def test_a_closed_year_refuses_to_have_its_periods_unlocked(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Quyết toán năm phải chặn cả đường đi vòng qua kỳ con (C2).

    Nếu không thì cam kết "số của niên độ này đã cố định" chỉ cần hai cú bấm là
    lách được, và không có gì trong sổ nói rằng nó đã bị lách.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        year_id = _create_year(service, code="KY-2041", start=date(2041, 1, 1))
        period = service.resolve_period(date(2041, 5, 1))
        service.lock_period(period.id, user_id=ACTOR_ID)

        session.get_one(FiscalYear, year_id).is_closed = True
        session.flush()

        with pytest.raises(PeriodClosedError, match="quyết toán"):
            service.unlock_period(period.id)
        with pytest.raises(PeriodClosedError, match="quyết toán"):
            service.lock_period(period.id, user_id=ACTOR_ID)


def test_changing_the_accounting_scheme_after_documents_exist_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """FR-SYS-004, nguyên tắc U14 "chốt một lần".

    `has_documents` do nơi gọi truyền vào: kernel không được biết bảng chứng từ
    nào tồn tại (luật phụ thuộc #5).
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        service = PeriodService(session)
        year_id = _create_year(service, code="KY-2038", start=date(2038, 1, 1))

        service.ensure_scheme_changeable(year_id, has_documents=False)
        with pytest.raises(AccountingSchemeLockedError):
            service.ensure_scheme_changeable(year_id, has_documents=True)
