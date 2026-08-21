"""Chi tiết phiếu thu/chi tiền mặt (SRS 03, FR-QUY-001..) — bảng thuộc module.

Cùng khuôn `gl_journal_lines` (phase-04 §Chứng từ): posting engine không đọc
các bảng này — module tự dịch chi tiết thành `PostingRequest`. Cột ở đây phục
vụ **màn hình, mẫu in và sửa lại**; sự thật đã ghi sổ nằm ở `gl_postings`.

Ba bảng, ba vòng đời:

* `cash_vouchers` — phần thân của header `vouchers` (PK = FK, một-một): loại
  phiếu, nghiệp vụ, TK quỹ, người nộp/nhận, trạng thái thủ quỹ.
* `cash_voucher_lines` — dòng định khoản nháp, cascade theo thân.
* `cash_settlements` — đối trừ công nợ của phiếu (`docs/srs/03` §4): mỗi dòng
  trỏ một chứng từ công nợ qua cặp `(target_kind, target_id)` của
  `kernel.protocols.SettlementTargetKind`. **Không FK** cho `target_id`, có
  chủ đích: đích nằm ở bảng của module khác tùy `target_kind` (hóa đơn bán
  phase 7, số dư đầu kỳ phase 4), và một FK đa hình là thứ PostgreSQL không
  nói được — người bảo đảm là service đối trừ + check toàn vẹn phase 4.

Khác `gl_journal_lines` một điểm cột: dòng ở đây mang **cặp Nợ/Có + một số
tiền** thay vì hai cột Nợ/Có tách — đó là cách kế toán viên đọc một phiếu thu
("Nợ 111/Có 131: 5.000.000"), và cũng là hình dạng mà nghiệp vụ định khoản
tự động (FR-SYS-025) điền sẵn. `posting_mapper` (lát 6B) trải nó thành hai
`PostingLine` một-bên khi ghi sổ.

Chỉ lưu **nguyên tệ** (`amount_fc`); tiền quy đổi tính lúc ghi sổ từ tỷ giá
trên header — cùng lập luận với `gl_journal_lines`: hai cột cùng nói một điều
là hai cột sẽ lệch. Không có `reason` riêng: "Lý do nộp/chi" trên mẫu in là
`vouchers.description` — thêm cột thứ hai cho cùng câu chữ là tạo chỗ lệch.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
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
from ket.kernel.contracts import PartnerKind
from ket.kernel.identifiers import uuid7
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.protocols import SettlementTargetKind
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

NOTE_MAX_LENGTH = 500

DESCRIPTION_MAX_LENGTH = 500
PAYER_RECEIVER_MAX_LENGTH = 255


class CashVoucherKind:
    """`cash_vouchers.kind` — hằng số nguyên, cùng lối `BalanceNature`."""

    RECEIPT = 0
    """Phiếu thu (PT)."""
    PAYMENT = 1
    """Phiếu chi (PC)."""


class TreasurerStatus:
    """`cash_vouchers.treasurer_status` (SRS 17 §3.1, FR-WHK-001..005)."""

    PENDING = 0
    """Chờ thủ quỹ ghi sổ quỹ."""
    BOOKED = 1
    """Thủ quỹ đã ghi sổ quỹ."""
    NOT_APPLICABLE = 2
    """Phân hệ thủ quỹ tắt — phiếu vào thẳng sổ quỹ lúc ghi sổ kế toán
    (FR-WHK-021)."""


class CashVoucher(DatasetBase, Audited):
    """Phần thân phiếu thu/chi — một-một với header `vouchers`.

    `Audited` nhưng không `RowVersioned`: tranh chấp sửa giải ở header (header
    có `row_version`), cùng lập luận với `gl_journal_lines`.
    """

    __tablename__ = "cash_vouchers"
    __table_args__ = (
        CheckConstraint(
            f"kind BETWEEN {CashVoucherKind.RECEIPT} AND {CashVoucherKind.PAYMENT}",
            name="kind_known",
        ),
        CheckConstraint("operation_code <> ''", name="operation_code_not_blank"),
        CheckConstraint(
            f"treasurer_status BETWEEN {TreasurerStatus.PENDING} "
            f"AND {TreasurerStatus.NOT_APPLICABLE}",
            name="treasurer_status_known",
        ),
        # Cặp cột sống chết cùng nhau — cùng lập luận `posted_stamp_complete`
        # của header: "đã ghi sổ quỹ nhưng không biết ai ghi" là nửa dữ liệu.
        CheckConstraint(
            "(treasurer_posted_at IS NULL) = (treasurer_posted_by IS NULL)",
            name="treasurer_stamp_complete",
        ),
        CheckConstraint(
            f"(treasurer_status = {TreasurerStatus.BOOKED}) = (treasurer_book_date IS NOT NULL)",
            name="treasurer_booked_has_date",
        ),
        CheckConstraint(
            f"treasurer_status <> {TreasurerStatus.BOOKED} OR treasurer_posted_at IS NOT NULL",
            name="treasurer_booked_has_stamp",
        ),
        CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)", name="partner_pair_complete"
        ),
        CheckConstraint(
            f"partner_kind IS NULL OR partner_kind BETWEEN {PartnerKind.CUSTOMER} "
            f"AND {PartnerKind.EMPLOYEE}",
            name="partner_kind_known",
        ),
        Index("ix_cash_vouchers_treasurer_status", "treasurer_status"),
        Index("ix_cash_vouchers_cash_account", "cash_account_id"),
    )

    id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    operation_code: Mapped[str] = mapped_column(String(OPERATION_CODE_MAX_LENGTH), nullable=False)
    """Nghiệp vụ đã chọn (FR-SYS-025) — `thu-khac`/`chi-khac` là lối cho phiếu
    tự định khoản, nên cột không cần `NULL`."""

    cash_account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    """TK quỹ (111x) của phiếu — thẻ đang chọn trên màn "Tiền vào tiền ra"."""

    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """Đối tác của cả phiếu (thu nợ một khách nhiều hóa đơn). Dòng vẫn mang
    đối tác riêng khi nghiệp vụ nhiều đối tượng — cùng khuôn dòng GLE."""

    payer_receiver_name: Mapped[str | None] = mapped_column(
        String(PAYER_RECEIVER_MAX_LENGTH), nullable=True
    )
    """"Người nộp tiền"/"Người nhận tiền" in trên phiếu — chữ tự do vì người
    cầm tiền đến không nhất thiết là một đối tác trong danh mục."""

    attachment_count: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """"Kèm theo … chứng từ gốc" trên mẫu in phiếu thu/chi."""

    treasurer_status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=TreasurerStatus.PENDING, server_default="0"
    )
    treasurer_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    treasurer_posted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """`user_id` trần như mọi tham chiếu `public.users`."""

    treasurer_book_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Ngày ghi sổ quỹ thủ quỹ chọn — `>= posting_date` (BR-WHK-05), service
    hàng đợi kiểm."""


