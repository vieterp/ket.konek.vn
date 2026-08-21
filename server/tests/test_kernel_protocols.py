"""`kernel/protocols.py` — registry Protocol liên-module (RT-18, lát 6A).

Không cần PostgreSQL: registry là cấu trúc trong tiến trình; bản cài giả ở đây
chỉ để kiểm luật đăng ký. Hành vi của từng provider thật kiểm ở lát cài nó
(6B: nguồn số dư đầu kỳ; phase 7/8: hóa đơn thật, phiếu kho).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.protocols import (
    CrossModuleProviders,
    InventoryMovementKind,
    InventoryMovementLine,
    OpenInvoice,
    SettlementTargetKind,
)


class _StubReceivables:
    """Bản cài tối thiểu thỏa `ReceivableProvider` — trả một hóa đơn cố định."""

    def __init__(self, invoice: OpenInvoice) -> None:
        self._invoice = invoice

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        return (self._invoice,)


class _StubInventory:
    """Bản cài tối thiểu thỏa `InventoryPosting`."""

    def create_movement(
        self,
        session: Session,
        *,
        kind: InventoryMovementKind,
        source_voucher_id: UUID,
        branch_id: int,
        posting_date: date,
        currency_code: str,
        exchange_rate: Decimal,
        lines: Sequence[InventoryMovementLine],
        user_id: int,
    ) -> UUID:
        return source_voucher_id


class _StubCommitments:
    def committed_quantities(
        self,
        session: Session,
        *,
        item_ids: Sequence[int],
        branch_id: int,
        warehouse_id: int | None = None,
    ) -> Mapping[int, Decimal]:
        return {}


def _an_invoice() -> OpenInvoice:
    return OpenInvoice(
        target_kind=SettlementTargetKind.OPENING_BALANCE,
        target_id=uuid4(),
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=7,
        branch_id=1,
        account_id=131,
        invoice_no="HD-001",
        invoice_date=date(2026, 1, 15),
        currency_code="VND",
        exchange_rate=Decimal(1),
        amount_fc=Decimal("1000"),
        remaining_fc=Decimal("400"),
        remaining=Decimal("400"),
    )


def test_a_fresh_registry_is_empty_not_fake() -> None:
    """Chưa module nào cài = danh sách rỗng và `inventory_posting()` là `None` —
    trạng thái thật của một bản cài trước phase 7/8, không phải no-op trả dữ
    liệu bịa (docstring `kernel/protocols.py`)."""
    providers = CrossModuleProviders()
    assert providers.receivable_providers() == ()
    assert providers.payable_providers() == ()
    assert providers.commitment_providers() == ()
    assert providers.inventory_posting() is None


def test_receivable_providers_accumulate_in_registration_order() -> None:
    providers = CrossModuleProviders()
    first = _StubReceivables(_an_invoice())
    second = _StubReceivables(_an_invoice())
    providers.register_receivable(first)
    providers.register_receivable(second)
    assert providers.receivable_providers() == (first, second)


def test_inventory_posting_refuses_a_second_implementation() -> None:
    """Chỉ module kho được ghi sổ kho — bản cài thứ hai là hai nơi tranh nhau
    một bảng tồn, ném ngay lúc import chứ không thắng-thua theo thứ tự."""
    providers = CrossModuleProviders()
    providers.register_inventory_posting(_StubInventory())
    with pytest.raises(ValueError, match="đã có bản cài"):
        providers.register_inventory_posting(_StubInventory())


def test_commitment_provider_registration_round_trips() -> None:
    providers = CrossModuleProviders()
    stub = _StubCommitments()
    providers.register_commitment(stub)
    assert providers.commitment_providers() == (stub,)


def test_settlement_source_is_one_per_target_kind() -> None:
    """Mỗi loại đích đối trừ đúng một chủ — bản cài thứ hai là hai nơi tranh
    nhau một cột `paid`, ném ngay lúc đăng ký (lát 6B)."""

    class _StubSource:
        def find(self, session: Session, *, target_ids: Sequence[UUID]) -> Sequence[OpenInvoice]:
            return ()

        def apply(
            self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
        ) -> None:
            return None

        def revert(
            self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
        ) -> None:
            return None

    providers = CrossModuleProviders()
    assert providers.settlement_source(SettlementTargetKind.OPENING_BALANCE) is None
    source = _StubSource()
    providers.register_settlement_source(SettlementTargetKind.OPENING_BALANCE, source)
    assert providers.settlement_source(SettlementTargetKind.OPENING_BALANCE) is source
    with pytest.raises(ValueError, match="đã có source"):
        providers.register_settlement_source(SettlementTargetKind.OPENING_BALANCE, _StubSource())


def test_open_invoice_is_frozen_and_rejects_unknown_fields() -> None:
    """Hợp đồng dữ liệu qua ranh giới module: đóng băng + cấm trường lạ
    (ADR-015 — không `dict[str, Any]` đi qua ranh giới)."""
    invoice = _an_invoice()
    with pytest.raises(ValidationError, match="frozen"):
        invoice.remaining_fc = Decimal(0)  # type: ignore[misc]
    with pytest.raises(ValidationError, match="la"):
        OpenInvoice.model_validate({**invoice.model_dump(), "la": 1})
