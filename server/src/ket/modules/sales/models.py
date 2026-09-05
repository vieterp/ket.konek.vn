"""Bảng của phân hệ Bán hàng (SRS 06) — hóa đơn bán + đối trừ giảm trừ.

Cùng khuôn `purchase` (7B): header là `vouchers` (posting làm chủ), module giữ
**thân** một-một (`sales_invoices`), dòng hàng/dịch vụ và dòng đối trừ cho hóa
đơn trả lại / giảm giá. Không bảng nào ở đây mang `branch_id`: phạm vi chi
nhánh là của header, và RLS canh ở đó.

Năm quyết định đáng ghi:

* **`kind` gom năm nghiệp vụ bán vào một loại chứng từ `SAL`** (bán hàng, bán
  dịch vụ, trả lại hàng bán, giảm giá hàng bán, bán đại lý) — cùng lập luận
  với `PURCHASE_DOCUMENT_TYPE`: chúng khác nhau ở TK bên Có và ở chiều bút
  toán, không khác nhau ở hình dạng dữ liệu.
* **Chiết khấu thương mại ghi giảm doanh thu NGAY TRÊN DÒNG**, không tách một
  bút toán 521 riêng: SRS 06 §3.2 cho cả hai đường ("qua TK 5211 **hoặc** trừ
  trực tiếp trên hóa đơn") và đường trừ trực tiếp là đường hóa đơn GTGT thật
  sự in ra. `amount_fc` vì thế là số **sau** chiết khấu — chính số vào doanh
  thu — còn `discount_amount_fc` đứng cạnh để bảng kê và báo cáo bán hàng
  (7G) đọc được phần đã giảm. Một cột duy nhất mang tiền hàng thì không có
  chỗ nào để hai con số lệch nhau.
* **`receivable_account_id` lưu thật trên thân**, không tra lại gói cấu hình
  lúc ghi sổ — cùng lý do `payable_account_id` của hóa đơn mua và
  `ar_ap_ledger.account_id`: gói đổi mặc định thì chứng từ cũ vẫn phải ghi
  đúng TK nó đã cất.
* **Trả lại hàng bán và giảm giá hàng bán đối trừ hóa đơn gốc** qua
  `sales_settlements`, cùng khuôn `purchase_settlements` (quyết định user
  2026-09-04): khoản giảm nợ ghi thẳng vào số đã trả của hóa đơn gốc thay vì
  treo một dòng công nợ âm — `ar_ap_ledger` không có dòng âm.
* **Ba cột giá vốn để trống ở lát này** (`cogs_account_id`,
  `inventory_account_id`, `unit_cost_fc`) cùng cờ `cogs_posted`: giá xuất kho
  là việc của phase 8, và nó ghi bổ sung vào chính ba cột ấy. Khai sẵn vì
  thêm cột vào một bảng đã có hóa đơn thật thì đắt hơn nhiều — cùng lập luận
  với `ar_ap_ledger.opening_invoice_id` (7A).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from ket.kernel.money import (
    UNIT_PRICE_PRECISION,
    UNIT_PRICE_SCALE,
    VAT_RATE_PRECISION,
    VAT_RATE_SCALE,
)
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

SALES_DOCUMENT_TYPE = "SAL"
"""Mã loại chứng từ trong `vouchers.document_type`, dãy số và gói tự động định khoản."""

DESCRIPTION_MAX_LENGTH = 500
INVOICE_FORM_MAX_LENGTH = 20
INVOICE_SERIAL_MAX_LENGTH = 20
INVOICE_NO_MAX_LENGTH = 50
SHIP_TO_MAX_LENGTH = 500
RECIPIENT_MAX_LENGTH = 200
PRICE_SOURCE_MAX_LENGTH = 20

DISCOUNT_PERCENT_PRECISION = 5
DISCOUNT_PERCENT_SCALE = 2
"""Tỷ lệ chiết khấu thương mại, cùng hình dạng với `item_discount_tiers.
discount_percent` — bậc chiết khấu chép thẳng xuống dòng chứng từ."""


class SalesInvoiceKind:
    """`sales_invoices.kind` — hằng số nguyên, cùng lối `PurchaseInvoiceKind`."""

    GOODS = 0
    """Bán hàng hóa (511/5111 — trong nước hoặc xuất khẩu)."""
    SERVICE = 1
    """Bán dịch vụ (511/5112)."""
    RETURN = 2
    """Hàng bán bị trả lại — đảo chiều bút toán, đối trừ hóa đơn gốc."""
    ALLOWANCE = 3
    """Giảm giá hàng bán — đảo chiều bút toán, đối trừ hóa đơn gốc."""
    AGENCY = 4
    """Bán qua đại lý đúng giá hưởng hoa hồng."""


REVERSING_KINDS = (SalesInvoiceKind.RETURN, SalesInvoiceKind.ALLOWANCE)
"""Hai loại ghi GIẢM doanh thu và giảm nợ hóa đơn gốc.

