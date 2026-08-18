"""Vật tư hàng hóa — danh mục mà **tính chất** quyết định mọi hành vi (FR-SYS-040).

Đây là danh mục duy nhất mà một cột đơn lẻ đổi cả cách hạch toán: `nature` nói
dòng hàng này có đi qua tồn kho hay không, và có sinh doanh thu/chi phí hay không
(SRS §6.1). Bốn giá trị, hai câu hỏi độc lập — xem `ItemNature`.

**Hai bảng con** ở lát này, và chỉ hai (H65):

* `item_units` — đơn vị quy đổi (FR-SYS-041, MUST).
* `item_variants` — mã quy cách (FR-SYS-046). Là SHOULD nhưng **không hoãn được**:
  FR-SYS-046 nói "và báo cáo tồn kho", tức quy cách là một **trục khóa** của bảng
  tồn kho ở phase 8 — cùng loại rủi ro LD-09 (lô/serial) vốn không thêm sau được.

Hoãn có chủ đích, vì nơi *đọc* chúng ở phase sau (H50/H65/H72): bảng giá nhiều
mức (FR-SYS-042 → phase 7), định mức nguyên vật liệu (FR-SYS-044 → phase 9), bậc
chiết khấu (FR-SYS-045 → phase 7), đặc tính/ảnh (FR-SYS-048 COULD → v1.1), số
lượng tồn tối thiểu (FR-SYS-047 COULD → phase 8), tùy chọn "giá bán là đơn giá
sau thuế" (FR-SYS-043 → phase 7). Ba cột tài khoản ngầm định (TK kho, TK doanh
thu, TK chi phí) chờ `chart_of_accounts` của phase 5: lưu mã tài khoản dạng chuỗi
bây giờ là mở một cửa sổ dữ liệu không ràng buộc nào kiểm, và mã sai chỉ lộ ra
khi phase 5 thêm khóa ngoại — trên dữ liệu của khách hàng.

**Hai trường chỉ khai lúc tạo** (`ItemFields` so với `ItemEditableFields`, H69):

* `nature` — đổi tính chất của một mã hàng đã có tồn kho là đổi ý nghĩa của số
  tồn đang có. Hàng hóa thành dịch vụ thì số tồn không mất đi, nó chỉ thôi có
  chỗ nào hiển thị.
* `base_unit_id` — `item_units.factor` mang nghĩa "bao nhiêu đơn vị chính cho một
  đơn vị quy đổi", nên đổi đơn vị chính làm **mọi** tỷ lệ đã khai sai lặng lẽ:
  con số giữ nguyên, nghĩa của nó đổi.

Sửa sai hai trường đó = xóa bản ghi (đường xóa còn mở khi chưa ai dùng tới) hoặc
"Ngừng theo dõi" rồi khai mã mới. Đó là cái giá có chủ đích, và nó đắt hơn hẳn
một con số sai âm thầm trong sổ kho.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String, and_, or_
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.errors import ItemWarehouseNotAllowedError
from ket.kernel.master_data.base import (
    MasterDataRow,
    master_data_table_args,
)
from ket.kernel.master_data.row_rules import RowRule

ITEM_TABLE_NAME = "items"

DESCRIPTION_MAX_LENGTH = 1000
"""Mô tả tự do trên tab Thông tin chung (SRS §6.2). Có trần vì nó lên báo giá và
chứng từ kho — một mô tả dài vài trang là mô tả không in được."""


class ItemNature(StrEnum):
    """Tính chất vật tư hàng hóa — SRS §6.1, FR-SYS-040.

    Bốn giá trị trả lời **hai** câu hỏi độc lập ("có theo dõi tồn kho không" và
    "có phát sinh doanh thu/chi phí không"), nên tại sao không hai cột boolean:
    vì ba trong bốn tổ hợp có tên nghiệp vụ mà kế toán viên dùng hàng ngày, còn
    tổ hợp thứ tư (theo dõi tồn nhưng không sinh doanh thu) **không tồn tại**.
    Hai cột boolean sẽ cho khai được tổ hợp thứ tư ấy, và không màn hình nào
    biết phải làm gì với nó.

    Tách "thành phẩm" khỏi "vật tư hàng hóa" dù cả hai cùng trả lời ✔/✔: hai
    tính chất này đi vào hai tài khoản kho khác nhau (152/156 so với 155) và
    thành phẩm là **đầu ra** của tính giá thành ở phase 9, thứ hàng mua về bán
    không bao giờ là.
    """

    GOODS = "goods"
    """Vật tư, hàng hóa — theo dõi tồn kho, sinh doanh thu/chi phí."""

    FINISHED_GOODS = "finished_goods"
    """Thành phẩm — như trên, nhưng do sản xuất/lắp ráp ra (đầu ra phase 9)."""

    SERVICE = "service"
    """Dịch vụ — không tồn kho, có doanh thu/chi phí (phí vận chuyển, phí hải quan)."""

    DESCRIPTION_ONLY = "description_only"
    """Chỉ là diễn giải — không tồn kho, không doanh thu/chi phí. Dòng "Chiết
    khấu thương mại" trên hóa đơn là ví dụ của SRS."""


