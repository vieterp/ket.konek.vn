"""`period_summary` (lát 7G-5) — bốn số của biên bản đối chiếu, ghép THEO KHOẢN.

Bài thuần Python trên `OpenItem` dựng tay: cái cần ghim là phép ghép hai mốc
đọc, không phải SQL (dataset đã có bài riêng). Mỗi ca kết bằng đẳng thức
`đầu + tăng − giảm = cuối`, và ca đầu tiên là chính lỗi review 7G-5 H-1: kỳ
vắt qua lượt chuyển số dư sang niên độ mới — dòng chuyển sang mang khóa MỚI,
giá trị = phần còn lại lúc chuyển, đã trả = chỉ năm nay — mà bản trừ-hai-tổng in
ra "phát sinh giảm" âm.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from ket.api.open_items import OpenItem, PeriodSummary, period_summary

FROM = date(2026, 1, 1)
TO = date(2026, 12, 31)


def _item(
    *,
    item_id: UUID | None = None,
    document_date: date | None,
    amount: int,
    settled: int,
    target_kind: int = 0,
) -> OpenItem:
    return OpenItem(
        item_id=item_id or uuid4(),
        document_id=None,
        target_kind=target_kind,
        source_label="Hóa đơn bán",
        document_no="HD",
        document_date=document_date,
        due_date=None,
        partner_kind=0,
        partner_id=1,
        partner_code="KH",
        partner_name="Khách",
        currency_code="VND",
        amount_fc=Decimal(amount),
        settled_fc=Decimal(settled),
        remaining_fc=Decimal(amount - settled),
        amount=Decimal(amount),
        settled=Decimal(settled),
        remaining=Decimal(amount - settled),
        days_overdue=None,
    )


def _holds(summary: PeriodSummary) -> None:
    assert summary.opening + summary.increase - summary.decrease == summary.closing, summary


def test_a_carried_invoice_across_the_fiscal_year_keeps_the_identity() -> None:
    """Hóa đơn 100 năm trước, trả 30 năm trước, chuyển sang 70, trả thêm 20 năm nay."""
    before = [_item(document_date=date(2025, 6, 1), amount=100, settled=30)]
    at = [_item(document_date=date(2025, 6, 1), amount=70, settled=20, target_kind=2)]
    summary = period_summary(before=before, at=at, from_date=FROM, to_date=TO)
    assert summary == PeriodSummary(
        opening=Decimal(70), increase=Decimal(0), decrease=Decimal(20), closing=Decimal(50)
    )
    _holds(summary)


def test_a_new_invoice_inside_the_period_is_an_increase() -> None:
    at = [_item(document_date=date(2026, 3, 1), amount=100, settled=40)]
    summary = period_summary(before=[], at=at, from_date=FROM, to_date=TO)
    assert (summary.opening, summary.increase, summary.decrease, summary.closing) == (
        Decimal(0),
        Decimal(100),
        Decimal(40),
        Decimal(60),
    )
    _holds(summary)


def test_an_invoice_fully_settled_inside_the_period_is_a_decrease_not_a_disappearance() -> None:
    shared = uuid4()
    before = [_item(item_id=shared, document_date=date(2025, 6, 1), amount=100, settled=0)]
    at = [_item(item_id=shared, document_date=date(2025, 6, 1), amount=100, settled=100)]
    summary = period_summary(before=before, at=at, from_date=FROM, to_date=TO)
    assert (summary.opening, summary.decrease, summary.closing) == (
        Decimal(100),
        Decimal(100),
        Decimal(0),
    )
    _holds(summary)


def test_a_settlement_posted_after_the_period_changes_nothing() -> None:
    shared = uuid4()
    before = [_item(item_id=shared, document_date=date(2025, 6, 1), amount=100, settled=10)]
    at = [_item(item_id=shared, document_date=date(2025, 6, 1), amount=100, settled=10)]
    summary = period_summary(before=before, at=at, from_date=FROM, to_date=TO)
    assert summary.decrease == Decimal(0)
    assert summary.opening == summary.closing == Decimal(90)
    _holds(summary)


def test_an_opening_advance_without_a_date_sits_in_the_opening_balance() -> None:
    at = [_item(document_date=None, amount=300, settled=0, target_kind=7)]
    summary = period_summary(before=[], at=at, from_date=FROM, to_date=TO)
    assert summary.opening == Decimal(300)
    assert summary.increase == Decimal(0)
    _holds(summary)


def test_an_opening_balance_invoice_dated_inside_the_period_is_not_counted_twice() -> None:
    """Dòng số dư ban đầu có ngày hóa đơn trong kỳ: có ở `before` nên thuộc đầu kỳ."""
    shared = uuid4()
    before = [
        _item(item_id=shared, document_date=date(2026, 2, 1), amount=100, settled=0, target_kind=2)
    ]
    at = [
        _item(item_id=shared, document_date=date(2026, 2, 1), amount=100, settled=25, target_kind=2)
    ]
    summary = period_summary(before=before, at=at, from_date=FROM, to_date=TO)
    assert summary.opening == Decimal(100)
    assert summary.increase == Decimal(0)
    assert summary.decrease == Decimal(25)
    _holds(summary)
