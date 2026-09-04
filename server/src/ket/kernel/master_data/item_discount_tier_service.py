"""Bậc chiết khấu theo số lượng của mã hàng — bảng con (FR-SYS-045).

Dịch vụ riêng chứ không nhét vào `MasterDataService`, cùng lập luận đã ghi ở
`item_unit_service.py`: bảng này không phải danh mục.

Chỉ **một** luật hợp nhất ở đây, khác `item_units` và `item_price_levels`: khóa
duy nhất của bảng là `(item_id, min_quantity)` — không có cột danh mục thứ hai —
nên chỉ lần gộp hai mã hàng đụng tới nó.

Dịch vụ **không** tự mở transaction — nhận `Session` của request, cùng hợp đồng
với mọi dịch vụ kernel khác.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    ItemDiscountTierBaseUnitMissingError,
    MasterDataNotFoundError,
)
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME, Item
from ket.kernel.master_data.models.item_discount_tier import (
    ITEM_DISCOUNT_TIER_TABLE_NAME,
    ItemDiscountTier,
)
from ket.kernel.persistence.versioning import require_row_version


class ItemDiscountTierService:
    """Thêm, sửa, xóa bậc chiết khấu của một mã hàng."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return ITEM_DISCOUNT_TIER_TABLE_NAME

    def list_for(self, item_id: int) -> Sequence[ItemDiscountTier]:
        """Bậc của một mã hàng, **ngưỡng tăng dần**.

        Tăng dần chứ không giảm dần, khác `ItemUnitService.list_for`: người dùng
        đọc bảng này như một thang ("từ 10 được 2%, từ 50 được 5%"), và đó cũng là
        thứ tự bộ chọn bậc quét qua. Một thang đọc ngược là một thang người ta
        phải đọc hai lần.

        **Không** phân trang: một mã hàng có vài bậc, không phải vài nghìn.
        """
        return (
            self._session.execute(
                select(ItemDiscountTier)
                .where(ItemDiscountTier.item_id == item_id)
                .order_by(ItemDiscountTier.min_quantity)
            )
            .scalars()
            .all()
        )

    def get(self, row_id: int, *, item_id: int) -> ItemDiscountTier:
        """Một dòng, **kèm** điều kiện nó thuộc đúng mã hàng đang mở.

        `item_id` bắt buộc — cùng lập luận đã ghi ở `ItemUnitService.get`.
        """
        row = self._session.get(ItemDiscountTier, row_id)
        if row is None or row.item_id != item_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy bậc chiết khấu của mã hàng",
                entity_type=self.entity_type,
                entity_id=row_id,
                item_id=item_id,
            )
        return row

    def add(
        self, *, item_id: int, min_quantity: Decimal, discount_percent: Decimal
    ) -> ItemDiscountTier:
        """Thêm một bậc."""
        self._ensure_measurable(item_id)
        row = ItemDiscountTier(
            item_id=item_id, min_quantity=min_quantity, discount_percent=discount_percent
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
        min_quantity: Decimal,
        discount_percent: Decimal,
    ) -> ItemDiscountTier:
        """Sửa một bậc — nhận **trọn** giá trị mới, kể cả ngưỡng.

        Đổi được `min_quantity` vì gõ nhầm ngưỡng là sai sót thường gặp nhất trên
        bảng này, và xóa rồi thêm lại để lại hai dòng nhật ký nói sai chuyện đã
        xảy ra — cùng lập luận `ItemUnitService.update`.
        """
        row = self.get(row_id, item_id=item_id)
        require_row_version(
            current=row.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        self._ensure_measurable(item_id)
        row.min_quantity = min_quantity
        row.discount_percent = discount_percent
        self._session.flush()
        return row

    def delete(self, row_id: int, *, item_id: int) -> None:
        """Xóa hẳn một bậc — chứng từ chép **tiền chiết khấu** vào dòng của nó."""
        self._session.delete(self.get(row_id, item_id=item_id))
        self._session.flush()

    def _ensure_measurable(self, item_id: int) -> None:
        """Mã hàng phải có đơn vị chính để ngưỡng số lượng quy về.

        Phép kiểm mà DB không diễn đạt được: nó so sự có mặt của một cột ở **bảng
        khác**. Cùng khuôn `ItemUnitService._ensure_convertible`, và an toàn ở tầng
        ứng dụng vì lý do y hệt: `base_unit_id` chốt một lần lúc tạo (H69) và
        đường ghi thứ hai vào nó — gộp hai đơn vị tính — chỉ chạy khi hai đơn vị
        đo được là bằng nhau (`UnitOfMeasureMergeHook`).

        Dịch vụ và dòng diễn giải vì thế không khai được bậc chiết khấu theo số
        lượng. Đó là hệ quả đúng: chiết khấu theo số lượng cần một số lượng so
        được, mà "20 giờ tư vấn" và "20 ngày tư vấn" chỉ so được khi có đơn vị
        chính để quy về. Chiết khấu cho dịch vụ đi bằng đường khác — mức giá
        (`item_price_levels`) hoặc chiết khấu gõ tay trên dòng chứng từ.
        """
        item = self._session.get(Item, item_id)
        if item is None:  # pragma: no cover - router nạp mã hàng chủ trước khi gọi
            raise MasterDataNotFoundError(
                "Không tìm thấy mã hàng", entity_type=ITEM_TABLE_NAME, entity_id=item_id
            )
        if item.base_unit_id is None:
            raise ItemDiscountTierBaseUnitMissingError(
                "Mã hàng chưa có đơn vị tính chính nên ngưỡng số lượng của bậc "
                "chiết khấu không quy về đâu được",
                entity_type=ITEM_TABLE_NAME,
                entity_id=item_id,
            )


class ItemDiscountTierMergeHook:
    """Hợp nhất bậc chiết khấu khi gộp **hai mã hàng** (FR-SYS-016).

    Một luật, cùng luật của `ItemPriceLevelOfItemMergeHook`: bậc nào của mã nguồn
    trùng `min_quantity` với mã đích thì bỏ đi, bản ghi được **giữ lại** là bản
    quyết định.

    So thẳng `min_quantity` là đúng **vì** hook của đơn vị quy đổi đứng trước hook
    này và đã từ chối cả lần gộp khi hai mã hàng khác đơn vị chính (H71): ngưỡng
    tính theo đơn vị chính, nên hai ngưỡng chỉ so được với nhau khi hai bên cùng
    một đơn vị chính. Không có phép từ chối ấy thì "10" của bên này và "10" của
    bên kia là hai con số khác nghĩa mang cùng một mặt chữ.

    Bậc **không** trùng ngưỡng thì cả hai cùng đi theo mã đích, và thang gộp lại
    vẫn là một thang hợp lệ — luật "lấy ngưỡng lớn nhất ≤ số lượng" không cần các
    bậc liên tục hay cách đều.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        target_thresholds = {row.min_quantity for row in self._rows_of(session, target_id)}
        for row in self._rows_of(session, source_id):
            if row.min_quantity in target_thresholds:
                # Xóa qua ORM để có vết trong `audit_log` (FR-NFR-012).
                session.delete(row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển: mọi bất biến đã đúng trước đó."""

    @staticmethod
    def _rows_of(session: Session, item_id: int) -> Sequence[ItemDiscountTier]:
        return (
            session.execute(select(ItemDiscountTier).where(ItemDiscountTier.item_id == item_id))
            .scalars()
            .all()
        )
