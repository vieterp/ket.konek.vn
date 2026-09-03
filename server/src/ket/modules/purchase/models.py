"""Bảng của phân hệ Mua hàng (SRS 05) — hóa đơn mua + chi phí mua hàng.

Cùng khuôn `cash_book`: header là `vouchers` (posting làm chủ), module giữ
**thân** một-một (`purchase_invoices`), dòng hàng/dịch vụ, dòng chi phí mua
hàng (landed cost) và dòng đối trừ cho hóa đơn trả lại. Không bảng nào ở đây
mang `branch_id`: phạm vi chi nhánh là của header, và RLS canh ở đó.

Bốn quyết định đáng ghi:

* **`kind` gom năm nghiệp vụ mua vào một loại chứng từ `PUR`** (mua nhập kho,
  mua dịch vụ, mua tài sản, hàng mua đang đi đường, trả lại hàng mua). Chúng
  khác nhau ở TK bên Nợ và ở chiều bút toán (trả lại đảo chiều), không khác
  nhau ở hình dạng dữ liệu — một bảng, một dãy số, một màn hình.
* **Chi phí mua hàng lồng trong hóa đơn** (`landed_costs`), không phải chứng
  từ riêng: SRS 05 §3.2 ghi "Nợ 152/156 (phân bổ) / Có 331" ngay trên hóa đơn
  mua, và phân bổ chỉ có nghĩa khi biết các dòng hàng nó rải lên. Mỗi dòng chi
  phí mang TK Có riêng (331 của NCC vận chuyển, 3333 thuế nhập khẩu, 33312
  thuế GTGT hàng nhập khẩu…) và NCC riêng — chi phí mua hàng hiếm khi do chính
  NCC bán hàng thu.
* **`payable_account_id` lưu thật trên thân**, không tra lại gói cấu hình lúc
  ghi sổ: cùng lý do `ar_ap_ledger.account_id` — gói đổi mặc định thì chứng từ
  cũ vẫn phải ghi đúng TK nó đã cất.
* **Trả lại hàng mua (`kind = RETURN`) đối trừ hóa đơn gốc** qua
  `purchase_settlements`, cùng khuôn `cash_settlements`: khoản giảm nợ ghi
  thẳng vào số đã trả của hóa đơn gốc thay vì treo một dòng công nợ âm.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.config.auto_posting_models import OPERATION_CODE_MAX_LENGTH
from ket.kernel.identifiers import uuid7
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

PURCHASE_DOCUMENT_TYPE = "PUR"
"""Mã loại chứng từ trong `vouchers.document_type`, dãy số và gói tự động định khoản."""

DESCRIPTION_MAX_LENGTH = 500
VENDOR_INVOICE_FORM_MAX_LENGTH = 20
VENDOR_INVOICE_SERIAL_MAX_LENGTH = 20
VENDOR_INVOICE_NO_MAX_LENGTH = 50

UNIT_PRICE_PRECISION = 24
UNIT_PRICE_SCALE = 6
"""Đơn giá nguyên tệ giữ 6 số lẻ: hàng nhập khẩu báo giá 0.0125 USD/cái là
chuyện thường, còn thành tiền thì làm tròn về `AMOUNT_SCALE` như mọi số tiền."""

VAT_RATE_PRECISION = 5
VAT_RATE_SCALE = 2


class PurchaseInvoiceKind:
    """`purchase_invoices.kind` — hằng số nguyên, cùng lối `CashVoucherKind`."""

    GOODS = 0
    """Mua hàng nhập kho (152/153/156/611)."""
    SERVICE = 1
    """Mua dịch vụ, chi phí không qua kho (627/641/642/242)."""
    ASSET = 2
    """Mua tài sản cố định (211/241)."""
    IN_TRANSIT = 3
    """Hàng mua đang đi đường (151)."""
    RETURN = 4
    """Trả lại hàng mua — đảo chiều bút toán, đối trừ hóa đơn gốc."""


class VendorInvoiceStatus:
    """`purchase_invoices.vendor_invoice_status` (FR-PUR-006)."""

    RECEIVED = 0
    """Đã nhận hóa đơn — được khấu trừ thuế GTGT đầu vào (BR-PUR-02)."""
    NOT_YET = 1
    """Hàng về trước, hóa đơn về sau."""
    NONE = 2
    """Không có hóa đơn (mua của cá nhân, bảng kê 01/TNDN)."""


class LandedCostAllocation:
    """`purchase_invoices.landed_cost_allocation` — cơ sở phân bổ chi phí mua
    hàng lên các dòng hàng (SRS 05 §3.2 "phân bổ")."""

    BY_VALUE = 0
    """Theo giá trị dòng (`amount_fc`)."""
    BY_QUANTITY = 1
    """Theo số lượng — mọi dòng phải có `quantity`."""
    MANUAL = 2
    """Người dùng nhập `landed_cost_fc` từng dòng, tổng phải khớp."""


class PurchaseInvoice(DatasetBase, Audited):
    """Thân hóa đơn mua — một-một với header `vouchers`.

    `Audited` nhưng không `RowVersioned`: tranh chấp sửa giải ở header, cùng
    lập luận `cash_vouchers`.
    """

    __tablename__ = "purchase_invoices"
    __table_args__ = (
        CheckConstraint(
            f"kind BETWEEN {PurchaseInvoiceKind.GOODS} AND {PurchaseInvoiceKind.RETURN}",
            name="kind_known",
        ),
        CheckConstraint("operation_code <> ''", name="operation_code_not_blank"),
        CheckConstraint(
            f"vendor_invoice_status BETWEEN {VendorInvoiceStatus.RECEIVED} "
            f"AND {VendorInvoiceStatus.NONE}",
            name="vendor_invoice_status_known",
        ),
        CheckConstraint(
            f"landed_cost_allocation BETWEEN {LandedCostAllocation.BY_VALUE} "
            f"AND {LandedCostAllocation.MANUAL}",
            name="landed_cost_allocation_known",
        ),
        CheckConstraint(
            "total_before_tax_fc >= 0 AND total_vat_fc >= 0 "
            "AND total_landed_cost_fc >= 0 AND total_fc >= 0",
            name="totals_not_negative",
        ),
        Index("ix_purchase_invoices_vendor", "vendor_id"),
    )

    id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    operation_code: Mapped[str] = mapped_column(String(OPERATION_CODE_MAX_LENGTH), nullable=False)
    """Nghiệp vụ đã chọn (FR-SYS-025) — quyết định TK gợi ý cho dòng hàng và
    TK công nợ; sau khi cất, TK thật nằm trên dòng, không tra lại."""

    vendor_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """Nhà cung cấp — luôn `PartnerKind.VENDOR`, nên không có cột `partner_kind`
    như phiếu quỹ (phiếu quỹ nhận cả ba loại đối tác, hóa đơn mua thì không)."""

    vendor_invoice_status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=VendorInvoiceStatus.RECEIVED, server_default="0"
    )
    vendor_invoice_form: Mapped[str | None] = mapped_column(
        String(VENDOR_INVOICE_FORM_MAX_LENGTH), nullable=True
    )
    vendor_invoice_serial: Mapped[str | None] = mapped_column(
        String(VENDOR_INVOICE_SERIAL_MAX_LENGTH), nullable=True
    )
    vendor_invoice_no: Mapped[str | None] = mapped_column(
        String(VENDOR_INVOICE_NO_MAX_LENGTH), nullable=True
    )
    vendor_invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Bốn trường hóa đơn GTGT của NCC (mẫu số, ký hiệu, số, ngày) — nguồn của
    bảng kê mua vào (7G)."""

    payment_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_terms.id", ondelete="RESTRICT"), nullable=True
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Hạn thanh toán — client gửi, hoặc service tính `document_date + due_days`
    của điều khoản (FR-PUR-034); đi vào `ar_ap_ledger.due_date`."""

    payable_account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    """TK công nợ phải trả của hóa đơn (331/3388…) — bên Có của mọi dòng hàng."""

    landed_cost_allocation: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=LandedCostAllocation.BY_VALUE, server_default="0"
    )

    total_before_tax_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    total_vat_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    total_landed_cost_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    total_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    """Ô "Tổng cộng" của chứng từ = hàng + thuế GTGT (của dòng **và** của khoản
    chi phí) + chi phí mua hàng — cùng số mà đối trừ của chứng từ trả lại phải
    khớp (`settlement_service.invoice_total_fc`). Đây KHÔNG phải số phải trả
    NCC bán hàng: phần chi phí nợ NCC khác (hoặc ngân sách, hoặc đã chi thẳng),
    và số phải trả từng NCC nằm ở dòng `ar_ap_ledger`. Service tính lại từ
    dòng mỗi lần cất; lưu để màn danh sách không phải cộng dòng."""


class PurchaseInvoiceLine(DatasetBase, Audited):
    """Một dòng hàng/dịch vụ: TK bên Nợ + số tiền + thuế GTGT + chiều phân tích."""

    __tablename__ = "purchase_invoice_lines"
    __table_args__ = (
        CheckConstraint("amount_fc > 0", name="amount_positive"),
        CheckConstraint("vat_amount_fc >= 0", name="vat_not_negative"),
        CheckConstraint("landed_cost_fc >= 0", name="landed_cost_not_negative"),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "vat_amount_fc = 0 OR vat_account_id IS NOT NULL", name="vat_account_required"
        ),
        Index("ix_purchase_invoice_lines_voucher", "voucher_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_invoices.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)

    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warehouse_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Vật tư / đơn vị / kho — rỗng được với dòng dịch vụ. Phase 8 (kho) đọc ba
    cột này để lập phiếu nhập; ở đây chúng chỉ là chiều phân tích."""

    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=True
    )
    unit_price_fc: Mapped[Decimal | None] = mapped_column(
        Numeric(UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE), nullable=True
    )
    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    """Thành tiền trước thuế — nhập tay được (chiết khấu, làm tròn theo hóa đơn
    NCC), service không ép bằng `quantity × unit_price_fc`."""

    vat_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(VAT_RATE_PRECISION, VAT_RATE_SCALE), nullable=True
    )
    vat_amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    landed_cost_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Phần chi phí mua hàng phân bổ vào dòng — service tính (theo giá trị / số
    lượng) hoặc nhận từ người dùng (`MANUAL`), luôn lưu để bút toán và giá nhập
    kho đọc cùng một con số."""

    account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    """TK hàng/chi phí bên Nợ (152/156/611/627/642/211…)."""
    vat_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    """TK thuế GTGT đầu vào (1331/1332) — bắt buộc khi dòng có thuế."""

    cost_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expense_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extended_dimensions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    """Đủ bộ chiều như dòng phiếu quỹ, trừ `bank_account_id` (hóa đơn mua không
    chạm 112x) — TK chi phí có thể đòi bất kỳ chiều nào theo `detail_tracking`."""


class LandedCost(DatasetBase, Audited):
    """Một dòng chi phí mua hàng: vận chuyển, bốc xếp, thuế nhập khẩu, thuế
    TTĐB, thuế GTGT hàng nhập khẩu… — phân bổ lên các dòng hàng của hóa đơn.

    `amount_fc` là phần **nhập vào giá vốn** (Nợ TK hàng / Có `credit_account`),
    `vat_amount_fc` là phần thuế GTGT được khấu trừ đi kèm (Nợ `vat_account` /
    Có `credit_account`). Dòng thuế GTGT hàng nhập khẩu vì thế là `amount_fc = 0`
    + `vat_amount_fc > 0` với TK Có 33312 — nên CHECK chỉ đòi một trong hai
    dương, không đòi `amount_fc > 0`.
    """

    __tablename__ = "landed_costs"
    __table_args__ = (
        CheckConstraint("amount_fc >= 0", name="amount_not_negative"),
        CheckConstraint("vat_amount_fc >= 0", name="vat_not_negative"),
        CheckConstraint("amount_fc > 0 OR vat_amount_fc > 0", name="has_amount"),
        CheckConstraint(
            "vat_amount_fc = 0 OR vat_account_id IS NOT NULL", name="vat_account_required"
        ),
        Index("ix_landed_costs_voucher", "voucher_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_invoices.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=False)

    vendor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """NCC của khoản chi phí (đơn vị vận chuyển…). Rỗng khi TK Có không theo
    dõi NCC (3333/3332/33312 — nợ ngân sách)."""

    credit_account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    vat_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(VAT_RATE_PRECISION, VAT_RATE_SCALE), nullable=True
    )
    vat_amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    vat_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )


class PurchaseSettlement(DatasetBase, Audited):
    """Một dòng đối trừ của hóa đơn TRẢ LẠI vào hóa đơn mua gốc — cùng khuôn
    `cash_settlements` (`docs/srs/03` §4, FR-SYS-066)."""

    __tablename__ = "purchase_settlements"
    __table_args__ = (
        CheckConstraint(
            f"target_kind BETWEEN {SettlementTargetKind.SALES_INVOICE} "
            f"AND {SettlementTargetKind.OPENING_BALANCE}",
            name="target_kind_known",
        ),
        CheckConstraint("amount_fc > 0", name="amount_fc_positive"),
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint(
            "voucher_id", "target_kind", "target_id", name="uq_purchase_settlements_voucher_target"
        ),
        Index("ix_purchase_settlements_target", "target_kind", "target_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_invoices.id", ondelete="CASCADE"), nullable=False
    )
    target_kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)
    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False)
    fx_diff: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Chênh lệch tỷ giá giữa tỷ giá hóa đơn trả lại và tỷ giá ghi nhận nợ —
    `posting_mapper` sinh cặp dòng 515/635 từ cột này."""
