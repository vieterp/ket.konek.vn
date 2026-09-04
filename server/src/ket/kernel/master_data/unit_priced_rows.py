"""Luật dùng chung của hai bảng giá theo đơn vị (FR-SYS-042, FR-SAL-020).

`item_price_levels` và `price_list_lines` là hai tầng khác nhau của thứ tự nguồn
giá, nhưng chúng có **cùng hình dạng**: một mã hàng, một đơn vị cho phép `NULL`
(nghĩa "theo đơn vị chính"), và một phần khóa còn lại phân biệt các dòng cùng mã
hàng. Vì hình dạng giống nhau nên hai luật đi kèm cũng giống nhau tới từng ca
biên, và viết chúng hai lần là hai chỗ để cùng một luật lệch nhau — đúng thứ
`items_common.py` đã tránh cho hai router bảng con.

Hai luật ấy:

* **đơn vị hợp lệ** — `NULL`, hoặc một dòng `item_units` đã khai của chính mã
  hàng; đơn vị chính gửi lên tường minh thì bị từ chối vì `NULL` đã là cách viết
  của nó;
* **dọn sau khi gộp hai đơn vị tính** — hai cách viết của cùng một dòng, cộng ca
  dòng trở thành "trỏ đúng đơn vị chính" sau khi `merge_service` dời
  `items.base_unit_id`.

Luật dọn ở đây là một hàm **thuần** trả về quyết định, không phải một hàm tự đọc
tự ghi: phần khác nhau giữa hai bảng là câu truy vấn, phần giống nhau là luật.
Tách như vậy thì luật test được bằng bảng giá trị biên, và mypy đọc được kiểu của
từng bảng thay vì một `Protocol` phải đúng ở cả cấp lớp lẫn cấp thể hiện.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Mapped, Session

from ket.kernel.errors import ItemPriceUnitNotAllowedError, MasterDataNotFoundError
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME, Item
from ket.kernel.master_data.models.item_unit import ItemUnit


def ensure_unit_is_priceable(session: Session, item_id: int, unit_id: int | None) -> None:
    """`unit_id` phải là một đơn vị quy đổi **đã khai** của chính mã hàng.

    Phép kiểm mà DB không diễn đạt được: nó so một cột của bảng giá với sự **tồn
    tại của một dòng ở bảng khác**, thứ `CHECK` không làm được và khóa ngoại tới
    `units_of_measure` không hẹp tới nơi.

    `None` đi qua ngay: đó là "theo đơn vị chính", và mã hàng nào cũng có một đơn
    vị chính hoặc không có đơn vị nào — cả hai đều là một cách tính giá hợp lệ, kể
    cả với mã dịch vụ khai không kèm đơn vị (`stock_item_needs_base_unit` chỉ ràng
    hàng hóa và thành phẩm).

    Đơn vị chính gửi lên **tường minh** thì bị từ chối chứ không âm thầm quy về
    `None`: hai cách viết cho cùng một dòng là hai dòng mà hai chỉ số duy nhất
    riêng phần không thấy nhau, và im lặng sửa dữ liệu người dùng gửi là cách để
    họ không bao giờ biết mình đang khai sai chỗ.
    """
    if unit_id is None:
        return
    item = session.get(Item, item_id)
    if item is None:  # pragma: no cover - router nạp mã hàng chủ trước khi gọi
        raise MasterDataNotFoundError(
            "Không tìm thấy mã hàng", entity_type=ITEM_TABLE_NAME, entity_id=item_id
        )
    if item.base_unit_id == unit_id:
        raise ItemPriceUnitNotAllowedError(
            "Đây là đơn vị tính chính của mã hàng — hãy để trống ô đơn vị để khai giá theo nó",
            entity_type=ITEM_TABLE_NAME,
            entity_id=item_id,
            unit_id=unit_id,
        )
    declared = session.execute(
        select(ItemUnit.id).where(ItemUnit.item_id == item_id, ItemUnit.unit_id == unit_id)
    ).first()
    if declared is None:
        raise ItemPriceUnitNotAllowedError(
            "Mã hàng chưa khai đơn vị quy đổi này nên chưa khai giá theo nó được",
            entity_type=ITEM_TABLE_NAME,
            entity_id=item_id,
            unit_id=unit_id,
        )


def items_touched_by_unit_merge(
    session: Session,
    *,
    item_id_column: Mapped[int],
    unit_id_column: Mapped[int | None],
    source_id: int,
    target_id: int,
) -> dict[int, int | None]:
    """Mã hàng mà lần gộp hai đơn vị tính có thể chạm tới, kèm đơn vị chính của nó.

    Hai vế của điều kiện, và vế thứ hai là thứ dễ bỏ sót: mã hàng có dòng giá theo
    một trong hai đơn vị (hình dạng "trùng thẳng"), **hoặc** mã hàng lấy một trong
    hai làm đơn vị chính (hình dạng "trở thành đơn vị chính sau lần gộp" — xem
    `plan_unit_merge_cleanup`). Bỏ vế sau thì lần gộp để lại những dòng giá trỏ
    đúng đơn vị chính, trạng thái mà mọi đường ghi đều cấm.

    Nhận **cột** chứ không lớp model: hai bảng giá là hai lớp khác nhau, và một
    tham số `type[...]` chung sẽ buộc phải khai một `Protocol` đúng ở cả cấp lớp
    lẫn cấp thể hiện — thứ mypy không diễn đạt được cho model SQLAlchemy.

    Hai lượt đọc chứ không một phép `JOIN`, cũng vì lý do ấy: dựng `JOIN` cần
    **bảng**, mà từ một cột đơn lẻ thì đường lấy bảng ra không có kiểu tĩnh. Hai
    câu đều lọc theo khóa và trả vài dòng — đây là đường gộp danh mục, không phải
    đường nóng.
    """
    item_ids = (
        session.execute(
            select(item_id_column)
            .where(
                or_(
                    unit_id_column.in_((source_id, target_id)),
                    item_id_column.in_(
                        select(Item.id).where(Item.base_unit_id.in_((source_id, target_id)))
                    ),
                )
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    if not item_ids:
        return {}
    pairs: Sequence[tuple[int, int | None]] = (
        session.execute(select(Item.id, Item.base_unit_id).where(Item.id.in_(item_ids)))
        .tuples()
        .all()
    )
    return dict(pairs)


def plan_unit_merge_cleanup[RowT](
    rows: Sequence[RowT],
    *,
    base_unit_id: int | None,
    unit_of: Callable[[RowT], int | None],
    slot_of: Callable[[RowT], Hashable],
    source_id: int,
    target_id: int,
) -> tuple[list[RowT], list[RowT]]:
    """Quyết định phải làm gì với dòng giá của **một** phạm vi duy nhất, trước khi
    `merge_service` gộp đơn vị `source_id` vào `target_id`.

    Trả `(xóa, chuyển_về_đơn_vị_chính)`. Hàm **thuần**: không đọc, không ghi, không
    biết bảng nào — nên nó test được bằng bảng giá trị biên thay vì bằng một lần
    gộp thật, và hai bảng giá (`item_price_levels`, `price_list_lines`) dùng chung
    đúng một bản luật. Truy vấn là phần khác nhau giữa hai bảng nên nó ở lại trong
    từng hook; luật là phần giống nhau nên nó ở đây.

    `rows` phải là **mọi** dòng của phạm vi ấy, kể cả dòng để trống đơn vị: ô
    `NULL` đã có chủ là điều kiện quyết định giữa "chuyển về" và "xóa".

    Tới lượt hàm này chạy, `UnitOfMeasureMergeHook` đã đứng trước trong
    `merge_hooks` và đã chứng minh "nguồn ≡ đích" (tỷ lệ quy đổi 1) hoặc từ chối cả
    lần gộp — nên ở đây hai đơn vị là **một**, và mọi việc còn lại là dọn hai cách
    viết của cùng một dòng. Hàm này không từ chối gì.

    Hai hình dạng phải dọn, và hình dạng thứ hai là thứ dễ bỏ sót:

    1. **Trùng thẳng** — cùng một ô `slot_of` khai giá cho cả hai đơn vị. Dòng của
       đơn vị **đích** thắng, dòng nguồn bỏ đi: bản ghi được giữ lại là bản quyết
       định, cùng luật đã áp cho đơn vị quy đổi và cho cờ mặc định của tài khoản
       ngân hàng.
    2. **Trở thành đơn vị chính sau lần gộp.** `merge_service` cũng trỏ
       `items.base_unit_id` sang đơn vị đích, nên một mã hàng lấy **một trong hai**
       làm đơn vị chính sẽ thấy dòng giá của mình đột nhiên trỏ đúng đơn vị chính —
       trạng thái mà `ensure_unit_is_priceable` cấm ở mọi đường ghi. Dòng ấy
       **chuyển thành `NULL`** nếu ô `NULL` của `slot_of` đó còn trống, và chỉ bị
       **xóa** khi ô ấy đã có chủ: giá là dữ liệu người dùng khai, xóa nó khi còn
       chỗ để giữ là làm mất số mà lần gộp không hề nhắc tới.
    """
    taken_null_slots = {slot_of(row) for row in rows if unit_of(row) is None}
    # Đơn vị chính **sau** lần gộp là đích, nếu nó đang là một trong hai.
    becomes_base = base_unit_id in (source_id, target_id)
    to_delete: list[RowT] = []
    to_base_unit: list[RowT] = []
    kept: set[Hashable] = set()
    # Đích trước nguồn: khi cả hai cùng khai một ô, dòng của đơn vị đích là dòng
    # thắng, nên nó phải được xét trước.
    for row in sorted(
        (row for row in rows if unit_of(row) in (source_id, target_id)),
        key=lambda row: unit_of(row) != target_id,
    ):
        slot = slot_of(row)
        if slot in kept:
            to_delete.append(row)
            continue
        if becomes_base:
            if slot in taken_null_slots:
                to_delete.append(row)
                continue
            to_base_unit.append(row)
            taken_null_slots.add(slot)
        kept.add(slot)
    return to_delete, to_base_unit
