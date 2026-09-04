"""Bảng giá theo đối tác / hợp đồng — tầng đầu của thứ tự nguồn giá (FR-SAL-020).

"Thiết lập chính sách giá: nhiều bảng giá theo nhóm khách hàng, thời gian hiệu
lực, số lượng". Ba trục ấy chia làm hai chỗ: **phạm vi và hiệu lực** ở đầu bảng
giá (tệp này), **số lượng và đơn giá** ở từng dòng (`price_list_line.py`).

Là một **danh mục** chứ không bảng riêng của phân hệ bán hàng, cùng lối `contracts`
và `payment_terms`: nó có mã người dùng đặt, có "ngừng theo dõi thay cho xóa", có
phạm vi chi nhánh, và cần nhập/xuất Excel như mọi danh mục khác. Bộ **định giá**
thì sống ở `modules/sales/pricing.py` — dữ liệu ở kernel, luật chọn ở module, đúng
ranh giới C1/C2.

**Phạm vi diễn đạt bằng ba cột đều cho phép `NULL`**, và tổ hợp `NULL` mang nghĩa
"không giới hạn theo trục đó":

* `partner_id` — một đối tác cụ thể, **hoặc một nút nhóm** trong cây đối tác.
  "Nhóm khách hàng" của FR-SAL-020 không cần cột thứ hai: danh mục đối tác vốn là
  cây (`MasterDataRow.is_group`), nên trỏ vào nút nhóm **là** cách nói "mọi khách
  trong nhóm này". Bộ định giá leo cây bằng `path` sẵn có.
* `contract_id` — bảng giá riêng của một hợp đồng.
* cả hai `NULL` — bảng giá **chung**, áp cho mọi đối tác.

**`direction` không phải cột thừa.** Danh mục đối tác gộp khách hàng và nhà cung
cấp làm một (FR-SYS-031, lát 3B-2), nên một bảng giá trỏ vào một đối tác **không**
tự nói nó là giá bán cho người ta hay giá mua của người ta. Không có cột này thì
câu trả lời phải suy từ phân hệ đang hỏi — và một quy tắc suy diễn là chỗ để hai
phân hệ suy ra hai kết quả khác nhau từ cùng một dòng.

**Không có cột "độ ưu tiên" do người dùng đặt.** Khi nhiều bảng giá cùng khớp, thứ
tự phân xử là *độ cụ thể* — hợp đồng hẹp hơn đối tác, đối tác hẹp hơn nhóm, nhóm
sâu hẹp hơn nhóm nông — và độ cụ thể đọc được từ chính dữ liệu. Một cột ưu tiên gõ
tay là con số thứ hai nói cùng chuyện, và khi hai con số lệch nhau thì không ai
biết cái nào đúng. Luật phân xử đầy đủ ghi ở `modules/sales/pricing.py`.
"""

from __future__ import annotations

from datetime import date
from typing import Self

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    SmallInteger,
    and_,
    false,
    or_,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args
from ket.kernel.master_data.models.item_price_level import PriceDirection
from ket.kernel.master_data.row_rules import RowRule

PRICE_LIST_TABLE_NAME = "price_lists"