INVENTORY_NATURES = frozenset({ItemNature.GOODS, ItemNature.FINISHED_GOODS})
"""Tính chất **có** theo dõi tồn kho.

Là một tập hằng chứ không một cột `tracks_inventory` trên bảng: hai nguồn sự thật
cho cùng một câu trả lời là hai chỗ để chúng lệch nhau, và cột dẫn xuất sẽ lệch
đúng vào lần ai đó sửa `nature` bằng SQL. Truy vấn SQL của phase 8 hỏi
`nature IN ('goods','finished_goods')` — nút **nhóm** mang `nature IS NULL` nên
tự động không lọt vào, đúng điều cần."""

_NATURE_MAX_LENGTH = 20

_NATURE_TYPE = Enum(
    ItemNature,
    # `VARCHAR` chứ không kiểu `ENUM` native, cùng lập luận `DimensionValueSource`:
    # thêm một giá trị vào kiểu native đòi `ALTER TYPE`, lệnh không chạy được
    # trong cùng transaction với phần còn lại của migration ở nhiều bản PostgreSQL.
    native_enum=False,
    length=_NATURE_MAX_LENGTH,
    # Không sinh `CHECK` từ **kiểu** enum: mọi enum của dự án khai `False` ở đây,
    # và bật riêng một cái sẽ tạo ra một ràng buộc mà `compare_metadata` của
    # Alembic không đối chiếu được (nó không dựng lại `CHECK` sinh từ kiểu), tức
    # cổng `test_migrations_match_models` đỏ vĩnh viễn.
    #
    # Ràng buộc liệt kê giá trị vẫn có — khai **tường minh** ở `_item_table_args()`
    # cạnh ba `CHECK` khác của bảng này, nơi Alembic đối chiếu được bình thường.
    # Xem `nature_known` ở đó (H90, lát 3C-1).
    create_constraint=False,
    # Lưu **giá trị** (`"goods"`) chứ không tên thành viên (`"GOODS"`): mặc định
    # của SQLAlchemy là tên, và nó sẽ lệch với mọi câu SQL viết tay của gói cấu
    # hình phase 5 cũng như của báo cáo tồn kho phase 8.
    values_callable=lambda enum_class: [member.value for member in enum_class],
)
"""Kiểu cột cho `nature` — `Mapped[ItemNature | None]` phải đọc lại đúng thành
viên enum, không phải `str` thuần (xem `dimensions/models.py`)."""

_INVENTORY_NATURE_SQL = ", ".join(f"'{nature.value}'" for nature in sorted(INVENTORY_NATURES))


_ALL_NATURE_SQL = ", ".join(f"'{nature.value}'" for nature in ItemNature)

_INVENTORY_NATURE_VALUES: Final[tuple[str, ...]] = tuple(
    nature.value for nature in sorted(INVENTORY_NATURES)
)
"""Cùng tập với `_INVENTORY_NATURE_SQL`, ở dạng Python dùng được trong biểu thức
Core của `item_row_rules`. Cả hai đọc từ `INVENTORY_NATURES` nên không có bản
sao nào để trôi — thêm một tính chất có tồn kho chỉ phải sửa một chỗ."""


