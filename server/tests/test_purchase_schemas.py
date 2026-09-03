"""Kiểm các validator Pydantic của hóa đơn mua (SRS 05, lát 7B) — thuần, không cần DB.

Bao gồm:
* Validator `PurchaseInvoiceLineIn._line_sane()` — VAT và kho hàng.
* Validator `LandedCostIn._cost_sane()` — yêu cầu ít nhất một trong hai: tiền hay thuế.
* Validator `PurchaseInvoiceIn._invoice_sane()` — chiều tỷ giá, trả lại hàng mua và đối trừ.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ket.kernel.protocols import SettlementTargetKind
from ket.modules.purchase.models import (
    PurchaseInvoiceKind,
    VendorInvoiceStatus,
)
from ket.modules.purchase.schemas import (
    LandedCostIn,
    PurchaseInvoiceIn,
    PurchaseInvoiceLineIn,
    PurchaseSettlementIn,
)

_ZERO = Decimal(0)
_TODAY = date(2026, 1, 15)


class TestPurchaseInvoiceLineValidator:
    """Kiểm `PurchaseInvoiceLineIn._line_sane()` — thiết lập VAT và kho hàng."""

    def test_valid_line_with_amount_only(self) -> None:
        """Dòng hợp lệ: chỉ có tiền, không có thuế, không chọn kho."""
        line = PurchaseInvoiceLineIn(
            description="Dịch vụ tư vấn",
            amount_fc=Decimal("1000000"),
            account_id=1,
        )
        assert line.amount_fc == Decimal("1000000")
        assert line.vat_amount_fc == _ZERO

    def test_valid_line_with_vat_and_account(self) -> None:
        """Dòng hợp lệ: có thuế GTGT và tài khoản thuế."""
        line = PurchaseInvoiceLineIn(
            description="Hàng hóa",
            amount_fc=Decimal("900000"),
            vat_amount_fc=Decimal("180000"),
            vat_account_id=100,
            account_id=1,
        )
        assert line.vat_amount_fc == Decimal("180000")
        assert line.vat_account_id == 100

    def test_vat_without_account_raises(self) -> None:
        """Dòng có tiền thuế GTGT nhưng không chỉ định tài khoản thuế → lỗi."""
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceLineIn(
                description="Hàng",
                amount_fc=Decimal("1000000"),
                vat_amount_fc=Decimal("200000"),
                account_id=1,
                # vat_account_id=None,  # Missing!
            )
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "tài khoản thuế" in error["msg"]

    def test_warehouse_without_item_raises(self) -> None:
        """Dòng nhập kho phải chỉ định vật tư — nếu không → lỗi."""
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceLineIn(
                description="Hàng",
                amount_fc=Decimal("1000000"),
                warehouse_id=10,
                # item_id=None,  # Missing!
                account_id=1,
            )
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "nhập kho" in error["msg"]

    def test_warehouse_with_item_valid(self) -> None:
        """Dòng nhập kho với cả vật tư và kho → hợp lệ."""
        line = PurchaseInvoiceLineIn(
            description="Hàng",
            amount_fc=Decimal("1000000"),
            warehouse_id=10,
            item_id=50,
            account_id=1,
        )
        assert line.warehouse_id == 10
        assert line.item_id == 50

    def test_quantity_and_unit_price_optional(self) -> None:
        """Số lượng và đơn giá không bắt buộc — để tính giá vốn, không bắt buộc."""
        line = PurchaseInvoiceLineIn(
            description="Dịch vụ",
            amount_fc=Decimal("1000000"),
            account_id=1,
            # quantity=None,
            # unit_price_fc=None,
        )
        assert line.quantity is None
        assert line.unit_price_fc is None

    def test_landed_cost_fc_ignored_unless_manual(self) -> None:
        """Trường `landed_cost_fc` được nhận nhưng server sẽ ghi đè ngoài chế độ thủ công."""
        line = PurchaseInvoiceLineIn(
            description="Hàng",
            amount_fc=Decimal("1000000"),
            landed_cost_fc=Decimal("50000"),  # Được nhận.
            account_id=1,
        )
        assert line.landed_cost_fc == Decimal("50000")


class TestLandedCostValidator:
    """Kiểm `LandedCostIn._cost_sane()` — ít nhất một trong: tiền hay thuế."""

    def test_valid_cost_with_amount_only(self) -> None:
        """Khoản chi phí hợp lệ: chỉ có tiền, không thuế."""
        cost = LandedCostIn(
            description="Vận chuyển",
            credit_account_id=331,
            amount_fc=Decimal("100000"),
        )
        assert cost.amount_fc == Decimal("100000")

    def test_valid_cost_with_vat_only(self) -> None:
        """Khoản chi phí hợp lệ: chỉ có thuế, không có tiền gốc."""
        cost = LandedCostIn(
            description="Thuế nhập khẩu",
            credit_account_id=3333,
            vat_amount_fc=Decimal("50000"),
            vat_account_id=100,
        )
        assert cost.vat_amount_fc == Decimal("50000")

    def test_valid_cost_with_both_amount_and_vat(self) -> None:
        """Khoản chi phí hợp lệ: có cả tiền lẫn thuế."""
        cost = LandedCostIn(
            description="Vận chuyển (có VAT)",
            credit_account_id=331,
            amount_fc=Decimal("100000"),
            vat_amount_fc=Decimal("20000"),
            vat_account_id=100,
        )
        assert cost.amount_fc == Decimal("100000")
        assert cost.vat_amount_fc == Decimal("20000")

    def test_both_zero_raises(self) -> None:
        """Cả tiền lẫn thuế đều 0 → lỗi (khoản chi phí phải khác 0)."""
        with pytest.raises(ValidationError) as exc_info:
            LandedCostIn(
                description="Chi phí",
                credit_account_id=331,
                amount_fc=_ZERO,
                vat_amount_fc=_ZERO,
            )
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "số tiền hoặc thuế" in error["msg"]

    def test_vat_without_account_raises(self) -> None:
        """Khoản có thuế nhưng không chỉ định tài khoản thuế → lỗi."""
        with pytest.raises(ValidationError) as exc_info:
            LandedCostIn(
                description="Chi phí",
                credit_account_id=331,
                amount_fc=_ZERO,
                vat_amount_fc=Decimal("50000"),
                # vat_account_id=None,
            )
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "tài khoản thuế" in error["msg"]


class TestPurchaseInvoiceValidator:
    """Kiểm `PurchaseInvoiceIn._invoice_sane()` — tỷ giá, loại, đối trừ."""

    def _make_basic_goods_invoice(self) -> PurchaseInvoiceIn:
        """Xây hóa đơn mua hàng cơ bản để làm nền."""
        return PurchaseInvoiceIn(
            kind=PurchaseInvoiceKind.GOODS,
            operation_code="mua-hang-hoa",
            vendor_id=1,
            payable_account_id=331,
            branch_id=1,
            document_date=_TODAY,
            posting_date=_TODAY,
            currency_code="VND",
            exchange_rate=Decimal(1),
            vendor_invoice_status=VendorInvoiceStatus.RECEIVED,
            lines=(
                PurchaseInvoiceLineIn(
                    description="Hàng",
                    amount_fc=Decimal("1000000"),
                    account_id=156,
                ),
            ),
        )

    def test_valid_goods_invoice(self) -> None:
        """Hóa đơn mua hàng hợp lệ với các trường bắt buộc."""
        invoice = self._make_basic_goods_invoice()
        assert invoice.kind == PurchaseInvoiceKind.GOODS
        assert len(invoice.lines) == 1

    def test_exchange_rate_zero_raises(self) -> None:
        """Tỷ giá = 0 → lỗi (tỷ giá phải dương)."""
        invoice = self._make_basic_goods_invoice()
        invoice.exchange_rate = _ZERO
        with pytest.raises(ValidationError) as exc_info:
            # Re-validate by recreating
            PurchaseInvoiceIn(**invoice.model_dump())
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "dương" in error["msg"].lower()

    def test_exchange_rate_negative_raises(self) -> None:
        """Tỷ giá âm → lỗi (tỷ giá phải dương)."""
        invoice = self._make_basic_goods_invoice()
        invoice.exchange_rate = Decimal(-1)
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceIn(**invoice.model_dump())
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "dương" in error["msg"].lower()

    def test_return_with_landed_costs_raises(self) -> None:
        """Chứng từ trả lại hàng không được có chi phí mua hàng."""
        invoice = self._make_basic_goods_invoice()
        invoice.kind = PurchaseInvoiceKind.RETURN
        invoice.landed_costs = (
            LandedCostIn(
                description="Vận chuyển",
                credit_account_id=331,
                amount_fc=Decimal("50000"),
            ),
        )
        invoice.settlements = (
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=uuid4(),
                amount_fc=Decimal("1000000"),
            ),
        )
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceIn(**invoice.model_dump())
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "trả lại" in error["msg"] and "chi phí" in error["msg"]

    def test_return_without_settlements_raises(self) -> None:
        """Chứng từ trả lại hàng phải đối trừ vào hóa đơn gốc."""
        invoice = self._make_basic_goods_invoice()
        invoice.kind = PurchaseInvoiceKind.RETURN
        invoice.settlements = ()  # No settlements!
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceIn(**invoice.model_dump())
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "trả lại" in error["msg"] and "đối trừ" in error["msg"]

    def test_return_with_valid_settlements(self) -> None:
        """Chứng từ trả lại hàng hợp lệ khi có đối trừ."""
        original_id = uuid4()
        invoice = self._make_basic_goods_invoice()
        invoice.kind = PurchaseInvoiceKind.RETURN
        invoice.settlements = (
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=original_id,
                amount_fc=Decimal("1000000"),
            ),
        )
        # Should not raise
        result = PurchaseInvoiceIn(**invoice.model_dump())
        assert result.kind == PurchaseInvoiceKind.RETURN
        assert len(result.settlements) == 1

    def test_goods_with_settlements_raises(self) -> None:
        """Chỉ chứng từ trả lại hàng mới được có đối trừ."""
        invoice = self._make_basic_goods_invoice()
        invoice.kind = PurchaseInvoiceKind.GOODS
        invoice.settlements = (
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=uuid4(),
                amount_fc=Decimal("500000"),
            ),
        )
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceIn(**invoice.model_dump())
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "trả lại" in error["msg"]

    def test_duplicate_settlement_targets_raises(self) -> None:
        """Một hóa đơn chỉ được đối trừ một lần trên mỗi chứng từ gốc."""
        original_id = uuid4()
        invoice = self._make_basic_goods_invoice()
        invoice.kind = PurchaseInvoiceKind.RETURN
        invoice.settlements = (
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=original_id,
                amount_fc=Decimal("500000"),
            ),
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=original_id,  # Duplicate!
                amount_fc=Decimal("500000"),
            ),
        )
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceIn(**invoice.model_dump())
        error = exc_info.value.errors()[0]
        assert error["type"] == "value_error"
        assert "một" in error["msg"]

    def test_multiple_settlement_targets_allowed(self) -> None:
        """Được đối trừ vào nhiều hóa đơn gốc khác nhau."""
        invoice = self._make_basic_goods_invoice()
        invoice.kind = PurchaseInvoiceKind.RETURN
        invoice.settlements = (
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=uuid4(),
                amount_fc=Decimal("500000"),
            ),
            PurchaseSettlementIn(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                target_id=uuid4(),  # Different ID
                amount_fc=Decimal("500000"),
            ),
        )
        # Should not raise
        result = PurchaseInvoiceIn(**invoice.model_dump())
        assert len(result.settlements) == 2

    def test_extra_fields_forbidden(self) -> None:
        """Không được gửi trường ngoài schema."""
        data = self._make_basic_goods_invoice().model_dump()
        data["unknown_field"] = "should fail"
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceIn(**data)
        error = exc_info.value.errors()[0]
        assert error["type"] == "extra_forbidden"

    def test_line_extra_fields_forbidden(self) -> None:
        """Dòng hàng không được có trường ngoài schema."""
        line_data = PurchaseInvoiceLineIn(
            description="Hàng",
            amount_fc=Decimal("1000000"),
            account_id=1,
        ).model_dump()
        line_data["unknown"] = "forbidden"
        with pytest.raises(ValidationError) as exc_info:
            PurchaseInvoiceLineIn(**line_data)
        error = exc_info.value.errors()[0]
        assert error["type"] == "extra_forbidden"

    def test_landed_cost_extra_fields_forbidden(self) -> None:
        """Chi phí mua hàng không được có trường ngoài schema."""
        cost_data = LandedCostIn(
            description="Vận chuyển",
            credit_account_id=331,
            amount_fc=Decimal("100000"),
        ).model_dump()
        cost_data["extra"] = "forbidden"
        with pytest.raises(ValidationError) as exc_info:
            LandedCostIn(**cost_data)
        error = exc_info.value.errors()[0]
        assert error["type"] == "extra_forbidden"
