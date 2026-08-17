"""Tài khoản ngân hàng của đối tác (FR-SYS-033).

Bảng con chứ không vài cột trên `partners`: yêu cầu nói "nhiều tài khoản", và
một đối tác thật sự có nhiều — tài khoản VND ở một ngân hàng, tài khoản ngoại tệ
ở ngân hàng khác, tài khoản riêng cho từng hợp đồng. Ba cột `bank_account_1..3`
là cách bảng nào cũng hết chỗ đúng vào lúc bận nhất.

Khóa ngoại **thật** tới `partners` và `banks`, không phải cặp
`(owner_type, owner_id)` dùng chung với nhân viên: một bảng gộp biến khóa ngoại
thành "trỏ vào bảng chung, tin rằng đúng loại", và PostgreSQL kiểm được cái thứ
nhất chứ không kiểm được cái thứ hai — cùng lập luận đã ghi ở
`master_data/base.py` khi chọn không gộp hai mươi danh mục vào một bảng.

`ON DELETE CASCADE` theo đối tác, khác `RESTRICT` của cây danh mục: tài khoản
ngân hàng **thuộc về** đối tác chứ không tồn tại độc lập, nên xóa đối tác mà giữ
lại tài khoản là giữ lại một mẩu dữ liệu không ai còn đường tới. Đường xóa đối
tác vốn đã hẹp — chỉ mở khi chưa chứng từ nào dùng tới (`master_data_usage`).
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
from ket.kernel.master_data.base import NAME_MAX_LENGTH
from ket.kernel.master_data.models.employee import BANK_ACCOUNT_MAX_LENGTH
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

PARTNER_BANK_ACCOUNT_TABLE_NAME = "partner_bank_accounts"

BANK_BRANCH_MAX_LENGTH = 255


class PartnerBankAccount(DatasetBase, Audited, RowVersioned):
    """Một tài khoản ngân hàng của một đối tác."""

    __tablename__ = PARTNER_BANK_ACCOUNT_TABLE_NAME
    __table_args__ = (
        UniqueConstraint(
            "partner_id", "account_number", name="uq_partner_bank_accounts_partner_account"
        ),
        CheckConstraint("account_number <> ''", name="account_number_not_blank"),
        CheckConstraint("account_holder <> ''", name="account_holder_not_blank"),
        # Đúng **một** tài khoản mặc định cho mỗi đối tác. Chỉ mục có điều kiện
        # chứ không cờ tự quản ở tầng ứng dụng: hai request cùng đặt mặc định
        # cho hai tài khoản khác nhau sẽ cùng đọc thấy "chưa có mặc định nào" và
        # cùng ghi — DB là chỗ duy nhất nối tiếp được hai lượt ghi đó.
        Index(
            "uq_partner_bank_accounts_default",
            "partner_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index("ix_partner_bank_accounts_partner_id", "partner_id"),
        Index("ix_partner_bank_accounts_bank_id", "bank_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    partner_id: Mapped[int] = mapped_column(
        ForeignKey("partners.id", ondelete="CASCADE"), nullable=False
    )
    bank_id: Mapped[int] = mapped_column(
        ForeignKey("banks.id", ondelete="RESTRICT"), nullable=False
    )
    """Ngân hàng **bắt buộc**: một số tài khoản không kèm ngân hàng thì không
    chuyển tiền được, và ủy nhiệm chi ở phase 6 sẽ phải hỏi lại đúng lúc người
    dùng tưởng mình đã khai xong."""

    account_number: Mapped[str] = mapped_column(String(BANK_ACCOUNT_MAX_LENGTH), nullable=False)
    account_holder: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    bank_branch: Mapped[str | None] = mapped_column(String(BANK_BRANCH_MAX_LENGTH), nullable=True)
    """Chi nhánh ngân hàng mở tài khoản — in trên ủy nhiệm chi, không phải chi
    nhánh của doanh nghiệp."""

    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    """Tài khoản cũ đã đóng vẫn phải giữ lại: nó nằm trên những ủy nhiệm chi đã
    lập của năm trước, và một dòng biến mất sẽ làm chứng từ cũ in ra thiếu."""