def _item_table_args() -> tuple[SchemaItem, ...]:
    return (
        *master_data_table_args(ITEM_TABLE_NAME),
        # Chỉ nhận bốn tính chất đã khai (H90, lát 3C-1 — đảo quyết định của 3B-3).
        #
        # 3B-3 cố ý bỏ ràng buộc này: "bất biến thật đã nằm ở
        # `nature_set_unless_group`, và một `CHECK` liệt kê nữa là chỗ thứ hai
        # phải sửa mỗi lần thêm một tính chất". Lập luận đó đúng khi **mọi** đường
        # ghi đi qua Pydantic, thứ vốn đã ép enum.
        #
        # Lát 3C-1 thêm một đường ghi không qua Pydantic: nhập liệu từ Excel ghi
        # bằng `INSERT ... SELECT`, nên một ô gõ sai đặt được `'khong_ton_tai'` vào
        # cột `varchar` này. Hậu quả không nằm ở dòng đó — SQLAlchemy đọc cột thành
        # `ItemNature`, nên sau đấy **mọi** lượt đọc `Item` qua ORM ném
        # `LookupError`: màn hình danh mục vật tư hỏng cho cả dữ liệu kế toán, và
        # bản ghi sai không sửa hay xóa được từ giao diện.
        #
        # Phase 5 còn thêm một đường ghi không qua Pydantic nữa (gói cấu hình chạy
        # SQL), nên lớp ở DB là lớp duy nhất bao được cả ba.
        CheckConstraint(
            f"nature IS NULL OR nature IN ({_ALL_NATURE_SQL})",
            name="nature_known",
        ),
        # Nút **nhóm** không có tính chất: nó chỉ gom cây, không bao giờ lên
        # chứng từ. Cho nó một tính chất mặc định sẽ là một giá trị mang nghĩa
        # với mọi truy vấn mà không mang nghĩa với người dùng.
        CheckConstraint("is_group OR nature IS NOT NULL", name="nature_set_unless_group"),
        # Và chiều ngược lại — nhóm **không được** mang thứ gì của một mã hàng
        # thật. Bản đầu tiên chỉ có ràng buộc trên, tức nhóm được **miễn** chứ
        # không bị **cấm**, nên một nhóm khai được `nature = 'goods'` (review
        # H-1). Hậu quả đến ở phase 8: mọi truy vấn tồn kho lọc theo
        # `nature IN (...)` sẽ cộng cả nút nhóm vào báo cáo, và không lần review
        # nào ở đó nghi ngờ danh mục. Đổi "miễn" thành "cấm" là chỗ này.
        CheckConstraint(
            "NOT is_group OR (nature IS NULL AND base_unit_id IS NULL AND warehouse_id IS NULL)",
            name="group_carries_no_item_data",
        ),
        # Hàng hóa và thành phẩm **phải** có đơn vị chính: mọi số tồn của chúng
        # được lưu theo đơn vị đó (phase 8), nên một mã hàng không có đơn vị
        # chính là một cột số tồn không có đơn vị đo.
        CheckConstraint(
            f"is_group OR nature NOT IN ({_INVENTORY_NATURE_SQL}) OR base_unit_id IS NOT NULL",
            name="stock_item_needs_base_unit",
        ),
        # Kho ngầm định chỉ có nghĩa với thứ đi qua kho. Dịch vụ mang kho ngầm
        # định sẽ điền một kho lên dòng chứng từ không phát sinh nhập xuất — một
        # giá trị không ai đọc, tức không ai sửa khi nó sai.
        CheckConstraint(
            f"warehouse_id IS NULL OR nature IN ({_INVENTORY_NATURE_SQL})",
            name="default_warehouse_needs_stock_nature",
        ),
        Index(f"ix_{ITEM_TABLE_NAME}_base_unit_id", "base_unit_id"),
        Index(f"ix_{ITEM_TABLE_NAME}_warehouse_id", "warehouse_id"),
        Index(f"ix_{ITEM_TABLE_NAME}_nature", "nature"),
    )