class CashVoucherLine(DatasetBase, Audited):
    """Một dòng định khoản của phiếu thu/chi: cặp Nợ/Có + số tiền nguyên tệ."""

    __tablename__ = "cash_voucher_lines"
    __table_args__ = (
        CheckConstraint("amount_fc >= 0", name="amount_not_negative"),
        CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)", name="partner_pair_complete"
        ),
        CheckConstraint(
            f"partner_kind IS NULL OR partner_kind BETWEEN {PartnerKind.CUSTOMER} "
            f"AND {PartnerKind.EMPLOYEE}",
            name="partner_kind_known",
        ),
        Index("ix_cash_voucher_lines_voucher", "voucher_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)

    debit_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    credit_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    """Hai cột đều `NULL` được **ở bản nháp**: nghiệp vụ "Thu khác" điền sẵn
    một bên, người dùng điền nốt trước khi ghi sổ — `posting_mapper` (6B) từ
    chối dịch dòng còn thiếu bên."""

    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )

    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cost_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expense_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warehouse_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Đủ bộ chiều như dòng GLE: TK đối ứng có thể đòi bất kỳ chiều nào theo
    `detail_tracking`, và thiếu cột là thiếu đường điền."""

    extended_dimensions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    """Chiều mở rộng dạng `{"<dimension_id>": value_id}` — cùng khuôn và cùng
    lý do JSONB với `gl_journal_lines`."""


class CashSettlement(DatasetBase, Audited):
    """Một dòng đối trừ công nợ của phiếu thu/chi (`docs/srs/03` §4, FR-SYS-066)."""

    __tablename__ = "cash_settlements"
    __table_args__ = (
        CheckConstraint(
            f"target_kind BETWEEN {SettlementTargetKind.SALES_INVOICE} "
            f"AND {SettlementTargetKind.OPENING_BALANCE}",
            name="target_kind_known",
        ),
        CheckConstraint("amount_fc > 0", name="amount_fc_positive"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # Một phiếu không đối trừ hai lần vào cùng một chứng từ công nợ — hai
        # dòng như vậy chỉ có thể là bấm nhầm, và gộp chúng là việc của form.
        UniqueConstraint(
            "voucher_id", "target_kind", "target_id", name="uq_cash_settlements_voucher_target"
        ),
        Index("ix_cash_settlements_target", "target_kind", "target_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    target_kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False)

    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    """Số đối trừ theo **nguyên tệ của hóa đơn** — người dùng nhập cột "Số thu"."""

    amount: Mapped[Decimal] = mapped_column(Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False)
    """Số VND theo tỷ giá của **phiếu** (tỷ giá lúc thanh toán)."""

    fx_diff: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Chênh lệch tỷ giá thu/trả tiền, VND, có dấu: dương = lãi tỷ giá.
    `settlement_service` (6B) tính = `amount_fc × (tỷ giá phiếu − tỷ giá ghi
    nhận nợ)` và hạch toán vào TK cấu hình (FR-SYS-066)."""


