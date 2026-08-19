"""Snapshot số dư + hàng đợi tính lại (ADR-011, RT-22).

Slice 4A chỉ mang **schema và phép đánh dấu dirty**; job tính lại set-based và
dịch vụ truy vấn thuộc slice 4B. Schema phải có từ migration đầu của posting vì
`PostingService.post`/`unpost` đánh dấu hàng đợi ngay từ chứng từ đầu tiên —
không có nó thì snapshot sẽ đúng cho tới đúng lần lùi ngày đầu tiên.

Khóa snapshot **gọn** (RT-22): chỉ `(kỳ, sổ, chi nhánh, TK, tiền tệ)`. Không
mang đối tác/vật tư/chiều — tổ hợp nổ làm snapshot phình hơn cả `gl_postings`
và mất tác dụng. Số theo đối tác đọc `ar_ap_ledger` (phase 7), theo vật tư đọc
`inventory_balances` (phase 8), theo chiều đọc thẳng `gl_postings` giới hạn kỳ.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.currency.models import CURRENCY_CODE_LENGTH
from ket.kernel.persistence.base import DatasetBase
from ket.posting.engine.models import AMOUNT_PRECISION, AMOUNT_SCALE, Ledger


class AccountBalance(DatasetBase):
    """Một dòng snapshot: dư đầu / phát sinh / dư cuối của một tổ hợp khóa.

    Không `Audited`, không `RowVersioned`: bảng do job tính lại ghi đè bằng
    set-based SQL (ADR-014) — nó là **bộ nhớ đệm có kiểm soát** của
    `gl_postings`, không phải dữ liệu con người sửa. Nguồn sự thật để đối chiếu
    là chính `gl_postings` (check `snapshot_matches_postings`, slice 4D).
    """

    __tablename__ = "account_balances"
    __table_args__ = (
        CheckConstraint(
            f"ledger IN ({Ledger.FINANCIAL}, {Ledger.MANAGEMENT})", name="ledger_known"
        ),
        Index(
            "uq_account_balances_key",
            "period_id",
            "ledger",
            "branch_id",
            "account_id",
            "currency_code",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    period_id: Mapped[int] = mapped_column(
        ForeignKey("accounting_periods.id", ondelete="RESTRICT"), nullable=False
    )
    ledger: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    currency_code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)

    opening_debit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    opening_credit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    period_debit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    period_credit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    closing_debit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    closing_credit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )

    opening_debit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    period_debit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )

    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BalanceRecalcQueue(DatasetBase):
    """Kỳ bẩn đang chờ tính lại snapshot — một dòng cho `(sổ, chi nhánh, kỳ-từ)`.

    Một dòng `from_period_id` là đủ cho cả đuôi: mọi kỳ **sau** kỳ đó cùng bẩn
    vì dư đầu của chúng lăn từ dư cuối kỳ trước. Khóa chính chặn trùng nên ghi
    sổ trăm chứng từ cùng kỳ chỉ để lại một dòng.

    Báo cáo đọc bảng này để biết kỳ nào phải tính trực tiếp từ `gl_postings`
    (chậm nhưng luôn đúng) thay vì tin snapshot; khóa sổ bị **cấm** khi hàng
    đợi của kỳ chưa rỗng (phase-04 §Snapshot).
    """

    __tablename__ = "balance_recalc_queue"
    __table_args__ = (
        CheckConstraint(
            f"ledger IN ({Ledger.FINANCIAL}, {Ledger.MANAGEMENT})", name="ledger_known"
        ),
    )

    ledger: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    branch_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_period_id: Mapped[int] = mapped_column(
        ForeignKey("accounting_periods.id", ondelete="RESTRICT"), primary_key=True
    )

    marked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