class Item(MasterDataRow):
    """Một mã vật tư, hàng hóa, thành phẩm, dịch vụ hoặc dòng diễn giải."""

    __tablename__ = ITEM_TABLE_NAME
    __table_args__ = _item_table_args()

    nature: Mapped[ItemNature | None] = mapped_column(_NATURE_TYPE, nullable=True)
    """`NULL` **chỉ** cho nút nhóm — ràng buộc `nature_set_unless_group` canh."""

    base_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=True
    )
    """Đơn vị chính (FR-SYS-041) — đơn vị mà **mọi** số tồn và mọi tỷ lệ quy đổi
    của mã hàng này quy về.

    Nên là đơn vị **nhỏ nhất** (chiếc, không phải thùng), đúng như FR-SYS-041 chỉ
    ra: quy đổi luôn là phép nhân từ đơn vị quy đổi về đơn vị chính, nên đơn vị
    chính nhỏ nhất thì mọi số tồn là số nguyên và không có phép chia nào để làm
    tròn. Hệ thống **không ép** điều đó — nó không biết đơn vị nào nhỏ hơn đơn vị
    nào — nên đây là hướng dẫn trên màn hình, không phải ràng buộc."""

    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )
    """Kho ngầm định điền sẵn lên chứng từ nhập/xuất (SRS §6.2 tab Ngầm định)."""

    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)
    """Mô tả tự do — in lên báo giá và phiếu kho, không tham gia phép tính nào."""


class ItemEditableFields(BaseModel):
    """Phần cột riêng **sửa được** qua `PUT` (`CatalogSpec.extra_update_fields`)."""

    warehouse_id: int | None = Field(default=None, title="Kho ngầm định")
    description: str | None = Field(
        title="Diễn giải", default=None, max_length=DESCRIPTION_MAX_LENGTH
    )


class ItemUpdateGuard:
    """Luật liên-trường của đường **sửa** vật tư (`CatalogSpec.update_guard`).

    Chỉ một luật, và nó tồn tại vì đúng một lý do: "kho ngầm định chỉ cho thứ đi
    qua kho" cần biết `nature`, mà `nature` chốt một lần lúc tạo nên nó **không**
    có trong thân request sửa. Validator của `ItemFields` vì thế chỉ chạy ở đường
    tạo, và đường sửa rơi xuống `CHECK` phía DB — trả về `ck_items_default_
    warehouse_needs_stock_nature` thay vì một câu người nhập đọc được (review H-4).

    `CHECK` vẫn là chỗ bảo đảm; đây là chỗ nói.
    """

    def check(self, record: MasterDataRow, payload: BaseModel) -> None:
        warehouse_id = getattr(payload, "warehouse_id", None)
        if warehouse_id is None or not isinstance(record, Item):
            return
        if record.nature not in INVENTORY_NATURES:
            raise ItemWarehouseNotAllowedError(
                "Dịch vụ và dòng diễn giải không nhận kho ngầm định",
                entity_type=ITEM_TABLE_NAME,
                entity_id=record.id,
            )