def _price_list_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(PRICE_LIST_TABLE_NAME),
        # **Bảng giá luôn dùng chung toàn công ty** (user chốt 2026-09-04). Danh
        # mục cố ý KHÔNG bật RLS, nên lớp cô lập chi nhánh duy nhất là
        # `MasterDataService._visible_to` ở đường đọc danh mục — mà bộ định giá là
        # một đường đọc **khác**, đọc thẳng bảng này. Bản đầu tiên vì thế để một
        # bảng giá riêng chi nhánh A thắng giá của chi nhánh B (review H-1).
        #
        # Đóng bằng ràng buộc chứ không bằng một phép lọc nữa ở bộ định giá: một
        # phép lọc là thứ đường đọc **thứ ba** sẽ lại quên, còn ràng buộc làm
        # trạng thái sai **không biểu diễn được** — cùng cách 7A đã đóng ca lệch
        # chi nhánh của `SubledgerEntry`.
        CheckConstraint("branch_id IS NULL", name="always_shared_company_wide"),
        CheckConstraint("direction IS NULL OR direction IN (0, 1)", name="direction_is_known"),
        # Nút **nhóm** không có chiều giá: nó chỉ gom cây, không bao giờ được bộ
        # định giá xét tới (`_price_list_query` lọc `is_group = FALSE`). Cho nó
        # một chiều mặc định là một giá trị mang nghĩa với mọi truy vấn mà không
        # mang nghĩa với người dùng — cùng lập luận `nature_set_unless_group`.
        CheckConstraint("is_group OR direction IS NOT NULL", name="direction_set_unless_group"),
        # Và chiều ngược lại: nhóm **không được** mang thứ gì của một bảng giá
        # thật. Chỉ có ràng buộc trên là nhóm được *miễn* chứ không bị *cấm* —
        # đúng lỗ hổng review H-1 của vật tư hàng hóa đã bắt.
        CheckConstraint(
            "NOT is_group OR (direction IS NULL AND partner_id IS NULL "
            "AND contract_id IS NULL AND effective_from IS NULL AND effective_to IS NULL)",
            name="group_has_no_pricing_fields",
        ),
        # Cửa sổ hiệu lực đảo đầu đuôi là một bảng giá **không ngày nào** áp
        # được — nó đọc vẫn xuôi tai và sẽ lặng lẽ không bao giờ khớp, đúng loại
        # sai sót mà `discount_window_within_due` của điều khoản thanh toán chặn.
        CheckConstraint(
            "effective_from IS NULL OR effective_to IS NULL OR effective_to >= effective_from",
            name="effective_window_is_ordered",
        ),
        Index(f"ix_{PRICE_LIST_TABLE_NAME}_partner_id", "partner_id"),
        Index(f"ix_{PRICE_LIST_TABLE_NAME}_contract_id", "contract_id"),
    )


class PriceList(MasterDataRow):
    """Một bảng giá: phạm vi áp dụng + cửa sổ hiệu lực."""

    __tablename__ = PRICE_LIST_TABLE_NAME
    __table_args__ = _price_list_table_args()

    direction: Mapped[PriceDirection | None] = mapped_column(SmallInteger, nullable=True)
    """Giá bán hay giá mua — dùng chung enum `PriceDirection` với `item_price_levels`.

    `NULL` **chỉ** cho nút nhóm — ràng buộc `direction_set_unless_group` canh."""

    partner_id: Mapped[int | None] = mapped_column(
        ForeignKey("partners.id", ondelete="RESTRICT"), nullable=True
    )
    """Đối tác hoặc **nút nhóm đối tác** mà bảng giá này áp cho. `NULL` = mọi đối tác.

    `RESTRICT`: xóa một đối tác đang có bảng giá riêng sẽ làm bảng giá ấy đổi
    nghĩa từ "của khách A" thành "của mọi khách" — một sự nới rộng âm thầm đúng
    vào thứ người dùng cố ý thu hẹp."""

    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=True
    )
    """Hợp đồng mà bảng giá này áp cho. `NULL` = không giới hạn theo hợp đồng."""

    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Ngày bắt đầu hiệu lực; `NULL` = không giới hạn đầu dưới."""

    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Ngày kết thúc hiệu lực, **tính cả ngày này**; `NULL` = còn hiệu lực vô hạn.

    Bao gồm đầu cuối chứ không nửa mở, vì đây là ngày người dùng đọc và gõ: "áp
    tới hết 31/12" viết là `2026-12-31`, không phải `2027-01-01`."""


class PriceListFields(BaseModel):
    """Phần riêng của bảng giá trên API (`registry.CatalogSpec`).

    Lặp lại luật của `CHECK` phía DB có chủ đích — hai lớp cho hai đường vào,
    cùng lập luận đã ghi ở `PaymentTermFields`.
    """

    direction: PriceDirection | None = Field(default=None, title="Chiều giá")
    partner_id: int | None = Field(default=None, title="Đối tác hoặc nhóm đối tác")
    contract_id: int | None = Field(default=None, title="Hợp đồng")
    effective_from: date | None = Field(default=None, title="Hiệu lực từ ngày")
    effective_to: date | None = Field(default=None, title="Hiệu lực đến hết ngày")

    @model_validator(mode="after")
    def _check_group_and_window(self) -> Self:
        """Hai luật liên-trường, nói bằng tiếng Việt thay vì tên ràng buộc DB.

        Cả hai đã có `CHECK` phía DB canh và đó mới là chỗ bảo đảm — mọi đường ghi
        đi qua nó, kể cả nhập liệu từ Excel. Phần thêm được ở đây là **thông
        điệp**: `409` kèm tên một ràng buộc nội bộ không nói cho người nhập biết
        phải điền ô nào. Cùng lập luận `ItemFields._check_nature_and_unit`.

        `getattr` cho `is_group` vì trường đó thuộc bộ cột **chung**: model này
        được `create_model` trộn với `MasterDataBaseCreateRequest` nên lúc chạy
        `is_group` có mặt, còn lúc kiểm kiểu tĩnh thì không.
        """
        if getattr(self, "branch_id", None) is not None:
            raise ValueError(
                "Bảng giá luôn dùng chung toàn công ty, không khai riêng cho một chi nhánh được"
            )
        if bool(getattr(self, "is_group", False)):
            if (
                self.direction is not None
                or self.partner_id is not None
                or self.contract_id is not None
                or self.effective_from is not None
                or self.effective_to is not None
            ):
                raise ValueError(
                    "Nhóm bảng giá chỉ để gom cây nên không nhận chiều giá, đối "
                    "tác, hợp đồng hay cửa sổ hiệu lực"
                )
            return self
        if self.direction is None:
            raise ValueError("Phải chọn chiều giá cho bảng giá (giá mua hoặc giá bán)")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError(
                "Ngày hết hiệu lực không được trước ngày bắt đầu — bảng giá như "
                "vậy sẽ không ngày nào áp được"
            )
        return self