class CashCountSheet(DatasetBase, Audited):
    """Biên bản kiểm kê quỹ (FR-QUY-030/031) — đối chiếu đếm thật với số sổ.

    `book_balance` là **ảnh chụp** số sổ tại lúc lập biên bản: số sổ đổi theo
    từng chứng từ ghi thêm, mà biên bản kiểm kê là chứng cứ của MỘT thời điểm —
    tính lại sau này ra số khác là đúng, và chính vì thế phải chụp.

    `counted_total` là số đếm thật do người kiểm kê chịu trách nhiệm; các dòng
    mệnh giá là chi tiết **tùy chọn** (đơn vị đếm nhanh chỉ ghi tổng) — khi có
    dòng, service bắt tổng dòng khớp `counted_total` lúc ghi.
    """

    __tablename__ = "cash_count_sheets"
    __table_args__ = (
        CheckConstraint("counted_total >= 0", name="counted_total_not_negative"),
        Index("ix_cash_count_sheets_account_date", "cash_account_id", "count_date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    cash_account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    count_date: Mapped[date] = mapped_column(Date, nullable=False)

    book_balance: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    counted_total: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(NOTE_MAX_LENGTH), nullable=True)

    adjustment_voucher_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("vouchers.id", ondelete="SET NULL"), nullable=True
    )
    """Phiếu thu/chi xử lý chênh lệch đã sinh từ biên bản này (FR-QUY-031).
    `SET NULL` khi phiếu bị xóa lúc còn nháp — biên bản quay về trạng thái
    "chưa xử lý", sinh lại được."""

    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CashCountSheetLine(DatasetBase, Audited):
    """Một dòng mệnh giá của biên bản kiểm kê: mệnh giá × số tờ."""

    __tablename__ = "cash_count_sheet_lines"
    __table_args__ = (
        CheckConstraint("denomination > 0", name="denomination_positive"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_cash_count_sheet_lines_sheet", "sheet_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    sheet_id: Mapped[UUID] = mapped_column(
        ForeignKey("cash_count_sheets.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    denomination: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