Chúng đi cùng nhau ở mọi nhánh (đảo chiều bút toán, bắt buộc có dòng đối trừ,
không ghi dòng sổ phụ mới) nên tên gọi chung này tồn tại để không nhánh nào
kiểm một loại mà quên loại kia."""


class SalesInvoice(DatasetBase, Audited):
    """Thân hóa đơn bán — một-một với header `vouchers`.

    `Audited` nhưng không `RowVersioned`: tranh chấp sửa giải ở header, cùng
    lập luận `cash_vouchers` và `purchase_invoices`.
    """

    __tablename__ = "sales_invoices"
    __table_args__ = (
        CheckConstraint(
            f"kind BETWEEN {SalesInvoiceKind.GOODS} AND {SalesInvoiceKind.AGENCY}",
            name="kind_known",
        ),
        CheckConstraint("operation_code <> ''", name="operation_code_not_blank"),
        CheckConstraint(
            "total_before_tax_fc >= 0 AND total_discount_fc >= 0 "
            "AND total_vat_fc >= 0 AND total_fc >= 0",
            name="totals_not_negative",
        ),
        Index("ix_sales_invoices_customer", "customer_id"),
    )

    id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    operation_code: Mapped[str] = mapped_column(String(OPERATION_CODE_MAX_LENGTH), nullable=False)
    """Nghiệp vụ đã chọn (FR-SYS-025) — quyết định TK gợi ý cho dòng doanh thu
    và TK công nợ; sau khi cất, TK thật nằm trên dòng, không tra lại."""

    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """Khách hàng — luôn `PartnerKind.CUSTOMER`, nên không có cột `partner_kind`
    như phiếu quỹ (phiếu quỹ nhận cả ba loại đối tác, hóa đơn bán thì không)."""

    salesperson_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Nhân viên bán hàng (`employees.id`) — SRS 06 §3.1, nguồn của báo cáo
    doanh số theo nhân viên (FR-SAL-008, báo cáo làm ở 7G)."""

    ship_to: Mapped[str | None] = mapped_column(String(SHIP_TO_MAX_LENGTH), nullable=True)
    recipient_name: Mapped[str | None] = mapped_column(String(RECIPIENT_MAX_LENGTH), nullable=True)

    invoice_form: Mapped[str | None] = mapped_column(String(INVOICE_FORM_MAX_LENGTH), nullable=True)
    invoice_serial: Mapped[str | None] = mapped_column(
        String(INVOICE_SERIAL_MAX_LENGTH), nullable=True
    )
    invoice_no: Mapped[str | None] = mapped_column(String(INVOICE_NO_MAX_LENGTH), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Bốn trường hóa đơn GTGT gõ tay (FR-SAL-004 đường "nhập số hóa đơn tay") —
    nguồn của bảng kê bán ra (7G). Đường cấp số tự động và ràng buộc bất biến
    của hóa đơn điện tử là việc của 7D–7F; ở lát này chúng chỉ là dữ liệu."""

    payment_term_id: Mapped[int | None] = mapped_column(
        ForeignKey("payment_terms.id", ondelete="RESTRICT"), nullable=True
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Hạn thanh toán — client gửi, hoặc service tính `document_date + due_days`
    của điều khoản (FR-SAL-009); đi vào `ar_ap_ledger.due_date`."""

    receivable_account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    """TK công nợ phải thu của hóa đơn (131/1388…) — bên Nợ của mọi dòng hàng."""

    price_list_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Bảng giá người lập chứng từ CHỌN TAY (FR-SAL-020). Ép chứ không "ưu
    tiên" — xem `kernel.pricing.quote_price`. Bảng giá thật sự đã trả lời từng
    dòng nằm ở `sales_invoice_lines.price_list_id`, và hai cột ấy khác nhau
    được: bảng chọn tay không có dòng cho mã hàng thì dòng rơi xuống tầng 2."""

    is_stock_issue: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Cờ "Kiêm phiếu xuất kho" (FR-SAL-003). Lát này chỉ **lưu** cờ: phiếu
    xuất kho và giá vốn đi qua `InventoryPosting`, mà bản cài của Protocol ấy
    là việc của phase 8."""

    cogs_posted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Giá vốn đã ghi sổ chưa — luôn `false` ở lát này; phase 8 lật khi tính
    xong giá xuất kho (BR-SAL-01 đòi doanh thu và giá vốn cùng kỳ)."""

    total_before_tax_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    """Tổng doanh thu **sau** chiết khấu — bằng tổng `amount_fc` của các dòng."""
    total_discount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    total_vat_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    total_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    """Ô "Tổng cộng" của chứng từ = doanh thu sau chiết khấu + thuế GTGT đầu
    ra — cũng là số phải thu khách hàng, và là số mà tổng đối trừ của chứng từ
    trả lại/giảm giá phải khớp (`settlement_service.invoice_total_fc`). Service
    tính lại từ dòng mỗi lần cất; lưu để màn danh sách không phải cộng dòng."""


class SalesInvoiceLine(DatasetBase, Audited):
    """Một dòng hàng/dịch vụ: TK doanh thu bên Có + thuế GTGT đầu ra + chiều
    phân tích + ba cột giá vốn phase 8 điền."""

    __tablename__ = "sales_invoice_lines"
    __table_args__ = (
        CheckConstraint("amount_fc > 0", name="amount_positive"),
        CheckConstraint("vat_amount_fc >= 0", name="vat_not_negative"),
        CheckConstraint("discount_amount_fc >= 0", name="discount_not_negative"),
        CheckConstraint(
            "discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100)",
            name="discount_percent_in_range",
        ),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_cost_fc IS NULL OR unit_cost_fc >= 0", name="unit_cost_not_negative"),
        CheckConstraint(
            "vat_amount_fc = 0 OR vat_account_id IS NOT NULL", name="vat_account_required"
        ),
        Index("ix_sales_invoice_lines_voucher", "voucher_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("sales_invoices.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)

    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warehouse_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Vật tư / đơn vị / kho — rỗng được với dòng dịch vụ. Phase 8 (kho) đọc ba
    cột này để lập phiếu xuất; ở đây chúng chỉ là chiều phân tích."""

    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=True
    )
    unit_price_fc: Mapped[Decimal | None] = mapped_column(
        Numeric(UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE), nullable=True
    )
    discount_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(DISCOUNT_PERCENT_PRECISION, DISCOUNT_PERCENT_SCALE), nullable=True
    )
    discount_amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Chiết khấu thương mại của dòng (FR-SYS-045 áp bậc, hoặc gõ tay). Đã trừ
    khỏi `amount_fc` — cột này giữ lại phần đã giảm cho bảng kê và báo cáo."""

    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    """Doanh thu của dòng, **sau** chiết khấu và **trước** thuế — nhập tay
    được, service không ép bằng `quantity × unit_price_fc − discount`: hóa đơn
    làm tròn theo cách của nó và số in trên hóa đơn là số phải khớp."""

    vat_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(VAT_RATE_PRECISION, VAT_RATE_SCALE), nullable=True
    )
    vat_amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )

    account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    """TK doanh thu bên Có (511/5111/5112) — hoặc TK giảm trừ doanh thu (521/
    511) trên chứng từ trả lại và giảm giá."""
    vat_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    """TK thuế GTGT đầu ra (33311) — bắt buộc khi dòng có thuế."""

    cogs_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    inventory_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    unit_cost_fc: Mapped[Decimal | None] = mapped_column(
        Numeric(UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE), nullable=True
    )
    """Cặp TK giá vốn / TK kho và đơn giá vốn (SRS 06 §3.1). Lát này chỉ nhận
    và lưu; bút toán Nợ 632 / Có 156 do phase 8 sinh khi tính xong giá xuất
    kho — xem `sales_invoices.cogs_posted`."""

    price_list_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_source: Mapped[str | None] = mapped_column(String(PRICE_SOURCE_MAX_LENGTH), nullable=True)
    """Tầng giá đã trả lời cho dòng (`kernel.pricing.PriceSource`) và bảng giá
    cụ thể, nếu có. Người dùng thấy một đơn giá tự điền và câu hỏi đầu tiên là
    "số này ở đâu ra" — lưu lại thì câu trả lời còn đúng cả sau khi bảng giá bị
    sửa. Không tham gia phép tính nào."""

    cost_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expense_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extended_dimensions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    """Đủ bộ chiều như dòng hóa đơn mua, trừ `bank_account_id` (hóa đơn bán
    không chạm 112x) — TK doanh thu có thể đòi bất kỳ chiều nào theo
    `detail_tracking`."""


class SalesSettlement(DatasetBase, Audited):
    """Một dòng đối trừ của chứng từ TRẢ LẠI / GIẢM GIÁ vào hóa đơn bán gốc —
    cùng khuôn `purchase_settlements` và `cash_settlements` (FR-SYS-066)."""

    __tablename__ = "sales_settlements"
    __table_args__ = (
        CheckConstraint(
            f"target_kind BETWEEN {SettlementTargetKind.SALES_INVOICE} "
            f"AND {SettlementTargetKind.OPENING_BALANCE}",
            name="target_kind_known",
        ),
        CheckConstraint("amount_fc > 0", name="amount_fc_positive"),
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint(
            "voucher_id", "target_kind", "target_id", name="uq_sales_settlements_voucher_target"
        ),
        Index("ix_sales_settlements_target", "target_kind", "target_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("sales_invoices.id", ondelete="CASCADE"), nullable=False
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
    """Chênh lệch tỷ giá giữa tỷ giá chứng từ giảm trừ và tỷ giá ghi nhận nợ —
    `posting_mapper` sinh cặp dòng 515/635 từ cột này."""