def price_list_row_rules() -> tuple[RowRule, ...]:
    """Ba luật của bảng giá (H3).

    `direction_is_known` cần một luật riêng, khác `nature_known` của vật tư hàng
    hóa: `nature` lưu bằng kiểu `Enum` của Postgres nên bước kiểm đọc được tập giá
    trị hợp lệ từ chính cột (`staging._allowed_value_errors`), còn `direction` lưu
    bằng `SMALLINT` — với bước kiểm nó chỉ là một số nguyên, và `7` đi lọt tới tận
    câu `INSERT`.

    Khóa ngoại nhắc bằng tên cột **của tệp mẫu** (`partner_code`, không phải
    `partner_id`): tệp mẫu cho người điền gõ **mã**, không gõ khóa ngoại (H79), nên
    cột `_id` không tồn tại ở bước kiểm. Cùng lối `item_row_rules` nhắc
    `base_unit_code`. `is_group` đọc bằng `row.flag`, không `row.value` — nó thuộc
    bộ cột chung.
    """
    return (
        RowRule(
            constraint="always_shared_company_wide",
            field="direction",
            message="Bảng giá luôn dùng chung toàn công ty, không khai riêng chi nhánh",
            # Tệp nhập liệu **không có** cột chi nhánh (phạm vi là lựa chọn của cả
            # lượt nhập, không của từng dòng — xem `test_import_template`), nên
            # đường này không vi phạm được. Luật khai để cổng
            # `test_every_check_constraint_is_either_a_row_rule_or_explained` thấy
            # ràng buộc đã có người canh, và để nó còn đúng nếu mai kia cột chi
            # nhánh xuất hiện trên tệp mẫu.
            violated=lambda row: false(),
        ),
        RowRule(
            constraint="direction_is_known",
            field="direction",
            message="Chiều giá chỉ nhận 0 (giá mua) hoặc 1 (giá bán)",
            violated=lambda row: and_(
                row.value("direction").is_not(None),
                or_(row.value("direction") < 0, row.value("direction") > 1),
            ),
        ),
        RowRule(
            constraint="direction_set_unless_group",
            field="direction",
            message="Bảng giá phải có chiều giá (chỉ nút nhóm được để trống)",
            violated=lambda row: and_(
                row.flag("is_group").is_(False), row.value("direction").is_(None)
            ),
        ),
        RowRule(
            constraint="group_has_no_pricing_fields",
            field="direction",
            message=("Nút nhóm không được khai chiều giá, đối tác, hợp đồng hay cửa sổ hiệu lực"),
            violated=lambda row: and_(
                row.flag("is_group"),
                or_(
                    row.value("direction").is_not(None),
                    row.value("partner_code").is_not(None),
                    row.value("contract_code").is_not(None),
                    row.value("effective_from").is_not(None),
                    row.value("effective_to").is_not(None),
                ),
            ),
        ),
        RowRule(
            constraint="effective_window_is_ordered",
            field="effective_to",
            message="Ngày hết hiệu lực không được trước ngày bắt đầu hiệu lực",
            violated=lambda row: and_(
                row.value("effective_from").is_not(None),
                row.value("effective_to").is_not(None),
                row.value("effective_to") < row.value("effective_from"),
            ),
        ),
    )
