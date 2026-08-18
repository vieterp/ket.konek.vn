"""Mã quy cách của mã hàng — thao tác trên bảng con (FR-SYS-046).

Dịch vụ riêng, cùng lập luận đã ghi ở `bank_account_service.py` và
`item_unit_service.py`: bảng con không phải danh mục, và nới khung danh mục để nó
nhận thêm hình dạng này là bẻ cong khung dùng chung.

Dịch vụ **không** tự mở transaction — nhận `Session` của request.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    ItemVariantNotSupportedError,
    MasterDataNotFoundError,
)
from ket.kernel.master_data.models.item import INVENTORY_NATURES, ITEM_TABLE_NAME, Item
from ket.kernel.master_data.models.item_variant import ITEM_VARIANT_TABLE_NAME, ItemVariant
from ket.kernel.persistence.versioning import require_row_version


class ItemVariantService:
    """Thêm, sửa, xóa mã quy cách của một mã hàng."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return ITEM_VARIANT_TABLE_NAME

    def list_for(self, item_id: int, *, include_inactive: bool = False) -> Sequence[ItemVariant]:
        """Quy cách của một mã hàng, theo mã.

        Ẩn quy cách đã ngừng theo mặc định: đây là nguồn dựng ô chọn trên chứng từ
        nhập/xuất, và một quy cách đã ngừng hiện ở đó thì việc ngừng nó chẳng có
        tác dụng gì. Màn hình quản lý gọi kèm `include_inactive=true` — cùng hợp
        đồng đã dùng cho tài khoản ngân hàng của đối tác.
        """
        statement = (
            select(ItemVariant).where(ItemVariant.item_id == item_id).order_by(ItemVariant.code)
        )
        if not include_inactive:
            statement = statement.where(ItemVariant.is_active)
        return self._session.execute(statement).scalars().all()

    def get(self, variant_id: int, *, item_id: int) -> ItemVariant:
        """Một quy cách, **kèm** điều kiện nó thuộc đúng mã hàng đang mở."""
        variant = self._session.get(ItemVariant, variant_id)
        if variant is None or variant.item_id != item_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy mã quy cách của mã hàng",
                entity_type=self.entity_type,
                entity_id=variant_id,
                item_id=item_id,
            )
        return variant

    def add(self, *, item_id: int, code: str, name: str) -> ItemVariant:
        """Thêm một quy cách."""
        self._ensure_stock_item(item_id)
        variant = ItemVariant(item_id=item_id, code=code, name=name)
        self._session.add(variant)
        self._session.flush()
        return variant

    def update(
        self,
        variant_id: int,
        *,
        item_id: int,
        expected_row_version: int,
        code: str,
        name: str,
        is_active: bool,
    ) -> ItemVariant:
        """Sửa một quy cách, gồm cả cờ còn dùng — nhận **trọn** giá trị mới."""
        variant = self.get(variant_id, item_id=item_id)
        require_row_version(
            current=variant.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        variant.code = code
        variant.name = name
        variant.is_active = is_active
        self._session.flush()
        return variant

    def delete(self, variant_id: int, *, item_id: int) -> None:
        """Xóa hẳn một quy cách.

        Còn mở vì ở lát này chưa chứng từ nào trỏ tới quy cách. Từ phase 8, khóa
        ngoại `RESTRICT` từ dòng tồn kho sẽ tự chặn đường này, và `is_active` là
        đường đúng cho quy cách đã có phát sinh — cùng lối FR-SYS-012 chỉ ra cho
        danh mục.
        """
        self._session.delete(self.get(variant_id, item_id=item_id))
        self._session.flush()

    def _ensure_stock_item(self, item_id: int) -> None:
        """Chỉ mã hàng **theo dõi tồn kho** mới có quy cách.

        Ở tầng ứng dụng chứ không `CHECK`: phép kiểm so một dòng của bảng này với
        `nature` của **dòng khác ở bảng khác**.

        Đủ an toàn vì **không đường ghi nào** đổi được `nature` sau khi tạo: nó
        vắng mặt ở thân request sửa (H69) và không khóa ngoại nào trỏ vào nó, nên
        câu `UPDATE` chung của `merge_service` cũng không chạm tới. Vế thứ hai mới
        là vế quyết định — `base_unit_id` có H69 y hệt mà vẫn đổi được qua đường
        gộp đơn vị tính (review C1), vì có khóa ngoại trỏ vào nó.
        """
        item = self._session.get(Item, item_id)
        if item is None:  # pragma: no cover - router nạp mã hàng chủ trước khi gọi
            raise MasterDataNotFoundError(
                "Không tìm thấy mã hàng", entity_type=ITEM_TABLE_NAME, entity_id=item_id
            )
        if item.nature not in INVENTORY_NATURES:
            raise ItemVariantNotSupportedError(
                "Chỉ hàng hóa và thành phẩm mới có mã quy cách",
                entity_type=ITEM_TABLE_NAME,
                entity_id=item_id,
            )


class ItemVariantMergeHook:
    """Hợp nhất mã quy cách khi gộp hai mã hàng (FR-SYS-016).

    `UNIQUE (item_id, code)` nghĩa là hai mã hàng trùng nhau mà **cùng** khai quy
    cách `"DO"` sẽ làm câu `UPDATE` chuyển khóa ngoại đổ — và đó là ca thường gặp,
    vì hai bản ghi trùng thường là cùng một mặt hàng nên có cùng bộ màu/size.

    Luật: quy cách nào bản đích đã có (**cùng mã**) thì dòng của nguồn bị bỏ; bản
    được giữ lại là bản quyết định, cùng luật đã áp cho tỷ lệ quy đổi và cho cờ
    mặc định của tài khoản ngân hàng.

    **Nợ đã biết, thuộc phase 8**: từ khi dòng tồn kho mang `variant_id`, xóa dòng
    quy cách của nguồn sẽ phải **trỏ lại** những dòng tồn kho ấy sang quy cách
    cùng mã của bản đích trước khi xóa. Hôm nay chưa bảng nào trỏ tới
    `item_variants.id`, nên bước đó chưa có gì để làm; khóa ngoại `RESTRICT` mà
    phase 8 thêm sẽ làm lần gộp đổ thay vì âm thầm mất số tồn, tức nó không nằm
    trong loại lỗi im lặng.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        target_codes = {
            variant.code
            for variant in session.execute(
                select(ItemVariant).where(ItemVariant.item_id == target_id)
            )
            .scalars()
            .all()
        }
        for variant in (
            session.execute(select(ItemVariant).where(ItemVariant.item_id == source_id))
            .scalars()
            .all()
        ):
            if variant.code in target_codes:
                # Qua ORM để có vết trong `audit_log` (FR-NFR-012).
                session.delete(variant)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển — xem `ItemUnitOfItemMergeHook.after_move`."""
