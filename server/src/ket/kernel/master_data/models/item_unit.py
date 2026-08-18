"""Đơn vị tính quy đổi của một mã hàng (FR-SYS-041).

Mua theo thùng, nhập kho theo chiếc, bán theo lố: một mã hàng có nhiều đơn vị,
nhưng **một** đơn vị chính mà mọi số tồn quy về (`items.base_unit_id`). Mỗi dòng
ở đây là một đơn vị khác đơn vị chính, kèm tỷ lệ đổi về đơn vị chính.

**Phẳng, không phải chuỗi** (H68). "Quy đổi nhiều cấp" của FR-SYS-041 được diễn
đạt bằng nhiều dòng cùng trỏ về đơn vị chính (thùng = 24 chiếc, lố = 12 chiếc),
không phải thùng → lố → chiếc. Lý do là chính cảnh báo trong FR-SYS-041: chuỗi
thì mỗi cấp là một phép nhân nữa, nên sai số làm tròn **cộng dồn theo số cấp**,
và một mã hàng ba cấp sẽ ra số tồn khác nhau tùy người nhập chọn đơn vị nào.
Phẳng thì mọi quy đổi là **một** phép nhân từ chính con số người dùng khai.

`factor` = số đơn vị **chính** cho một đơn vị quy đổi. `quantity_base =
quantity × factor`, luôn nhân, không bao giờ chia — đó là lý do FR-SYS-041 khuyên
đơn vị chính nên là đơn vị nhỏ nhất.

**Dịch vụ cũng khai được đơn vị quy đổi**, khác mã quy cách vốn chỉ dành cho thứ
có tồn kho (`ItemVariantService._ensure_stock_item`). Bất đối xứng có chủ đích:
quy cách chỉ có nghĩa như một trục của báo cáo tồn kho, còn quy đổi số lượng thì
có nghĩa với **mọi** thứ bán theo số lượng — dịch vụ tính theo giờ mà báo giá
theo ngày là quy đổi thật, dùng ở dòng chứng từ bán của phase 7. Điều kiện duy
nhất là mã hàng phải có đơn vị chính để quy về.

**Không** có cờ `is_active` ở đây, khác `partner_bank_accounts`: chứng từ ở phase
7 chép cả `factor` vào dòng của nó (tỷ lệ phải in đúng như lúc lập, kể cả sau này
nhà cung cấp đổi cách đóng thùng), nên dòng ở đây không phải thứ chứng từ cũ trỏ
tới, và xóa là đường đúng khi thôi mua theo thùng. Nếu phase 7 hóa ra cần khóa
ngoại tới `item_units.id` thì lúc đó thêm cờ là một `ALTER TABLE`.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE

ITEM_UNIT_TABLE_NAME = "item_units"


class ItemUnit(DatasetBase, Audited, RowVersioned):
    """Một đơn vị quy đổi của một mã hàng, kèm tỷ lệ về đơn vị chính."""

    __tablename__ = ITEM_UNIT_TABLE_NAME
    __table_args__ = (
        # Một đơn vị khai hai lần cho cùng mã hàng là hai tỷ lệ cho cùng một phép
        # quy đổi, và không câu truy vấn nào chọn được cái đúng.
        UniqueConstraint("item_id", "unit_id", name="uq_item_units_item_unit"),
        # Tỷ lệ 0 biến mọi số lượng thành 0; tỷ lệ âm biến nhập thành xuất. Cả hai
        # đi qua được mọi phép kiểm phía trên nếu DB không nói không.
        CheckConstraint("factor > 0", name="factor_is_positive"),
        Index(f"ix_{ITEM_UNIT_TABLE_NAME}_item_id", "item_id"),
        Index(f"ix_{ITEM_UNIT_TABLE_NAME}_unit_id", "unit_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`: đơn vị quy đổi **thuộc về** mã hàng, không tồn tại độc lập —
    cùng lập luận đã ghi ở `partner_bank_account.py`. Đường xóa mã hàng vốn đã
    hẹp (chỉ mở khi chưa chứng từ nào dùng tới)."""

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    """`RESTRICT`: đơn vị tính là danh mục dùng chung, xóa nó khi còn tỷ lệ trỏ
    tới là làm mất tên đơn vị trên chứng từ đã lập."""

    factor: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    """Số đơn vị **chính** cho một đơn vị này. Thùng 24 chiếc → `24`.

    `NUMERIC` chứ không bao giờ số nhị phân, và lưu rộng hơn mức làm tròn của
    phase 8 (xem `kernel/quantity.py`): tỷ lệ được **nhân** vào số lượng trước
    khi làm tròn, nên nó phải rộng hơn cả số lượng — cùng lập luận đã áp cho tỷ
    giá ở `money.RATE_SCALE_DEFAULT`."""
