"""Định mức nguyên vật liệu của mã hàng — thao tác trên bảng con (FR-SYS-044).

Cùng khuôn `item_unit_service.py`: dịch vụ riêng, nhận `Session` của request,
không tự mở transaction; hook gộp đặt cạnh vì `item_bom_lines` mang `UNIQUE
(item_id, component_item_id)` — gộp hai mã hàng đụng nó ở cả hai cột (H64).

Hai phép kiểm mà DB không diễn đạt được, dịch vụ làm: linh kiện phải là hàng
qua kho (`INVENTORY_NATURES`, không nút nhóm — như `ItemVariantService`), và
định mức không được tạo **vòng** qua nhiều cấp (recursive CTE đi xuống từ linh
kiện; `CHECK component_differs` chỉ chặn vòng độ dài một).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    ItemBomComponentInvalidError,
    ItemBomCycleError,
    MasterDataMergeRefusedError,
    MasterDataNotFoundError,
)
from ket.kernel.master_data.models.item import INVENTORY_NATURES, ITEM_TABLE_NAME, Item
from ket.kernel.master_data.models.item_bom_line import ITEM_BOM_LINE_TABLE_NAME, ItemBomLine
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.quantity import QUANTITY_SCALE

_QUANTITY_UNIT = Decimal(1).scaleb(-QUANTITY_SCALE)


@dataclass(frozen=True)
class ExplodedComponent:
    """Một dòng linh kiện gợi ý cho phiếu lắp ráp / tháo dỡ: số lượng theo đơn
    vị **chính** của linh kiện, đã nhân với số lượng thành phẩm."""

    component_item_id: int
    unit_id: int
    quantity: Decimal
    allocation_ratio: Decimal


class ItemBomService:
    """Thêm, sửa, xóa dòng định mức của một mã hàng; nổ định mức một cấp."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return ITEM_BOM_LINE_TABLE_NAME

    def list_for(self, item_id: int) -> Sequence[ItemBomLine]:
        """Định mức của một thành phẩm, theo `id` — thứ tự người dùng khai, vì
        đó cũng là thứ tự dòng linh kiện trên phiếu sinh ra."""
        return (
            self._session.execute(
                select(ItemBomLine).where(ItemBomLine.item_id == item_id).order_by(ItemBomLine.id)
            )
            .scalars()
            .all()
        )

    def get(self, row_id: int, *, item_id: int) -> ItemBomLine:
        """Một dòng, **kèm** điều kiện nó thuộc đúng mã hàng đang mở (cùng lập
        luận `ItemUnitService.get`)."""
        row = self._session.get(ItemBomLine, row_id)
        if row is None or row.item_id != item_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy dòng định mức của mã hàng",
                entity_type=self.entity_type,
                entity_id=row_id,
                item_id=item_id,
            )
        return row

    def add(
        self,
        *,
        item_id: int,
        component_item_id: int,
        quantity: Decimal,
        allocation_ratio: Decimal,
    ) -> ItemBomLine:
        lock_bom_writes(self._session)
        self._ensure_component(item_id, component_item_id)
        row = ItemBomLine(
            item_id=item_id,
            component_item_id=component_item_id,
            quantity=quantity,
            allocation_ratio=allocation_ratio,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def update(
        self,
        row_id: int,
        *,
        item_id: int,
        expected_row_version: int,
        component_item_id: int,
        quantity: Decimal,
        allocation_ratio: Decimal,
    ) -> ItemBomLine:
        """Sửa một dòng — nhận trọn giá trị mới, kể cả đổi linh kiện (chọn nhầm
        mã phải sửa được tại dòng, không xóa-thêm để lại hai vết nhật ký)."""
        row = self.get(row_id, item_id=item_id)
        require_row_version(
            current=row.row_version, expected=expected_row_version, entity=self.entity_type
        )
        if component_item_id != row.component_item_id:
            lock_bom_writes(self._session)
            self._ensure_component(item_id, component_item_id)
        row.component_item_id = component_item_id
        row.quantity = quantity
        row.allocation_ratio = allocation_ratio
        self._session.flush()
        return row

    def delete(self, row_id: int, *, item_id: int) -> None:
        self._session.delete(self.get(row_id, item_id=item_id))
        self._session.flush()

    def explode(self, item_id: int, quantity: Decimal) -> list[ExplodedComponent]:
        """Nổ định mức **một cấp** cho `quantity` đơn vị chính thành phẩm.

        Linh kiện là thành phẩm của định mức khác không nổ tiếp: lắp nó là một
        phiếu khác (nhiều vòng = nhiều phiếu, FR-STK-005), và phiếu này chỉ xuất
        thứ có trong kho.
        """
        rows = self.list_for(item_id)
        if not rows:
            return []
        components = {
            item.id: item
            for item in self._session.execute(
                select(Item).where(Item.id.in_([row.component_item_id for row in rows]))
            ).scalars()
        }
        exploded: list[ExplodedComponent] = []
        for row in rows:
            component = components[row.component_item_id]
            if component.base_unit_id is None:  # pragma: no cover - _ensure_component chặn
                raise ItemBomComponentInvalidError(
                    "Linh kiện chưa có đơn vị tính chính",
                    entity_type=ITEM_TABLE_NAME,
                    entity_id=component.id,
                )
            exploded.append(
                ExplodedComponent(
                    component_item_id=row.component_item_id,
                    unit_id=component.base_unit_id,
                    # Làm tròn về số lẻ của cột số lượng — dòng gợi ý gửi nguyên
                    # văn lên phiếu phải qua được validator (review 8C-2 M-2).
                    quantity=(row.quantity * quantity).quantize(_QUANTITY_UNIT),
                    allocation_ratio=row.allocation_ratio,
                )
            )
        return exploded

    def _ensure_component(self, item_id: int, component_item_id: int) -> None:
        if component_item_id == item_id:
            raise ItemBomComponentInvalidError(
                "Thành phẩm không thể là linh kiện của chính nó",
                entity_type=ITEM_TABLE_NAME,
                entity_id=item_id,
            )
        for role, candidate_id in (("thành phẩm", item_id), ("linh kiện", component_item_id)):
            item = self._session.get(Item, candidate_id)
            if item is None:
                raise MasterDataNotFoundError(
                    "Không tìm thấy mã hàng", entity_type=ITEM_TABLE_NAME, entity_id=candidate_id
                )
            if item.is_group or item.nature not in INVENTORY_NATURES or item.base_unit_id is None:
                raise ItemBomComponentInvalidError(
                    f"Mã hàng {role} của định mức phải là hàng hóa / thành phẩm theo dõi tồn "
                    "kho, có đơn vị tính chính",
                    entity_type=ITEM_TABLE_NAME,
                    entity_id=candidate_id,
                )
        if reaches(self._session, start_item_id=component_item_id, target_item_id=item_id):
            raise ItemBomCycleError(
                "Linh kiện này (trực tiếp hay qua định mức của nó) lại cần chính thành phẩm — "
                "định mức không được tạo vòng",
                entity_type=ITEM_TABLE_NAME,
                entity_id=item_id,
                component_item_id=component_item_id,
            )


def reaches(session: Session, *, start_item_id: int, target_item_id: int) -> bool:
    """`True` nếu đi xuống định mức từ `start_item_id` gặp `target_item_id`.

    Recursive CTE với `UNION` (**khử trùng**) trên `item_id`: mỗi mã hàng vào
    tập đúng một lần nên câu dừng sau ≤ số mã hàng bước kể cả khi dữ liệu đã có
    vòng — `UNION ALL` + đếm độ sâu nổ theo lũy thừa bậc rẽ nhánh (review 8C-2
    M-1).
    """
    walk = (
        select(ItemBomLine.component_item_id.label("item_id"))
        .where(ItemBomLine.item_id == start_item_id)
        .cte("bom_walk", recursive=True)
    )
    step = select(ItemBomLine.component_item_id).where(ItemBomLine.item_id == walk.c.item_id)
    walk = walk.union(step)
    found = session.scalar(select(walk.c.item_id).where(walk.c.item_id == target_item_id).limit(1))
    return found is not None


def lock_bom_writes(session: Session) -> None:
    """Một khóa advisory cho mọi lượt ghi định mức của dataset — phép kiểm vòng
    là đọc-rồi-ghi, hai request cùng lúc (A cần B ‖ B cần A) đều qua nếu không
    xếp hàng; vòng lọt vào danh mục là hai phiếu lắp ráp chéo không hội tụ ở
    engine (review 8C-2 M-1). Khóa nhả cùng transaction; định mức sửa ít nên
    xếp hàng cả dataset là rẻ."""
    schema = session.scalar(text("SELECT current_schema()"))
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"item_bom:{schema}"}
    )


