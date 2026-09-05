"""Chi tiết chứng từ nghiệp vụ khác (FR-GLE-001) — bảng thuộc module.

Posting engine không đọc bảng này (phase-04 §Chứng từ): module tự dịch từng
dòng thành `PostingRequest`. Vì thế cột ở đây phục vụ **màn hình và sửa lại**
— nó là bản nháp có cấu trúc của định khoản, còn sự thật đã ghi sổ nằm ở
`gl_postings`.

`ON DELETE CASCADE` theo header: dòng chi tiết không có đời sống riêng, và
`VoucherService.delete` dựa vào chính khóa ngoại này. Bỏ ghi sổ **không** đụng
bảng này (chỉ `gl_postings` bị xóa) — đó là thứ giữ cho post → unpost → post
lại cho kết quả giống hệt.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
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
from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.identifiers import uuid7
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.protocols import SettlementTargetKind
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

DESCRIPTION_MAX_LENGTH = 500


class JournalLine(DatasetBase, Audited):
    """Một dòng định khoản của chứng từ nghiệp vụ khác.

    `Audited` vì người dùng sửa trực tiếp từng dòng qua form — diff số tiền
    trên một dòng nháp là vết mà kiểm toán nội bộ muốn thấy trước cả khi ghi
    sổ. Không `RowVersioned`: tranh chấp giải ở mức chứng từ (header có
    `row_version`), hai người cùng sửa một chứng từ đã bị chặn ở đó.
    """

    __tablename__ = "gl_journal_lines"
    __table_args__ = (
        CheckConstraint("debit_fc >= 0 AND credit_fc >= 0", name="amounts_not_negative"),
        CheckConstraint("NOT (debit_fc > 0 AND credit_fc > 0)", name="not_both_sides"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        Index("ix_gl_journal_lines_voucher", "voucher_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    corresponding_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    currency_code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE_DEFAULT), nullable=False, default=Decimal(1)
    )
    debit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    credit_fc: Mapped[Decimal] = mapped_column(
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
    bank_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """TK ngân hàng doanh nghiệp của dòng 112x (chiều `bank_account`, lát 6G-1)."""

    extended_dimensions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)
    """Chiều mở rộng của dòng, dạng `{"<dimension_id>": value_id}`.

    JSONB chứ không bảng con, khác với `posting_dimension_values`: bảng kia là
    dữ liệu **đã ghi sổ** mà báo cáo truy vấn theo chiều (cần bảng thật + index);
    còn đây là bản nháp chỉ có một người đọc — chính form của nó. Khi ghi sổ,
    `posting_mapper` dựng lại thành `ExtendedDimensionValue` có kiểu.
    """

    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)


class JournalSettlement(DatasetBase, Audited):
    """Một dòng đối trừ công nợ của chứng từ nghiệp vụ khác (lát 7C-3).

    Cùng khuôn `cash_settlements` / `purchase_settlements` / `sales_settlements`,
    khác đúng một cột: **`journal_line_id`**. Ba bảng kia gắn dòng đối trừ vào
    *chứng từ*, vì mỗi chứng từ ấy chỉ có một đối tác và một TK công nợ. Chứng
    từ GLE thì không — một bút toán bù trừ 131 ↔ 331 chạm hai TK công nợ, và
    một bút toán phân loại lại đầu năm chạm nhiều đối tác cùng lúc. Đối trừ vì
    thế thuộc về **dòng định khoản**, không thuộc về chứng từ: nó là thứ trả
    lời "số Có 131 ở dòng 2 này giảm nợ của hóa đơn nào".

    Hệ quả kéo theo: phép kiểm BR-QUY-03 ("tổng đối trừ = tổng tiền chứng từ")
    áp cho **từng dòng**, không cho cả chứng từ — xem `settlement_service`.
    """

    __tablename__ = "gl_journal_settlements"
    __table_args__ = (
        CheckConstraint(
            f"target_kind BETWEEN {SettlementTargetKind.SALES_INVOICE} "
            f"AND {SettlementTargetKind.JOURNAL_PAYABLE}",
            name="target_kind_known",
        ),
        CheckConstraint("amount_fc > 0", name="amount_fc_positive"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # Duy nhất theo DÒNG chứ không theo chứng từ: hai dòng của cùng một
        # bút toán bù trừ được phép trỏ vào cùng một hóa đơn đích (mỗi dòng
        # giảm một phần), và chặn ở mức chứng từ sẽ cấm đúng nghiệp vụ ấy.
        UniqueConstraint(
            "journal_line_id",
            "target_kind",
            "target_id",
            name="uq_gl_journal_settlements_line_target",
        ),
        Index("ix_gl_journal_settlements_voucher", "voucher_id"),
        Index("ix_gl_journal_settlements_target", "target_kind", "target_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), nullable=False
    )
    """Giữ cả `voucher_id` lẫn `journal_line_id` dù cột đầu suy được từ cột
    sau: mọi lượt đọc theo chứng từ (ghi sổ, bỏ ghi sổ, nộp nhánh cho check
    toàn vẹn) sẽ phải join `gl_journal_lines` chỉ để lọc — và một trong số đó
    chạy trên mọi lượt job toàn vẹn."""

    journal_line_id: Mapped[UUID] = mapped_column(
        ForeignKey("gl_journal_lines.id", ondelete="CASCADE"), nullable=False
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
    """Chênh lệch giữa tỷ giá chứng từ GLE và tỷ giá ghi nhận nợ.

    **Lưu nhưng chưa sinh bút toán 515/635 ở lát này**: dòng định khoản của
    chứng từ GLE do người dùng tự gõ, nên cặp bù chênh lệch tỷ giá cũng là thứ
    người dùng tự gõ — hệ thống chèn thêm một cặp nữa là ghi đè lên bút toán
    người ta cố ý lập. Cột giữ số để màn hình và báo cáo đối chiếu chỉ ra được
    phần chênh, và để lát nào mở tự-động-hóa nó có sẵn dữ liệu."""
