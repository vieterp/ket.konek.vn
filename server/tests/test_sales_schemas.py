"""Luật hình dạng của payload hóa đơn bán (lát 7C-2) — không cần DB.

Những luật ở đây chặn payload **trước** khi service chạm tới bất cứ thứ gì, nên
chúng phải đứng riêng khỏi `test_sales_invoice_flow.py`: một luật biên bị gỡ mà
chỉ có bài đi qua DB canh thì nó chỉ đỏ ở một môi trường có PostgreSQL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ket.kernel.protocols import SettlementTargetKind
from ket.modules.sales.models import REVERSING_KINDS, SalesInvoiceKind
from ket.modules.sales.schemas import (
    SalesInvoiceIn,
    SalesInvoiceLineIn,
    SalesInvoiceUpdate,
    SalesSettlementIn,
)

JAN_15 = date(2026, 1, 15)

REVENUE_ACCOUNT = 501
VAT_ACCOUNT = 502
RECEIVABLE_ACCOUNT = 503


def _line(**overrides: object) -> SalesInvoiceLineIn:
    fields: dict[str, object] = {
        "description": "Hàng A",
        "quantity": Decimal(2),
        "unit_price_fc": Decimal(100),
        "amount_fc": Decimal(200),
        "account_id": REVENUE_ACCOUNT,
    }
    fields.update(overrides)
    return SalesInvoiceLineIn(**fields)  # type: ignore[arg-type]


def _settlement(amount: Decimal = Decimal(200)) -> SalesSettlementIn:
    return SalesSettlementIn(
        target_kind=SettlementTargetKind.SALES_INVOICE, target_id=uuid4(), amount_fc=amount
    )


def _invoice(**overrides: object) -> SalesInvoiceIn:
    fields: dict[str, object] = {
        "kind": SalesInvoiceKind.GOODS,
        "operation_code": "ban-hang-hoa",
        "customer_id": 1,
        "receivable_account_id": RECEIVABLE_ACCOUNT,
        "branch_id": 1,
        "document_date": JAN_15,
        "posting_date": JAN_15,
        "currency_code": "VND",
        "lines": (_line(),),
    }
    fields.update(overrides)
    return SalesInvoiceIn(**fields)  # type: ignore[arg-type]


def test_vat_line_needs_a_tax_account() -> None:
    with pytest.raises(ValidationError, match="tài khoản thuế"):
        _line(vat_amount_fc=Decimal(20))
    # Có TK thuế thì hợp lệ.
    assert _line(vat_amount_fc=Decimal(20), vat_account_id=VAT_ACCOUNT).vat_amount_fc == Decimal(20)


def test_stock_issue_line_needs_an_item() -> None:
    with pytest.raises(ValidationError, match="vật tư/hàng hóa"):
        _line(warehouse_id=7)
    assert _line(warehouse_id=7, item_id=3).warehouse_id == 7


def test_discount_percent_stays_within_a_hundred() -> None:
    with pytest.raises(ValidationError):
        _line(discount_percent=Decimal(101))
    with pytest.raises(ValidationError):
        _line(discount_percent=Decimal(-1))
    assert _line(discount_percent=Decimal(100)).discount_percent == Decimal(100)


@pytest.mark.parametrize("kind", REVERSING_KINDS)
def test_reversing_kinds_require_a_settlement_target(kind: int) -> None:
    """Sổ phụ không có dòng âm, nên chứng từ giảm trừ không đối trừ vào đâu là
    một chứng từ không có chỗ để ghi (quyết định user 2026-09-04)."""
    with pytest.raises(ValidationError, match="đối trừ vào hóa đơn gốc"):
        _invoice(kind=kind, operation_code="tra-lai-hang-ban")
    assert (
        len(
            _invoice(
                kind=kind, operation_code="tra-lai-hang-ban", settlements=(_settlement(),)
            ).settlements
        )
        == 1
    )


@pytest.mark.parametrize(
    "kind", [SalesInvoiceKind.GOODS, SalesInvoiceKind.SERVICE, SalesInvoiceKind.AGENCY]
)
def test_normal_kinds_cannot_carry_settlements(kind: int) -> None:
    with pytest.raises(ValidationError, match="mới đối trừ hóa đơn gốc"):
        _invoice(kind=kind, settlements=(_settlement(),))


def test_one_settlement_row_per_target_invoice() -> None:
    target = uuid4()
    duplicated = tuple(
        SalesSettlementIn(
            target_kind=SettlementTargetKind.SALES_INVOICE,
            target_id=target,
            amount_fc=Decimal(100),
        )
        for _ in range(2)
    )
    with pytest.raises(ValidationError, match="một dòng trên mỗi hóa đơn"):
        _invoice(
            kind=SalesInvoiceKind.RETURN,
            operation_code="tra-lai-hang-ban",
            settlements=duplicated,
        )


def test_exchange_rate_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="Tỷ giá phải dương"):
        _invoice(exchange_rate=Decimal(0))


def test_invoice_needs_at_least_one_line() -> None:
    with pytest.raises(ValidationError):
        _invoice(lines=())


def test_unknown_field_is_refused() -> None:
    """`extra="forbid"` ở mọi ranh giới: một trường gõ sai tên phải đỏ ngay,
    không âm thầm bị bỏ qua rồi để người dùng tưởng đã khai."""
    with pytest.raises(ValidationError):
        _invoice(price_policy_id=3)


def test_update_carries_a_row_version() -> None:
    with pytest.raises(ValidationError):
        SalesInvoiceUpdate(**_invoice().model_dump())  # type: ignore[arg-type]
    updated = SalesInvoiceUpdate(**{**_invoice().model_dump(), "row_version": 2})  # type: ignore[arg-type]
    assert updated.row_version == 2
