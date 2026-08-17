"""Đồng tiền và tỷ giá (N5, FR-NFR-032).

Doanh nghiệp Việt Nam ghi sổ bằng VND nhưng mua bán bằng USD, EUR, JPY. Chuẩn
mực đòi mỗi chứng từ ngoại tệ giữ **cả hai** con số: nguyên tệ (số thật trên hóa
đơn) và số quy đổi (số vào sổ cái), cùng với tỷ giá đã dùng. Giữ đủ ba thứ đó là
điều kiện để đánh giá lại chênh lệch tỷ giá cuối kỳ và để giải trình với cơ quan
thuế; giữ thiếu thì không tính ngược lại được.

Tỷ giá là **dữ liệu theo ngày**, không phải thiết lập: cùng một loại tiền có
nhiều loại tỷ giá cùng lúc (tỷ giá hạch toán, mua vào, bán ra của ngân hàng), và
mỗi loại đổi mỗi ngày. Vì vậy khóa chính gồm cả ba chiều.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.money import MONEY_SCALE_DEFAULT, MONEY_SCALE_MAX, RATE_SCALE_DEFAULT
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

CURRENCY_CODE_LENGTH = 3
"""Mã ISO 4217 — `VND`, `USD`. Ba ký tự là chuẩn quốc tế, không phải giới hạn tự đặt."""

RATE_PRECISION = 18
"""Tổng số chữ số của tỷ giá. `NUMERIC` chứ không `float`: tỷ giá được **nhân**
vào mọi số tiền ngoại tệ, nên sai số nhị phân ở đây không đứng yên một chỗ mà
nhân lên theo từng dòng chứng từ (`kernel/money.py`)."""


class RateType(StrEnum):
    """Loại tỷ giá — cùng ngày, cùng loại tiền, khác mục đích.

    Kế toán VN dùng tỷ giá hạch toán để ghi sổ, còn tỷ giá mua/bán của ngân hàng
    xuất hiện ở nghiệp vụ thật (bán ngoại tệ, thanh toán bằng ngoại tệ). Gộp
    chúng làm một là cách để chênh lệch tỷ giá cuối kỳ tính ra sai.
    """

    ACCOUNTING = "accounting"
    BANK_BUY = "bank_buy"
    BANK_SELL = "bank_sell"


RATE_TYPE_SQL_LIST = ", ".join(f"'{member.value}'" for member in RateType)
"""Danh sách giá trị cho ràng buộc `CHECK`, sinh từ chính `RateType`.

Chép tay ba chuỗi vào câu SQL là cách để enum và ràng buộc trôi lệch nhau ở lần
thêm loại tỷ giá thứ tư — mà lệch theo hướng nguy hiểm: Python nhận giá trị mới,
PostgreSQL từ chối nó, và lỗi hiện ra ở tầng driver chứ không ở tầng nghiệp vụ."""


class Currency(DatasetBase, Audited, RowVersioned):
    """Một loại tiền dùng trong dữ liệu kế toán này.

    Không dùng `MasterDataMixin`: đồng tiền không có cây, không có bản riêng
    theo chi nhánh (cả công ty ghi sổ bằng một đồng tiền hạch toán), và khóa
    chính của nó là chính mã ISO — ép nó vào khuôn danh mục sẽ thêm bốn cột
    không bao giờ dùng cùng một khóa `int` thứ hai vô nghĩa.
    """

    __tablename__ = "currencies"
    __table_args__ = (
        CheckConstraint(
            f"decimal_places BETWEEN 0 AND {MONEY_SCALE_MAX}", name="decimal_places_in_range"
        ),
        # Đúng **một** đồng tiền hạch toán. Không có đồng nào thì không quy đổi
        # được gì; hai đồng thì mọi báo cáo tổng hợp phụ thuộc vào việc truy vấn
        # trả dòng nào trước — một sai số không lặp lại được nên không gỡ được.
        # Chỉ mục bộ phận chặn vế "hai"; vế "không có" do
        # `ExchangeRateService.base_currency` chặn lúc chạy, vì DB không có ràng
        # buộc "ít nhất một dòng".
        Index(
            "uq_currencies_single_base", "is_base", unique=True, postgresql_where=text("is_base")
        ),
    )

    code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(100), nullable=True)

    decimal_places: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=MONEY_SCALE_DEFAULT,
        server_default=str(MONEY_SCALE_DEFAULT),
    )
    """Số chữ số thập phân của **nguyên tệ**. VND ghi tới đồng (0), USD tới cent
    (2), JPY không có phần lẻ (0). Số quy đổi vào sổ dùng `money.scale` chung của
    dữ liệu kế toán, không dùng cột này."""

    is_base: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Đồng tiền hạch toán — đồng mà sổ cái ghi bằng (LD-12)."""

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class ExchangeRate(DatasetBase, Audited):
    """Tỷ giá của một loại tiền, tại một ngày, theo một loại tỷ giá.

    `Audited` vì tỷ giá là **số liệu quyết định giá trị ghi sổ**: sửa một dòng ở
    đây làm đổi số tiền của mọi chứng từ ngoại tệ hạch toán theo nó, nên "ai đổi
    tỷ giá ngày 31/12" là câu hỏi kiểm toán viên hỏi thật.

    Không `RowVersioned`: bản ghi này là dữ liệu **nhập một lần** theo ngày, gần
    như không ai mở form sửa đồng thời với người khác. Thêm một tầng xung đột ở
    đây chỉ tạo ra `409` mà không ai biết làm gì với nó (xem `RowVersioned`).
    """

    __tablename__ = "exchange_rates"
    __table_args__ = (
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint(f"rate_type IN ({RATE_TYPE_SQL_LIST})", name="rate_type_known"),
    )

    currency_code: Mapped[str] = mapped_column(
        String(CURRENCY_CODE_LENGTH),
        ForeignKey("currencies.code", ondelete="RESTRICT"),
        primary_key=True,
    )
    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    rate_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    rate: Mapped[Decimal] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE_DEFAULT), nullable=False
    )