class ItemBomOfItemMergeHook:
    """Hợp nhất định mức khi gộp **hai mã hàng** (FR-SYS-016).

    `merge_service` chuyển cả `item_id` lẫn `component_item_id` bằng `UPDATE`
    chung, nên trước đó phải dọn ba thế đụng ràng buộc:

    1. **Nguồn là linh kiện của đích hoặc ngược lại** → từ chối cả lần gộp: sau
       chuyển đó là dòng "A cần A" (`CHECK component_differs`), và hai mã hàng
       mà một cái lắp từ cái kia không phải một bản ghi khai hai lần.
    2. **Cùng linh kiện ở cả hai định mức** → bản đích thắng, dòng nguồn bỏ
       (cùng luật `ItemUnitOfItemMergeHook`).
    3. **Cùng thành phẩm khai cả nguồn lẫn đích làm linh kiện** → giữ dòng đích.

    Sau chuyển, kiểm định mức quanh đích không tạo vòng mới (nguồn là linh kiện
    của B, B là linh kiện của đích → gộp xong đích cần B cần đích).
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        lock_bom_writes(session)
        direct = session.scalar(
            select(ItemBomLine.id).where(
                ((ItemBomLine.item_id == source_id) & (ItemBomLine.component_item_id == target_id))
                | (
                    (ItemBomLine.item_id == target_id)
                    & (ItemBomLine.component_item_id == source_id)
                )
            )
        )
        if direct is not None:
            raise MasterDataMergeRefusedError(
                "Một trong hai mã hàng là linh kiện trong định mức của mã kia nên không gộp được",
                entity_type=ITEM_TABLE_NAME,
                entity_id=source_id,
                reason="bom_self_reference",
            )
        target_components = set(
            session.scalars(
                select(ItemBomLine.component_item_id).where(ItemBomLine.item_id == target_id)
            ).all()
        )
        for row in session.scalars(select(ItemBomLine).where(ItemBomLine.item_id == source_id)):
            if row.component_item_id in target_components:
                session.delete(row)
        products_with_target = set(
            session.scalars(
                select(ItemBomLine.item_id).where(ItemBomLine.component_item_id == target_id)
            ).all()
        )
        for row in session.scalars(
            select(ItemBomLine).where(ItemBomLine.component_item_id == source_id)
        ):
            if row.item_id in products_with_target:
                session.delete(row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        if reaches(session, start_item_id=target_id, target_item_id=target_id):
            raise ItemBomCycleError(
                "Gộp hai mã hàng này làm định mức tạo vòng — tách định mức trước rồi gộp lại",
                entity_type=ITEM_TABLE_NAME,
                entity_id=target_id,
            )
