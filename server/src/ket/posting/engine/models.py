"""Bảng phát sinh chung `gl_postings` — một bảng cho mọi phân hệ (ADR-005/006).

Bất biến của bảng (phase-04 §Bảng phát sinh chung): chỉ INSERT khi ghi sổ và
DELETE khi bỏ ghi sổ, **không bao giờ UPDATE**. Sửa chứng từ đã ghi sổ = bỏ ghi
sổ → sửa → ghi sổ lại trong một transaction. Kỳ đã khóa thì cả hai đường đều bị
chặn — ở tầng dịch vụ bằng `FOR SHARE` trên dòng kỳ (RT-09) và ở tầng DB bằng
trigger `gl_postings_period_guard` (migration `0009`).

**Không** mixin `Audited`, có chủ đích: dòng phát sinh là dữ liệu **suy ra** từ
chứng từ — vết kiểm toán nằm ở chuyển trạng thái của `vouchers` (ai ghi sổ, ai
bỏ ghi sổ, lúc nào), và nhật ký hóa 2 triệu dòng suy ra mỗi năm chỉ làm loãng
thứ kiểm toán viên cần đọc. Đường ghi hàng loạt của engine vì thế cũng không đi
qua listener ORM (giới hạn đã nêu ở `auditing/listener.py`).

Cột chiều (đối tác, vật tư, sáu chiều lõi) cố ý **không có khóa ngoại**: chúng
được validator kiểm lúc ghi sổ, và tám ràng buộc khóa ngoại trên bảng
2M dòng/năm là chi phí trả cho mỗi INSERT ở đúng bảng nóng nhất hệ thống —
trong khi dòng chỉ sinh ra từ chứng từ đã qua kiểm.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import IntEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.persistence.base import DatasetBase

AMOUNT_PRECISION = 18
AMOUNT_SCALE = 2
"""`NUMERIC(18,2)` theo phase-04. Đủ cho số tiền tới ~10^16 — trần thực tế của
sổ VND — và khớp `money.scale` mặc định. Bản cài đặt `money.scale` lớn hơn 2
sẽ bị cột này làm tròn thêm một lần: giới hạn đã biết, ghi ở đây để người nới
`MONEY_SCALE_MAX` nhìn thấy nó."""

DESCRIPTION_MAX_LENGTH = 500


class Ledger(IntEnum):
    """Hai hệ thống sổ hoạt động song song (N3, LD-07, FR-NFR-031)."""

    FINANCIAL = 0
    MANAGEMENT = 1


class GlPosting(DatasetBase):
    """Một dòng phát sinh trên một sổ — đơn vị nhỏ nhất của sổ cái."""

    __tablename__ = "gl_postings"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts_not_negative"),
        CheckConstraint("NOT (debit > 0 AND credit > 0)", name="not_both_sides"),
        CheckConstraint("debit_fc >= 0 AND credit_fc >= 0", name="fc_amounts_not_negative"),
        CheckConstraint("NOT (debit_fc > 0 AND credit_fc > 0)", name="fc_not_both_sides"),
        CheckConstraint(
            f"ledger IN ({Ledger.FINANCIAL}, {Ledger.MANAGEMENT})", name="ledger_known"
        ),
        Index("uq_gl_postings_line", "voucher_id", "ledger", "line_no", unique=True),
        Index("ix_gl_postings_acc_period", "ledger", "account_id", "period_id", "branch_id"),
        Index(
            "ix_gl_postings_partner",
            "ledger",
            "partner_kind",
            "partner_id",
            "period_id",
            postgresql_where=text("partner_id IS NOT NULL"),
        ),
        Index("ix_gl_postings_date", "ledger", "posting_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=False
    )
    """`RESTRICT` chứ không `CASCADE`: xóa chứng từ khi còn dòng sổ là lỗi của
    luồng gọi (phải bỏ ghi sổ trước) — DB không được im lặng dọn hộ."""

    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_line_id: Mapped[UUID | None] = mapped_column(nullable=True)
    """Dòng chi tiết của module đã sinh ra dòng sổ này — để đối chiếu, không FK
    vì bảng chi tiết thuộc module và engine không biết tên nó (luật phụ thuộc #3)."""

    ledger: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_id: Mapped[int] = mapped_column(Integer, nullable=False)

    account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    corresponding_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """TK đối ứng — phục vụ sổ Nhật ký chung và sổ cái kiểu VN (in cột đối
    ứng). Không FK: nó là chú thích trình bày, TK thật đã có dòng riêng."""

    currency_code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE_DEFAULT), nullable=False
    )
    debit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    credit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    debit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    credit: Mapped[Decimal] = mapped_column(
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

    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)


class PostingDimensionValue(DatasetBase):
    """Giá trị chiều **mở rộng** của một dòng phát sinh (LD-08, EAV có chủ đích).

    `CASCADE` theo dòng phát sinh: bỏ ghi sổ xóa dòng thì giá trị chiều đi
    theo — chúng không có đời sống riêng. Không RLS: phạm vi của nó là phạm vi
    của dòng cha, và mọi đường đọc đều join qua `gl_postings` đã bị RLS lọc.
    """

    __tablename__ = "posting_dimension_values"

    posting_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("gl_postings.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dimension_id: Mapped[int] = mapped_column(
        ForeignKey("analysis_dimensions.id", ondelete="RESTRICT"), primary_key=True
    )
    value_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """`id` trong `analysis_dimension_values` (chiều `list`) hoặc trong danh mục
    mà `master_slug` trỏ tới (chiều `master`) — hai đích nên không FK được;
    validator ghi sổ kiểm lúc ghi."""
