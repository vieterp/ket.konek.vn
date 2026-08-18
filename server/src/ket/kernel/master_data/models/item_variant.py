"""Mã quy cách (biến thể) của một mã hàng — màu, size (FR-SYS-046).

**Vì sao bảng này có ở lát nền dù FR-SYS-046 là SHOULD** (H65): yêu cầu nói theo
dõi quy cách "trên chứng từ nhập/xuất **và báo cáo tồn kho**", tức quy cách là một
**trục khóa** của bảng tồn kho — `inventory_balances (chi nhánh, kho, VTHH, lô,
serial, quy cách)` ở phase 8. Thêm một trục khóa vào bảng tồn kho sau khi đã có
số tồn là đúng thứ LD-09 và rủi ro SRS 19 §9 #3 nói là không làm được. Giao diện
khai quy cách và báo cáo tồn theo quy cách thì hoãn được; **bảng** thì không.

Một bảng riêng chứ không một cột chuỗi trên dòng chứng từ: cột chuỗi thì "Đỏ",
"đỏ" và "Do" là ba quy cách khác nhau trong báo cáo tồn kho, và không có gì để
người dùng chọn từ một danh sách.

Không phải danh mục (`MasterDataRow`): quy cách không có cây, không có mã duy nhất
theo chi nhánh, và phạm vi chi nhánh của nó là phạm vi của mã hàng chủ. Nới khung
danh mục để nó nhận thêm hình dạng này là đúng lối "bẻ cong khung dùng chung" mà
§Risk của phase 3 cảnh báo.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.base import CODE_MAX_LENGTH, NAME_MAX_LENGTH
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

ITEM_VARIANT_TABLE_NAME = "item_variants"


class ItemVariant(DatasetBase, Audited, RowVersioned):
    """Một quy cách của một mã hàng."""

    __tablename__ = ITEM_VARIANT_TABLE_NAME
    __table_args__ = (
        UniqueConstraint("item_id", "code", name="uq_item_variants_item_code"),
        CheckConstraint("code <> ''", name="code_not_blank"),
        CheckConstraint("name <> ''", name="name_not_blank"),
        Index(f"ix_{ITEM_VARIANT_TABLE_NAME}_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    item_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ITEM_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )

    code: Mapped[str] = mapped_column(String(CODE_MAX_LENGTH), nullable=False)
    """Mã người dùng đặt, duy nhất **trong một mã hàng** — `"DO-M"`, `"XANH-L"`.
    Không duy nhất toàn bảng: "Đỏ" của áo và "Đỏ" của mũ là hai dòng độc lập, và
    một mã duy nhất toàn bảng sẽ buộc người dùng ghép tên hàng vào mã quy cách."""

    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    """Quy cách ngừng kinh doanh vẫn phải giữ lại: nó nằm trên chứng từ nhập xuất
    của năm trước và trên số tồn đầu kỳ, nên một dòng biến mất sẽ làm báo cáo tồn
    kho của năm đó thiếu một cột. Ẩn khỏi ô chọn là việc của `is_active`; xóa hẳn
    chỉ mở khi chưa chứng từ nào dùng tới."""
