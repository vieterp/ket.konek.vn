"""Biên vào của posting engine: `PostingLine`/`PostingDimensions` và hai validator
không cần DB (cân Nợ/Có, kiểm tài khoản).

Phần cần DB (chiều bắt buộc, giá trị chiều `list`) nằm trong test tích hợp —
ở đây là bảng giá trị biên chạy nhanh trong CI không có PostgreSQL.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, DetailTracking
from ket.posting.engine.dimensions import PartnerKind, PostingDimensions
from ket.posting.engine.prepared import PreparedLine
from ket.posting.engine.requests import PostingLine
from ket.posting.engine.validators.account import check_accounts
from ket.posting.engine.validators.balanced import check_balanced

VND = "VND"
ONE = Decimal(1)


def _line(
    *,
    account_id: int = 1,
    debit: Decimal = Decimal(0),
    credit: Decimal = Decimal(0),
    currency: str = VND,
    rate: Decimal = ONE,
    dimensions: PostingDimensions | None = None,
) -> PostingLine:
    return PostingLine(
        account_id=account_id,
        debit_fc=debit,
        credit_fc=credit,
        currency=currency,
        rate=rate,
        dimensions=dimensions or PostingDimensions(),
    )


def _prepared(line: PostingLine, *, ledger: int = 0, line_no: int = 1) -> PreparedLine:
    return PreparedLine(
        ledger=ledger,
        line_no=line_no,
        source=line,
        debit=line.debit_fc * line.rate,
        credit=line.credit_fc * line.rate,
    )


class TestLineBoundary:
    def test_negative_amounts_are_refused(self) -> None:
        with pytest.raises(ValidationError, match="không được âm"):
            _line(debit=Decimal(-1))

    def test_a_line_cannot_carry_both_sides(self) -> None:
        with pytest.raises(ValidationError, match="vừa Nợ vừa Có"):
            _line(debit=Decimal(1), credit=Decimal(1))

    def test_rate_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="Tỷ giá"):
            _line(debit=Decimal(1), rate=Decimal(0))

    def test_partner_id_and_kind_live_together(self) -> None:
        with pytest.raises(ValidationError, match="partner_kind"):
            PostingDimensions(partner_id=7)
        with pytest.raises(ValidationError, match="partner_kind"):
            PostingDimensions(partner_kind=PartnerKind.CUSTOMER)


class TestTrackingAnswer:
    def test_customer_tracking_requires_the_right_partner_kind(self) -> None:
        """TK 131 điền nhà cung cấp là công nợ nhầm sổ — phải trả lời "thiếu"."""
        vendor = PostingDimensions(partner_id=7, partner_kind=PartnerKind.VENDOR)
        assert not vendor.has_tracking(DetailTracking.CUSTOMER)
        assert vendor.has_tracking(DetailTracking.VENDOR)

    def test_unknown_tracking_value_is_a_config_bug_and_explodes(self) -> None:
        with pytest.raises(ValueError, match="không nhận ra"):
            PostingDimensions().has_tracking("khong_ton_tai")


class TestBalanced:
    def test_gap_is_reported_per_ledger_with_the_amount(self) -> None:
        lines = [
            _prepared(_line(debit=Decimal(120_000))),
            _prepared(_line(credit=Decimal(100_000)), line_no=2),
        ]
        violations = check_balanced(lines)
        assert [v.code for v in violations] == ["posting.unbalanced", "posting.unbalanced_fc"]
        assert violations[0].details["gap"] == "20000"

    def test_fc_imbalance_is_caught_even_when_converted_numbers_agree(self) -> None:
        """Lưới thứ hai của phase-04: tỷ giá lệch giữa hai dòng USD."""
        usd_debit = _line(debit=Decimal(100), currency="USD", rate=Decimal(25_000))
        usd_credit = _line(credit=Decimal(125), currency="USD", rate=Decimal(20_000))
        lines = [_prepared(usd_debit), _prepared(usd_credit, line_no=2)]
        violations = check_balanced(lines)
        assert [v.code for v in violations] == ["posting.unbalanced_fc"]
        assert violations[0].details["currency"] == "USD"

    def test_a_balanced_multicurrency_voucher_passes(self) -> None:
        lines = [
            _prepared(_line(debit=Decimal(100), currency="USD", rate=Decimal(25_000))),
            _prepared(_line(credit=Decimal(100), currency="USD", rate=Decimal(25_000)), line_no=2),
            _prepared(_line(debit=Decimal(50_000)), line_no=3),
            _prepared(_line(credit=Decimal(50_000)), line_no=4),
        ]
        assert check_balanced(lines) == []


def _account(
    *,
    account_id: int,
    package_id: int = 1,
    code: str = "111",
    is_summary: bool = False,
    is_inactive: bool = False,
) -> ChartOfAccount:
    account = ChartOfAccount(
        package_id=package_id,
        code=code,
        name=f"TK {code}",
        path="1.",
        balance_nature=BalanceNature.DEBIT,
        is_summary=is_summary,
        is_inactive=is_inactive,
    )
    account.id = account_id
    return account


class TestAccounts:
    def test_missing_wrong_package_inactive_and_summary_are_all_reported(self) -> None:
        accounts = {
            1: _account(account_id=1),
            2: _account(account_id=2, code="11", is_summary=True),
            3: _account(account_id=3, code="112", is_inactive=True),
            4: _account(account_id=4, code="331", package_id=2),
        }
        lines = [
            _prepared(_line(account_id=99, debit=Decimal(1))),
            _prepared(_line(account_id=2, debit=Decimal(1)), line_no=2),
            _prepared(_line(account_id=3, debit=Decimal(1)), line_no=3),
            _prepared(_line(account_id=4, credit=Decimal(3)), line_no=4),
        ]
        codes = [v.code for v in check_accounts(lines, accounts=accounts, active_package_id=1)]
        assert codes == [
            "account.not_found",
            "account.summary",
            "account.inactive",
            "account.wrong_package",
        ]

    def test_a_clean_line_produces_no_violation(self) -> None:
        accounts = {1: _account(account_id=1)}
        lines = [_prepared(_line(account_id=1, debit=Decimal(1)))]
        assert check_accounts(lines, accounts=accounts, active_package_id=1) == []


def test_prepared_line_keeps_source_reference() -> None:
    """`source_line_id` đi xuyên suốt để đối chiếu dòng sổ ↔ dòng chi tiết."""
    source_id = uuid4()
    line = PostingLine(
        account_id=1,
        debit_fc=Decimal(1),
        currency=VND,
        rate=ONE,
        source_line_id=source_id,
    )
    assert _prepared(line).source.source_line_id == source_id
