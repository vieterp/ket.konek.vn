"""Năm tài chính và kỳ kế toán (LD-12, FR-NFR-031/033, FR-SYS-004).

Hai bảng chứ không một: **năm** mang những quyết định chốt một lần cho cả năm
(chế độ kế toán TT200/TT133, đồng tiền hạch toán, phương pháp tính giá xuất kho,
phương pháp tính thuế GTGT), còn **kỳ** là đơn vị khóa sổ. Gộp làm một thì mỗi
tháng lại phải trả lời lại những câu chỉ hỏi một lần mỗi năm, và không có gì
ngăn tháng 3 dùng TT200 còn tháng 4 dùng TT133.

Nhiều năm tài chính tồn tại song song trong cùng một dữ liệu kế toán
(FR-NFR-033): tháng 1 năm sau người ta vẫn đang chốt sổ năm trước, và cả hai
năm đều phải ghi được.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.currency.models import CURRENCY_CODE_LENGTH
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

PERIODS_PER_YEAR = 12
"""Số kỳ sinh tự động khi tạo năm tài chính."""

ADJUSTMENT_PERIOD_NO = 13
"""Kỳ điều chỉnh — nơi ghi bút toán kết chuyển và điều chỉnh sau ngày 31/12 mà
vẫn thuộc niên độ cũ. **Không** sinh sẵn cùng 12 kỳ kia: nó chỉ ra đời khi khóa
sổ năm (phase 10a), và một kỳ 13 mở sẵn quanh năm là chỗ để chứng từ rơi nhầm
vào mà không ai nhận ra."""


class AccountingScheme(StrEnum):
    """Chế độ kế toán áp dụng cho năm (LD-06, FR-SYS-004)."""

    TT200 = "TT200"
    TT133 = "TT133"


class InventoryValuationMethod(StrEnum):
    """Phương pháp tính giá xuất kho — chốt theo năm, không đổi giữa chừng."""

    WEIGHTED_AVERAGE_PERIOD = "wavg_period"
    WEIGHTED_AVERAGE_MOVING = "wavg_moving"
    FIFO = "fifo"
    SPECIFIC = "specific"


class VatMethod(StrEnum):
    """Phương pháp tính thuế GTGT."""

    DEDUCTION = "deduction"
    DIRECT = "direct"


def _sql_values(enum_type: type[StrEnum]) -> str:
    """Danh sách giá trị cho `CHECK`, sinh từ enum để hai bên không trôi lệch."""
    return ", ".join(f"'{member.value}'" for member in enum_type)


class FiscalYear(DatasetBase, Audited, RowVersioned):
    """Một niên độ kế toán và những quyết định chốt một lần của nó."""

    __tablename__ = "fiscal_years"
    __table_args__ = (
        CheckConstraint("end_date > start_date", name="date_range_ordered"),
        CheckConstraint(
            f"accounting_scheme IN ({_sql_values(AccountingScheme)})",
            name="accounting_scheme_known",
        ),
        CheckConstraint(
            f"inventory_valuation_method IN ({_sql_values(InventoryValuationMethod)})",
            name="inventory_valuation_method_known",
        ),
        CheckConstraint(f"vat_method IN ({_sql_values(VatMethod)})", name="vat_method_known"),
        Index("ix_fiscal_years_start_date", "start_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    """Nhãn người dùng đọc: `2026`, hoặc `2026-2027` cho niên độ lệch năm dương lịch."""

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    accounting_scheme: Mapped[str] = mapped_column(String(10), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)
    inventory_valuation_method: Mapped[str] = mapped_column(String(20), nullable=False)
    vat_method: Mapped[str] = mapped_column(String(20), nullable=False)

    is_closed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Khóa cả năm — khác với khóa từng kỳ. Đặt ở đây vì "quyết toán xong năm
    2025" là một sự kiện của năm, và nó phải chặn được cả kỳ 13 lẫn mọi thao tác
    mở lại kỳ con."""


class AccountingPeriod(DatasetBase, Audited, RowVersioned):
    """Một kỳ trong năm — đơn vị khóa sổ (FR-NFR-031).

    `locked_at`/`locked_by` là **dữ liệu**, không phải cờ boolean: câu hỏi kiểm
    toán không phải "kỳ này có khóa không" mà "ai khóa, lúc nào" (FR-NFR-013).
    Một cột `is_locked` trả lời được câu thứ nhất và mất hẳn câu thứ hai.
    """

    __tablename__ = "accounting_periods"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="date_range_ordered"),
        CheckConstraint(
            f"period_no BETWEEN 1 AND {ADJUSTMENT_PERIOD_NO}", name="period_no_in_range"
        ),
        # Khóa kỳ ghi hai cột cùng lúc; một nửa dữ liệu ("đã khóa nhưng không
        # biết ai khóa") là thứ chỉ phát hiện ra lúc cần giải trình, tức là quá
        # muộn. Ràng buộc rẻ hơn nhiều so với việc tin rằng mọi đường ghi đều
        # nhớ ghi đủ.
        CheckConstraint("(locked_at IS NULL) = (locked_by IS NULL)", name="lock_stamp_complete"),
        Index("uq_accounting_periods_year_no", "fiscal_year_id", "period_no", unique=True),
        Index("ix_accounting_periods_range", "start_date", "end_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fiscal_year_id: Mapped[int] = mapped_column(
        ForeignKey("fiscal_years.id", ondelete="RESTRICT"), nullable=False
    )
    period_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """`user_id` trần, không khóa ngoại: `users` nằm ở schema điều khiển và
    không có khóa ngoại nào được trỏ ra ngoài schema dataset (xem
    `persistence/base.py`)."""
