"""Mẫu số hóa đơn (`docs/srs/01` §7, `docs/srs/07` FR-EIV-003, `docs/srs/08` FR-INV-011).

Danh mục mẫu số / ký hiệu hóa đơn mà doanh nghiệp đã đăng ký với cơ quan thuế.
Dùng chung cho **cả hóa đơn giấy và hóa đơn điện tử** — FR-INV-011 nói thẳng như
vậy, và đó là lý do danh mục này nằm ở kernel chứ không trong `modules/einvoice`:
hóa đơn đặt in/tự in của SRS 08 không đi qua nhà cung cấp HĐĐT nào cả.

**`code` mang KÝ HIỆU, cột riêng `form_no` mang MẪU SỐ.** Hai thứ đi thành cặp
và cặp ấy mới là phạm vi của dãy số (BR-EIV-02), nhưng chỉ một trong hai đáng
làm mã danh mục: ký hiệu (`C26TAA`) là thứ duy nhất trong doanh nghiệp và là thứ
in trên tờ hóa đơn, còn mẫu số (`1`, `01GTKT3/001`) lặp lại trên hàng chục ký
hiệu. Đặt ký hiệu vào `code` nên hai chỉ mục duy nhất của `master_data_table_args`
canh đúng thứ cần canh mà không phải thêm ràng buộc nào.

**Ngày bắt đầu sử dụng KHÔNG ở đây** dù BR-EIV-03 cần nó. Nó thuộc *hồ sơ đăng
ký* (`invoice_registrations`, FR-EIV-002 / FR-INV-001): một ký hiệu được thông
báo phát hành nhiều lần — thêm dải số, đổi thông tin, phân bổ cho chi nhánh mới —
và mỗi lần có ngày bắt đầu của riêng nó. Chép ngày ấy lên danh mục là giữ lại
đúng một lần trong số đó, và không lần nào nói được nó là lần nào.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import CheckConstraint, SmallInteger, String, and_, or_
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args
from ket.kernel.master_data.row_rules import RowRule

INVOICE_FORM_TABLE_NAME = "invoice_forms"

FORM_NO_MAX_LENGTH = 20
"""Đủ cho mẫu số hóa đơn giấy dạng đầy đủ (`01GTKT3/001`, 11 ký tự) lẫn mẫu số
một chữ số của HĐĐT theo NĐ123/TT78."""

PROVIDER_CODE_MAX_LENGTH = 50


class InvoiceFormKind(IntEnum):
    """Hóa đơn của ký hiệu này thuộc hình thức nào.

    `IntEnum` với bộ giá trị đóng: pháp luật hiện hành cho đúng ba hình thức, và
    một hình thức thứ tư sẽ đi kèm cả một bộ quy định mới chứ không chỉ một giá
    trị mới — nên đây là chỗ đáng khai đóng, khác `kind` của chứng từ mua/bán.
    """

    DIEN_TU = 0
    """Hóa đơn điện tử — phát hành qua nhà cung cấp dịch vụ có kết nối CQT."""
    DAT_IN = 1
    """Hóa đơn đặt in — dải số in sẵn, quản lý theo SRS 08."""
    TU_IN = 2
    """Hóa đơn tự in."""


def _invoice_form_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(INVOICE_FORM_TABLE_NAME),
        CheckConstraint("kind IS NULL OR kind IN (0, 1, 2)", name="kind_is_known"),
        CheckConstraint("form_no IS NULL OR form_no <> ''", name="form_no_not_blank"),
        # Cùng cặp ràng buộc "nhóm được miễn / nhóm bị cấm" của `price_lists`:
        # chỉ có vế miễn thì nút nhóm *được phép* mang một mẫu số vô nghĩa, và
        # nó sẽ trôi vào phạm vi dãy số của một ký hiệu không tồn tại.
        CheckConstraint(
            "is_group OR (form_no IS NOT NULL AND kind IS NOT NULL)",
            name="form_set_unless_group",
        ),
        CheckConstraint(
            "NOT is_group OR (form_no IS NULL AND kind IS NULL AND provider_code IS NULL)",
            name="group_has_no_invoice_fields",
        ),
        # Nhà cung cấp dịch vụ chỉ có nghĩa với hóa đơn điện tử. Hóa đơn đặt in
        # mang mã nhà cung cấp là một dòng nói rằng nó sẽ được phát hành qua
        # cổng CQT — thứ không bao giờ xảy ra với giấy.
        CheckConstraint("provider_code IS NULL OR kind = 0", name="provider_only_for_electronic"),
    )


class InvoiceForm(MasterDataRow):
    """Một ký hiệu hóa đơn đã đăng ký, kèm mẫu số của nó."""

    __tablename__ = INVOICE_FORM_TABLE_NAME
    __table_args__ = _invoice_form_table_args()

    form_no: Mapped[str | None] = mapped_column(String(FORM_NO_MAX_LENGTH), nullable=True)
    """Mẫu số hóa đơn. `NULL` **chỉ** cho nút nhóm."""

    kind: Mapped[InvoiceFormKind | None] = mapped_column(SmallInteger, nullable=True)
    """Hình thức hóa đơn. `NULL` **chỉ** cho nút nhóm."""

    provider_code: Mapped[str | None] = mapped_column(
        String(PROVIDER_CODE_MAX_LENGTH), nullable=True
    )
    """Mã nhà cung cấp dịch vụ HĐĐT phát hành ký hiệu này (FR-EIV-001/042).

    Là **mã cấu hình**, không phải khóa ngoại: registry nhà cung cấp sống trong
    tiến trình (`modules/einvoice`, lát 7E) chứ không trong DB, cùng lối
    `POSTING_DOCUMENT_REGISTRY`. `NULL` với hóa đơn giấy, và với hóa đơn điện tử
    thì `NULL` nghĩa "dùng nhà cung cấp mặc định của bản cài".
    """


class InvoiceFormFields(BaseModel):
    """Phần riêng của mẫu số hóa đơn trên API (`registry.CatalogSpec`).

    Lặp luật của `CHECK` phía DB có chủ đích — hai lớp cho hai đường vào, cùng
    lập luận đã ghi ở `PriceListFields`.
    """

    form_no: str | None = Field(default=None, max_length=FORM_NO_MAX_LENGTH, title="Mẫu số")
    kind: InvoiceFormKind | None = Field(default=None, title="Hình thức hóa đơn")
    provider_code: str | None = Field(
        default=None, max_length=PROVIDER_CODE_MAX_LENGTH, title="Nhà cung cấp dịch vụ HĐĐT"
    )

    @model_validator(mode="after")
    def _check_group_and_provider(self) -> Self:
        """Ba luật liên-trường, nói bằng tiếng Việt thay vì tên ràng buộc DB.

        `getattr` cho `is_group` vì trường đó thuộc bộ cột **chung**: model này
        được `create_model` trộn với `MasterDataBaseCreateRequest` nên lúc chạy
        `is_group` có mặt, còn lúc kiểm kiểu tĩnh thì không.
        """
        if bool(getattr(self, "is_group", False)):
            if self.form_no is not None or self.kind is not None or self.provider_code is not None:
                raise ValueError(
                    "Nhóm mẫu số chỉ để gom cây nên không nhận mẫu số, hình thức "
                    "hay nhà cung cấp dịch vụ"
                )
            return self
        if self.form_no is None or self.kind is None:
            raise ValueError("Phải khai cả mẫu số và hình thức hóa đơn cho ký hiệu này")
        if self.provider_code is not None and self.kind is not InvoiceFormKind.DIEN_TU:
            raise ValueError(
                "Chỉ hóa đơn điện tử mới đi qua nhà cung cấp dịch vụ — hóa đơn "
                "đặt in và tự in không có nhà cung cấp"
            )
        return self


def invoice_form_row_rules() -> tuple[RowRule, ...]:
    """Luật liên-trường mà bước nhập liệu Excel phải kiểm được (H3).

    `kind_is_known` cần luật riêng cùng lý do `direction_is_known` của bảng giá:
    cột lưu bằng `SMALLINT` nên với bước kiểm nó chỉ là một số nguyên, và `7` đi
    lọt tới tận câu `INSERT`.
    """
    return (
        RowRule(
            constraint="kind_is_known",
            field="kind",
            message="Hình thức hóa đơn chỉ nhận 0 (điện tử), 1 (đặt in) hoặc 2 (tự in)",
            violated=lambda row: and_(
                row.value("kind").is_not(None),
                or_(row.value("kind") < 0, row.value("kind") > 2),
            ),
        ),
        RowRule(
            constraint="form_no_not_blank",
            field="form_no",
            message="Mẫu số không được để trắng",
            violated=lambda row: row.value("form_no") == "",
        ),
        RowRule(
            constraint="form_set_unless_group",
            field="form_no",
            message="Ký hiệu hóa đơn phải có cả mẫu số và hình thức (chỉ nút nhóm được để trống)",
            violated=lambda row: and_(
                row.flag("is_group").is_(False),
                or_(row.value("form_no").is_(None), row.value("kind").is_(None)),
            ),
        ),
        RowRule(
            constraint="group_has_no_invoice_fields",
            field="form_no",
            message="Nút nhóm không được khai mẫu số, hình thức hay nhà cung cấp dịch vụ",
            violated=lambda row: and_(
                row.flag("is_group"),
                or_(
                    row.value("form_no").is_not(None),
                    row.value("kind").is_not(None),
                    row.value("provider_code").is_not(None),
                ),
            ),
        ),
        RowRule(
            constraint="provider_only_for_electronic",
            field="provider_code",
            message="Chỉ hóa đơn điện tử mới khai được nhà cung cấp dịch vụ",
            violated=lambda row: and_(
                row.value("provider_code").is_not(None),
                or_(row.value("kind").is_(None), row.value("kind") != 0),
            ),
        ),
    )
