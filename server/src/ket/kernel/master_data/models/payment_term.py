"""Điều khoản thanh toán (`docs/srs/01` §7, FR-SYS-032).

Ba cột riêng ngoài bộ chung, và cả ba đều có nơi đọc ngay ở lát kế tiếp: đối
tượng công nợ (3B-2) gắn một điều khoản mặc định, và từ đó hạn thanh toán của
mọi hóa đơn tự tính ra thay vì gõ tay từng lần.

Cách đọc bộ ba: "thanh toán trong `due_days` ngày; trả trong `discount_days`
ngày đầu thì được chiết khấu `discount_percent`%". Điều khoản 2/10 net 30 quen
thuộc là `due_days=30, discount_days=10, discount_percent=2`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import CheckConstraint, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

PAYMENT_TERM_TABLE_NAME = "payment_terms"

DISCOUNT_PRECISION = 7
DISCOUNT_SCALE = 4
"""Bốn chữ số thập phân cho phần trăm: đủ cho `0,0001%`, và `Numeric` chứ không
dấu phẩy động vì con số này nhân vào tiền (`test_no_float_in_domain.py`)."""


def _payment_term_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(PAYMENT_TERM_TABLE_NAME),
        # Số ngày âm không có nghĩa nào cả, và một dấu trừ gõ nhầm sẽ đẩy hạn
        # thanh toán về **quá khứ** — hóa đơn vừa lập đã nằm trong báo cáo nợ
        # quá hạn.
        CheckConstraint("due_days >= 0", name="due_days_not_negative"),
        CheckConstraint("discount_days >= 0", name="discount_days_not_negative"),
        CheckConstraint(
            "discount_percent >= 0 AND discount_percent <= 100",
            name="discount_percent_in_range",
        ),
        # Chiết khấu chỉ có nghĩa khi cửa sổ hưởng chiết khấu nằm **trong** hạn
        # nợ: `discount_days > due_days` mô tả một ưu đãi không ai chạm tới được.
        CheckConstraint("discount_days <= due_days", name="discount_window_within_due"),
    )


class PaymentTerm(MasterDataRow):
    """Một điều khoản thanh toán dùng để tính hạn nợ và chiết khấu thanh toán."""

    __tablename__ = PAYMENT_TERM_TABLE_NAME
    __table_args__ = _payment_term_table_args()

    due_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    """Số ngày được nợ tính từ ngày chứng từ. `0` = thanh toán ngay."""

    discount_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """Số ngày đầu được hưởng chiết khấu thanh toán sớm. `0` = không có."""

    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(DISCOUNT_PRECISION, DISCOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default="0",
    )
    """Tỷ lệ chiết khấu khi trả trong `discount_days` ngày đầu."""


class PaymentTermFields(BaseModel):
    """Phần riêng của điều khoản thanh toán trên API (`registry.CatalogSpec`).

    Lặp lại luật của `CHECK` phía DB, có chủ đích — hai lớp cho hai đường vào.
    Lớp này trả lỗi **theo trường** cho người đang điền form; lớp `CHECK` canh
    mọi đường ghi khác, mà đường thứ hai đã nhìn thấy được rồi: nhập Excel ở lát
    3C không đi qua Pydantic của endpoint này.
    """

    due_days: int = Field(title="Số ngày được nợ", default=0, ge=0)
    discount_days: int = Field(title="Số ngày hưởng chiết khấu", default=0, ge=0)
    discount_percent: Decimal = Field(
        title="Tỷ lệ chiết khấu (%)", default=Decimal(0), ge=0, le=100
    )

    @model_validator(mode="after")
    def _discount_window_within_due(self) -> Self:
        if self.discount_days > self.due_days:
            raise ValueError(
                "Số ngày hưởng chiết khấu không được vượt quá số ngày được nợ — "
                "cửa sổ chiết khấu như vậy sẽ không ai chạm tới được"
            )
        return self