class ItemFields(ItemEditableFields):
    """Phần cột riêng khi **tạo mới** — thêm hai trường chốt một lần (H69).

    Là lớp con của `ItemEditableFields` chứ không hai model rời: nhờ vậy thân
    request tạo mới thừa hưởng cả trường lẫn validator của thân request sửa, nên
    không có luật nào chỉ áp cho một trong hai đường. Cổng
    `test_master_data_registry.py` khẳng định đúng quan hệ kế thừa ấy.
    """

    nature: ItemNature | None = Field(default=None, title="Tính chất")
    base_unit_id: int | None = Field(default=None, title="Đơn vị chính")

    @model_validator(mode="after")
    def _check_nature_and_unit(self) -> ItemFields:
        """Hai luật liên-trường, nói bằng tiếng Việt thay vì tên ràng buộc DB.

        Cả hai đã có `CHECK` phía DB canh (`nature_set_unless_group`,
        `stock_item_needs_base_unit`) và đó mới là chỗ bảo đảm — mọi đường ghi
        đều đi qua nó, kể cả nhập liệu và SQL của gói cấu hình. Phần thêm được ở
        đây là **thông điệp**: `409` kèm tên một ràng buộc nội bộ không nói cho
        người nhập biết phải điền ô nào (đúng nhận xét L3 của review lát 3B-2).

        `getattr` cho `is_group` vì trường đó thuộc bộ cột **chung**: model này
        được `create_model` trộn với `MasterDataBaseCreateRequest` nên lúc chạy
        `is_group` có mặt, còn lúc kiểm kiểu tĩnh thì không — nó không phải
        trường của lớp này. Mặc định `False` là mặc định của chính trường đó ở
        bộ cột chung.
        """
        is_group = bool(getattr(self, "is_group", False))
        if is_group:
            if (
                self.nature is not None
                or self.base_unit_id is not None
                or self.warehouse_id is not None
            ):
                raise ValueError(
                    "Nhóm vật tư chỉ để gom cây nên không nhận tính chất, đơn vị tính "
                    "chính hay kho ngầm định"
                )
            return self
        if self.nature is None:
            raise ValueError(
                "Phải chọn tính chất cho vật tư hàng hóa (hàng hóa, thành phẩm, "
                "dịch vụ hoặc chỉ là diễn giải)"
            )
        if self.nature in INVENTORY_NATURES and self.base_unit_id is None:
            raise ValueError("Hàng hóa và thành phẩm phải có đơn vị tính chính")
        if self.nature not in INVENTORY_NATURES and self.warehouse_id is not None:
            raise ValueError("Dịch vụ và dòng diễn giải không nhận kho ngầm định")
        return self


def item_row_rules() -> tuple[RowRule, ...]:
    """Bốn luật của vật tư hàng hóa (H3) — bộ đắt nhất trong cả registry.

    `nature_known` **không** có mặt ở đây: nó đã được `staging._allowed_value_errors`
    kiểm từ lát 3C-1 (R2-1), bằng tập giá trị đọc từ chính kiểu cột. Khai lại là
    hai câu báo lỗi cho một ô sai.

    Vì sao cả bốn đều đáng: chúng là bộ luật mà H76 dựng ra để `nature` **quyết
    định hành vi** (FR-SYS-040). Bước nhập liệu là đường ghi thứ ba vào chúng —
    sau đường tạo và đường sửa — và nó là đường duy nhất tạo mười nghìn bản ghi
    trong một lần bấm nút.
    """
    return (
        RowRule(
            constraint="nature_set_unless_group",
            field="nature",
            message="Vật tư hàng hóa phải có tính chất (chỉ nút nhóm được để trống)",
            violated=lambda row: and_(
                row.flag("is_group").is_(False), row.value("nature").is_(None)
            ),
        ),
        RowRule(
            constraint="group_carries_no_item_data",
            field="nature",
            message="Nút nhóm không được khai tính chất, đơn vị chính hay kho ngầm định",
            violated=lambda row: and_(
                row.flag("is_group"),
                or_(
                    row.value("nature").is_not(None),
                    row.value("base_unit_code").is_not(None),
                    row.value("warehouse_code").is_not(None),
                ),
            ),
        ),
        RowRule(
            constraint="stock_item_needs_base_unit",
            field="base_unit_code",
            message="Hàng hóa và thành phẩm phải có đơn vị tính chính",
            violated=lambda row: and_(
                row.flag("is_group").is_(False),
                row.value("nature").in_(_INVENTORY_NATURE_VALUES),
                row.value("base_unit_code").is_(None),
            ),
        ),
        RowRule(
            constraint="default_warehouse_needs_stock_nature",
            field="warehouse_code",
            message="Chỉ hàng hóa và thành phẩm mới nhận kho ngầm định",
            violated=lambda row: and_(
                row.value("warehouse_code").is_not(None),
                row.value("nature").notin_(_INVENTORY_NATURE_VALUES),
            ),
        ),
    )
