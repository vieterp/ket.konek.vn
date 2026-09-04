"""Dòng của bảng giá — thao tác trên bảng con (FR-SAL-020).

Dịch vụ riêng chứ không nhét vào `MasterDataService`, cùng lập luận đã ghi ở
`item_unit_service.py`: bảng này không phải danh mục.

**Ba** luật hợp nhất ở đây — nhiều nhất trong cả registry — vì hai chỉ số duy
nhất của bảng chứa ba cột danh mục: `price_list_id`, `item_id`, `unit_id`. Gộp
hai bảng giá, gộp hai mã hàng và gộp hai đơn vị tính đụng chúng theo ba cách, nên
mỗi bên một hook thay vì một hook đoán xem mình đang được gọi cho danh mục nào.

Luật đơn vị hợp lệ và luật dọn sau khi gộp đơn vị dùng chung với
`item_price_levels` qua `unit_priced_rows` — hai bảng cùng hình dạng thì cùng ca
biên.

Dịch vụ **không** tự mở transaction — nhận `Session` của request, cùng hợp đồng
với mọi dịch vụ kernel khác.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.master_data.models.price_list_line import (
    PRICE_LIST_LINE_TABLE_NAME,
    PriceListLine,
)
from ket.kernel.master_data.unit_priced_rows import (
    ensure_unit_is_priceable,
    items_touched_by_unit_merge,
    plan_unit_merge_cleanup,
)
from ket.kernel.persistence.versioning import require_row_version


def _slot_of(row: PriceListLine) -> tuple[Decimal]:
    """Ô phân biệt các dòng cùng (bảng giá, mã hàng): chỉ có ngưỡng số lượng."""
    return (row.min_quantity,)


def _unit_of(row: PriceListLine) -> int | None:
    return row.unit_id


class PriceListLineService:
    """Thêm, sửa, xóa dòng của một bảng giá."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return PRICE_LIST_LINE_TABLE_NAME

    def list_for(self, price_list_id: int) -> Sequence[PriceListLine]:
        """Dòng của một bảng giá — theo mã hàng, đơn vị chính trước, ngưỡng tăng dần.

        Ngưỡng **tăng** dần vì người dùng đọc mỗi mã hàng như một thang ("từ 1 giá
        này, từ 10 giá kia") — cùng lập luận `ItemDiscountTierService.list_for`.

        **Không** phân trang, khác đường đọc danh mục: một bảng giá có vài chục
        tới vài trăm dòng, và màn hình của nó là một lưới sửa tại chỗ — phân trang
        ở đó là bắt người sửa giá đi lật trang giữa hai lần gõ.
        """
        return (
            self._session.execute(
                select(PriceListLine)
                .where(PriceListLine.price_list_id == price_list_id)
                .order_by(
                    PriceListLine.item_id,
                    PriceListLine.unit_id.nulls_first(),
                    PriceListLine.min_quantity,
                )
            )
            .scalars()
            .all()
        )

    def get(self, row_id: int, *, price_list_id: int) -> PriceListLine:
        """Một dòng, **kèm** điều kiện nó thuộc đúng bảng giá đang mở.

        `price_list_id` bắt buộc — cùng lập luận đã ghi ở `ItemUnitService.get`:
        không đối chiếu hai id trên đường dẫn thì `/price_lists/7/lines/91` trả về
        dòng của bảng giá 12.
        """
        row = self._session.get(PriceListLine, row_id)
        if row is None or row.price_list_id != price_list_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy dòng của bảng giá",
                entity_type=self.entity_type,
                entity_id=row_id,
                price_list_id=price_list_id,
            )
        return row

    def add(
        self,
        *,
        price_list_id: int,
        item_id: int,
        unit_id: int | None,
        min_quantity: Decimal,
        price: Decimal,
    ) -> PriceListLine:
        """Thêm một dòng. `unit_id=None` = giá theo đơn vị chính của mã hàng."""
        ensure_unit_is_priceable(self._session, item_id, unit_id)
        row = PriceListLine(
            price_list_id=price_list_id,
            item_id=item_id,
            unit_id=unit_id,
            min_quantity=min_quantity,
            price=price,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def update(
        self,
        row_id: int,
        *,
        price_list_id: int,
        expected_row_version: int,
        item_id: int,
        unit_id: int | None,
        min_quantity: Decimal,
        price: Decimal,
    ) -> PriceListLine:
        """Sửa một dòng — nhận **trọn** giá trị mới, kể cả mã hàng.

        Đổi được `item_id`: chọn nhầm mã hàng trên một lưới vài trăm dòng là sai
        sót thường gặp, và xóa rồi thêm lại để lại hai dòng nhật ký nói sai chuyện
        đã xảy ra — cùng lập luận `ItemUnitService.update`.
        """
        row = self.get(row_id, price_list_id=price_list_id)
        require_row_version(
            current=row.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        ensure_unit_is_priceable(self._session, item_id, unit_id)
        row.item_id = item_id
        row.unit_id = unit_id
        row.min_quantity = min_quantity
        row.price = price
        self._session.flush()
        return row

    def delete(self, row_id: int, *, price_list_id: int) -> None:
        """Xóa hẳn một dòng — chứng từ chép **đơn giá** vào dòng của nó lúc lập."""
        self._session.delete(self.get(row_id, price_list_id=price_list_id))
        self._session.flush()


class PriceListLineOfPriceListMergeHook:
    """Hợp nhất dòng khi gộp **hai bảng giá** (FR-SYS-016).

    Dòng nào của bảng giá nguồn trùng `(mã hàng, đơn vị, ngưỡng)` với bảng giá đích
    thì bỏ đi — bản ghi được **giữ lại** là bản quyết định, cùng luật của hai hook
    kia. Hai đơn giá khác nhau cho cùng một câu hỏi là dấu hiệu một trong hai đã
    sai, và người gộp đang nói bản đích là bản đúng.

    Đây là hook mà không có nó thì mọi lần gộp hai bảng giá có cùng một mã hàng
    đều đổ ở `uq_price_list_lines_*` — và hai bảng giá đáng gộp thì gần như chắc
    chắn có mã hàng chung, vì đó chính là lý do chúng bị khai trùng.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        target_keys = {
            (row.item_id, row.unit_id, row.min_quantity)
            for row in _lines_of_list(session, target_id)
        }
        for row in _lines_of_list(session, source_id):
            if (row.item_id, row.unit_id, row.min_quantity) in target_keys:
                # Xóa qua ORM để có vết trong `audit_log` (FR-NFR-012).
                session.delete(row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển: mọi bất biến đã đúng trước đó."""


class PriceListLineOfItemMergeHook:
    """Hợp nhất dòng bảng giá khi gộp **hai mã hàng** (FR-SYS-016).

    Cùng luật, chiều khác: dòng của mã nguồn trùng `(bảng giá, đơn vị, ngưỡng)` với
    mã đích thì bỏ đi.

    So thẳng `min_quantity` là đúng **vì** `ItemUnitOfItemMergeHook` đứng trước
    trong `merge_hooks` của danh mục vật tư và đã từ chối cả lần gộp khi hai mã
    hàng khác đơn vị chính (H71): dòng để trống đơn vị nghĩa là "theo đơn vị
    chính", nên hai dòng `NULL` chỉ so được với nhau khi hai bên cùng một đơn vị
    chính.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        target_keys = {
            (row.price_list_id, row.unit_id, row.min_quantity)
            for row in _lines_of_item(session, target_id)
        }
        for row in _lines_of_item(session, source_id):
            if (row.price_list_id, row.unit_id, row.min_quantity) in target_keys:
                session.delete(row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển: mọi bất biến đã đúng trước đó."""


class PriceListLineOfUnitMergeHook:
    """Hợp nhất dòng bảng giá khi gộp **hai đơn vị tính** (FR-SYS-016).

    Luật dọn dùng chung với `item_price_levels` — xem
    `unit_priced_rows.plan_unit_merge_cleanup`, kể cả ca dòng trở thành "trỏ đúng
    đơn vị chính" sau khi `merge_service` dời `items.base_unit_id`.

    Khác `item_price_levels` ở **phạm vi duy nhất**: ở kia một mã hàng là một
    phạm vi, ở đây phải chia nhỏ tới từng `(bảng giá, mã hàng)` — cùng một mã hàng
    xuất hiện trong nhiều bảng giá, và ô `NULL` của bảng giá này không nói gì về ô
    `NULL` của bảng giá kia. Gộp chúng làm một sẽ xóa oan những dòng hoàn toàn hợp
    lệ. Trong mỗi phạm vi, ô phân biệt chỉ là ngưỡng số lượng.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        touched = items_touched_by_unit_merge(
            session,
            item_id_column=PriceListLine.item_id,
            unit_id_column=PriceListLine.unit_id,
            source_id=source_id,
            target_id=target_id,
        )
        for item_id, base_unit_id in touched.items():
            for rows in _lines_of_item_by_list(session, item_id).values():
                to_delete, to_base_unit = plan_unit_merge_cleanup(
                    rows,
                    base_unit_id=base_unit_id,
                    unit_of=_unit_of,
                    slot_of=_slot_of,
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


def _lines_of_list(session: Session, price_list_id: int) -> Sequence[PriceListLine]:
    return (
        session.execute(select(PriceListLine).where(PriceListLine.price_list_id == price_list_id))
        .scalars()
        .all()
    )


def _lines_of_item(session: Session, item_id: int) -> Sequence[PriceListLine]:
    return (
        session.execute(select(PriceListLine).where(PriceListLine.item_id == item_id))
        .scalars()
        .all()
    )


def _lines_of_item_by_list(session: Session, item_id: int) -> dict[int, list[PriceListLine]]:
    """Dòng của một mã hàng, gom theo bảng giá — mỗi bảng giá là một phạm vi duy nhất."""
    grouped: dict[int, list[PriceListLine]] = {}
    for row in _lines_of_item(session, item_id):
        grouped.setdefault(row.price_list_id, []).append(row)
    return grouped
