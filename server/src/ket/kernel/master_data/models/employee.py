"""Nhân viên (`docs/srs/01` §5.2, FR-SYS-035/036).

Nhân viên là **hai** thứ cùng lúc: một đối tượng công nợ (tạm ứng, hoàn ứng —
phase 6/7) và một đối tượng tính lương (phase 9). Đó là lý do nó là một danh mục
riêng chứ không một cờ trên `partners`: hai bên có bộ trường khác hẳn nhau, và
một bảng gộp sẽ có phân nửa số cột luôn rỗng ở mỗi dòng.

**Cột đưa vào lát này** là phần *định danh và liên hệ* — thứ trả lời "người này
là ai": phòng ban, chức danh, số CMND/CCCD, mã số thuế TNCN, và tài khoản nhận
lương.

**Cột hoãn tới phase 9** (H59) là phần *tham số tính toán*: lương thỏa thuận, hệ
số lương, số người phụ thuộc, số sổ BHXH. Không phải vì chúng không quan trọng
mà vì ý nghĩa của chúng do chỗ tính quyết định — "lương thỏa thuận" là gộp hay
chưa gộp bảo hiểm, tính theo tháng hay theo ngày công, là câu hỏi mà bảng lương
trả lời chứ không phải danh mục. Khai trước ở đây là đoán một hợp đồng chưa tồn
tại (H50); thêm sau là một `ALTER TABLE`.

Tài khoản nhận lương nằm **trên chính dòng nhân viên**, khác đối tác (bảng con):
FR-SYS-033 nói "nhiều tài khoản" cho đối tượng công nợ, còn §5.2 chỉ nêu *một*
tài khoản nhận lương. Một bảng con cho một dòng là một lần `JOIN` và một bộ route
không ai gọi.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import (
    NAME_MAX_LENGTH,
    MasterDataRow,
    master_data_table_args,
)
from ket.kernel.master_data.models.partner import (
    EMAIL_MAX_LENGTH,
    PHONE_MAX_LENGTH,
    TAX_CODE_MAX_LENGTH,
)
from ket.kernel.master_data.row_rules import RowRule

EMPLOYEE_TABLE_NAME = "employees"

ID_NUMBER_MAX_LENGTH = 20
"""CCCD 12 số, CMND cũ 9 số, hộ chiếu tối đa 9–12 ký tự."""

BANK_ACCOUNT_MAX_LENGTH = 50
DEPARTMENT_MAX_LENGTH = 150
POSITION_MAX_LENGTH = 150


def _employee_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(EMPLOYEE_TABLE_NAME),
        CheckConstraint("id_number IS NULL OR id_number <> ''", name="id_number_not_blank"),
        CheckConstraint("tax_code IS NULL OR tax_code <> ''", name="tax_code_not_blank"),
        # Số tài khoản mà không biết của ngân hàng nào thì không lập được ủy
        # nhiệm chi trả lương — nửa thông tin ở đây tệ hơn không có, vì màn hình
        # hiện ra như đã khai xong.
        CheckConstraint(
            "(bank_account_number IS NULL) = (bank_id IS NULL)",
            name="bank_account_needs_bank",
        ),
        Index(f"ix_{EMPLOYEE_TABLE_NAME}_tax_code", "tax_code"),
        Index(f"ix_{EMPLOYEE_TABLE_NAME}_bank_id", "bank_id"),
    )


class Employee(MasterDataRow):
    """Một nhân viên — vừa là đối tượng công nợ tạm ứng, vừa là đối tượng tính lương."""

    __tablename__ = EMPLOYEE_TABLE_NAME
    __table_args__ = _employee_table_args()

    department: Mapped[str | None] = mapped_column(String(DEPARTMENT_MAX_LENGTH), nullable=True)
    """Phòng ban dạng chữ, **chưa** phải khóa ngoại tới một danh mục cơ cấu tổ
    chức: FR-SYS-050 nói tới cơ cấu tổ chức dạng cây, nhưng chưa có nơi nào
    trong v1 *đọc* nó để phân bổ chi phí hay để phân quyền. Cây nhân viên
    (`parent_id`) đã gom được theo phòng ban cho màn hình danh sách."""

    position: Mapped[str | None] = mapped_column(String(POSITION_MAX_LENGTH), nullable=True)

    id_number: Mapped[str | None] = mapped_column(String(ID_NUMBER_MAX_LENGTH), nullable=True)
    """Số CMND/CCCD/hộ chiếu — in trên hợp đồng lao động và bảng kê thuế TNCN."""

    tax_code: Mapped[str | None] = mapped_column(String(TAX_CODE_MAX_LENGTH), nullable=True)
    """Mã số thuế thu nhập cá nhân."""

    phone: Mapped[str | None] = mapped_column(String(PHONE_MAX_LENGTH), nullable=True)
    email: Mapped[str | None] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=True)

    bank_id: Mapped[int | None] = mapped_column(
        ForeignKey("banks.id", ondelete="RESTRICT"), nullable=True
    )
    bank_account_number: Mapped[str | None] = mapped_column(
        String(BANK_ACCOUNT_MAX_LENGTH), nullable=True
    )
    bank_account_holder: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    """Chủ tài khoản khi khác tên nhân viên (tài khoản của người thân, tài khoản
    có dấu tiếng Việt viết khác). Rỗng nghĩa là trùng tên nhân viên."""


class EmployeeFields(BaseModel):
    """Phần riêng của nhân viên trên API (`registry.CatalogSpec`)."""

    department: str | None = Field(
        title="Phòng ban", default=None, max_length=DEPARTMENT_MAX_LENGTH
    )
    position: str | None = Field(title="Chức danh", default=None, max_length=POSITION_MAX_LENGTH)
    id_number: str | None = Field(
        title="Số CMND/CCCD", default=None, max_length=ID_NUMBER_MAX_LENGTH
    )
    tax_code: str | None = Field(
        title="Mã số thuế TNCN", default=None, max_length=TAX_CODE_MAX_LENGTH
    )
    phone: str | None = Field(title="Điện thoại", default=None, max_length=PHONE_MAX_LENGTH)
    email: str | None = Field(title="Email", default=None, max_length=EMAIL_MAX_LENGTH)

    bank_id: int | None = Field(default=None, title="Ngân hàng")
    bank_account_number: str | None = Field(
        title="Số tài khoản nhận lương", default=None, max_length=BANK_ACCOUNT_MAX_LENGTH
    )
    bank_account_holder: str | None = Field(
        title="Chủ tài khoản", default=None, max_length=NAME_MAX_LENGTH
    )

    @model_validator(mode="after")
    def _bank_account_needs_bank(self) -> Self:
        """Lặp lại `CHECK` phía DB để lỗi nói được **việc phải làm**.

        Hai lớp cho hai đường vào, cùng lối `PaymentTermFields` đã đi. Điều lớp
        này thêm được là **thông điệp**, không phải vị trí: luật liên-trường của
        Pydantic báo ở mức thân request (`loc = ["body"]`) y như `CHECK` của DB,
        nhưng nó trả về câu tiếng Việt dưới đây thay vì tên một ràng buộc mà
        người nhập liệu không tra được. `CHECK` canh mọi đường ghi khác — nhập
        Excel ở lát 3C không đi qua Pydantic của endpoint này.
        """
        if (self.bank_account_number is None) != (self.bank_id is None):
            raise ValueError(
                "Số tài khoản và ngân hàng phải khai cùng nhau — thiếu một trong hai "
                "thì không lập được ủy nhiệm chi trả lương"
            )
        return self


def employee_row_rules() -> tuple[RowRule, ...]:
    """Số tài khoản và ngân hàng phải cùng có hoặc cùng không (H3).

    Một số tài khoản không kèm ngân hàng là một số tài khoản không chuyển tiền
    được, và một ngân hàng không kèm số tài khoản là một ô không dùng vào việc
    gì. Bảng lương ở phase 9 đọc cặp này để lập ủy nhiệm chi.
    """
    return (
        RowRule(
            constraint="bank_account_needs_bank",
            field="bank_account_number",
            message="Số tài khoản ngân hàng và ngân hàng phải cùng khai, hoặc cùng để trống",
            violated=lambda row: (
                row.value("bank_account_number").is_(None) != row.value("bank_code").is_(None)
            ),
        ),
    )
