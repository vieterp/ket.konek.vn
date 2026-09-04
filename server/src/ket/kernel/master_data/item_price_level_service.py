"""Bảng giá nhiều mức của mã hàng — thao tác trên bảng con (FR-SYS-042).

Dịch vụ riêng chứ không nhét vào `MasterDataService`, cùng lập luận đã ghi ở
`item_unit_service.py`: bảng này không phải danh mục (không cây, không mã duy
nhất theo chi nhánh, không "ngừng theo dõi thay cho xóa").

Cũng như `item_units`, ở đây có **hai** luật hợp nhất cho hai danh mục khác
nhau, vì khóa duy nhất của bảng chứa **cả** `item_id` lẫn `unit_id`: gộp hai mã
hàng và gộp hai đơn vị tính đụng nó theo hai cách. Mỗi bên một hook riêng thay vì
một hook đoán xem mình đang được gọi cho danh mục nào.

Dịch vụ **không** tự mở transaction — nhận `Session` của request, cùng hợp đồng
với mọi dịch vụ kernel khác.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.master_data.models.item_price_level import (
    ITEM_PRICE_LEVEL_TABLE_NAME,
    ItemPriceLevel,
    PriceDirection,
)
from ket.kernel.master_data.unit_priced_rows import (
    ensure_unit_is_priceable,
    items_touched_by_unit_merge,
    plan_unit_merge_cleanup,
)
from ket.kernel.persistence.versioning import require_row_version


class ItemPriceLevelService:
    """Thêm, sửa, xóa mức giá của một mã hàng."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return ITEM_PRICE_LEVEL_TABLE_NAME

    def list_for(self, item_id: int) -> Sequence[ItemPriceLevel]:
        """Mọi mức giá của một mã hàng — mua trước bán, đơn vị chính trước, mức tăng dần.

        `unit_id` xếp với `NULLS FIRST` để dòng "theo đơn vị chính" luôn đứng đầu
        nhóm của nó: đó là dòng người dùng đọc trước, và là dòng bộ định giá rơi
        về khi không có gì khớp hơn.

        **Không** phân trang, cùng lập luận `ItemUnitService.list_for`: một mã
        hàng có vài mức giá, không phải vài nghìn.
        """
        return (
            self._session.execute(
                select(ItemPriceLevel)
                .where(ItemPriceLevel.item_id == item_id)
                .order_by(
                    ItemPriceLevel.direction,
                    ItemPriceLevel.unit_id.nulls_first(),
                    ItemPriceLevel.level,
                )
            )
            .scalars()
            .all()
        )

    def get(self, row_id: int, *, item_id: int) -> ItemPriceLevel:
        """Một dòng, **kèm** điều kiện nó thuộc đúng mã hàng đang mở.

        `item_id` bắt buộc chứ không tùy chọn — cùng lập luận đã ghi ở
        `ItemUnitService.get`: đường dẫn HTTP mang cả hai id, và không đối chiếu
        chúng thì `/items/7/prices/91` trả về dòng của mã hàng 12.
        """
        row = self._session.get(ItemPriceLevel, row_id)
        if row is None or row.item_id != item_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy mức giá của mã hàng",
                entity_type=self.entity_type,
                entity_id=row_id,
                item_id=item_id,
            )
        return row

    def add(
        self,
        *,
        item_id: int,
        unit_id: int | None,
        direction: PriceDirection,
        level: int,
        price: Decimal,
        label: str | None = None,
    ) -> ItemPriceLevel:
        """Thêm một mức giá. `unit_id=None` = giá theo đơn vị chính."""
        ensure_unit_is_priceable(self._session, item_id, unit_id)
        row = ItemPriceLevel(
            item_id=item_id,
            unit_id=unit_id,
            direction=direction,
            level=level,
            price=price,
            label=label,
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
        unit_id: int | None,
        direction: PriceDirection,
        level: int,
        price: Decimal,
        label: str | None = None,
    ) -> ItemPriceLevel:
        """Sửa một dòng — nhận **trọn** giá trị mới, không phải phần chênh lệch.

        Đổi được cả `unit_id`, `direction` và `level`, cùng lập luận
        `ItemUnitService.update`: một lần chọn nhầm ô phải sửa được ngay tại dòng
        đó, còn xóa rồi thêm lại để lại hai dòng nhật ký nói sai chuyện đã xảy ra.
        """
        row = self.get(row_id, item_id=item_id)
        require_row_version(
            current=row.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        ensure_unit_is_priceable(self._session, item_id, unit_id)
        row.unit_id = unit_id
        row.direction = direction
        row.level = level
        row.price = price
        row.label = label
        self._session.flush()
        return row

    def delete(self, row_id: int, *, item_id: int) -> None:
        """Xóa hẳn một mức giá.

        Xóa thật chứ không "ngừng theo dõi": chứng từ chép **đơn giá** vào dòng
        của nó lúc lập (đơn giá phải in đúng như lúc bán, kể cả sau khi bảng giá
        đổi), nên dòng ở đây không phải thứ chứng từ cũ trỏ tới — cùng lập luận
        đã ghi ở `models/item_unit.py`.
        """
        self._session.delete(self.get(row_id, item_id=item_id))
        self._session.flush()


class ItemPriceLevelOfItemMergeHook:
    """Hợp nhất mức giá khi gộp **hai mã hàng** (FR-SYS-016).

    Một luật: dòng nào của mã nguồn trùng `(đơn vị, chiều, mức)` với mã đích thì
    bỏ đi — bản ghi được **giữ lại** là bản quyết định, cùng luật đã áp cho đơn vị
    quy đổi và cho cờ mặc định của tài khoản ngân hàng. Hai giá khác nhau cho cùng
    một câu hỏi là dấu hiệu **một trong hai đã sai**, và người gộp đang nói bản
    đích là bản đúng.

    So khóa **trong Python** chứ không bằng một câu `SQL`, nên `None == None` cho
    ra "trùng" mà không phải viện tới `IS NOT DISTINCT FROM` — và đó đúng là điều
    ta muốn: `NULL` nghĩa "theo đơn vị chính", nên hai dòng `NULL` cùng `(chiều,
    mức)` **là** trùng nhau. Chúng trùng theo nghĩa đen, không phải theo quy ước:
    hook của đơn vị quy đổi (`ItemUnitOfItemMergeHook`) đứng **trước** hook này
    trong `merge_hooks` và đã từ chối cả lần gộp nếu hai mã hàng khác đơn vị chính,
    nên tới lượt đây thì "đơn vị chính" của hai bên chắc chắn là cùng một đơn vị.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        target_keys = {
            (row.unit_id, row.direction, row.level) for row in self._rows_of(session, target_id)
        }
        for row in self._rows_of(session, source_id):
            if (row.unit_id, row.direction, row.level) in target_keys:
                # Xóa qua ORM để có vết trong `audit_log` (FR-NFR-012); số dòng ở
                # đây đếm bằng số mức giá của một mã hàng, tức vài dòng.
                session.delete(row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển: mọi bất biến đã đúng trước đó.

        Khai rỗng thay vì bỏ khỏi hợp đồng `MergeHook` — cùng lập luận đã ghi ở
        `ItemUnitOfItemMergeHook.after_move`.
        """

    @staticmethod
    def _rows_of(session: Session, item_id: int) -> Sequence[ItemPriceLevel]:
        return (
            session.execute(select(ItemPriceLevel).where(ItemPriceLevel.item_id == item_id))
            .scalars()
            .all()
        )


class UnitOfMeasurePriceMergeHook:
    """Hợp nhất mức giá khi gộp **hai đơn vị tính** (FR-SYS-016).

    Chiều còn lại của `item_price_levels`, cùng lý do `UnitOfMeasureMergeHook` tồn
    tại cho `item_units`: hai chỉ số duy nhất của bảng chứa `unit_id`, nên câu
    `UPDATE` vô danh của `merge_service` đụng chúng ở mọi mã hàng đã khai giá cho
    cả hai đơn vị.

    Luật dọn viết một lần ở `unit_priced_rows.plan_unit_merge_cleanup` và dùng
    chung với `price_list_lines` — hai bảng cùng hình dạng thì cùng ca biên, và
    viết hai lần là hai chỗ để một luật lệch nhau. Ở đây chỉ còn phần khác nhau:
    câu truy vấn.

    Ô phân biệt các dòng cùng mã hàng ở đây là `(chiều, mức)`.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        touched = items_touched_by_unit_merge(
            session,
            item_id_column=ItemPriceLevel.item_id,
            unit_id_column=ItemPriceLevel.unit_id,
            source_id=source_id,
            target_id=target_id,
        )
        for item_id, base_unit_id in touched.items():
            to_delete, to_base_unit = plan_unit_merge_cleanup(
                self._rows_of(session, item_id),
                base_unit_id=base_unit_id,
                unit_of=lambda row: row.unit_id,
                slot_of=lambda row: (row.direction, row.level),
                source_id=source_id,
                target_id=target_id,
            )
            for row in to_delete:
                # Xóa và sửa qua ORM để có vết trong `audit_log` (FR-NFR-012).
                session.delete(row)
            for row in to_base_unit:
                row.unit_id = None
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển — xem `before_move`."""

    @staticmethod
    def _rows_of(session: Session, item_id: int) -> Sequence[ItemPriceLevel]:
        return (
            session.execute(select(ItemPriceLevel).where(ItemPriceLevel.item_id == item_id))
            .scalars()
            .all()
        )
