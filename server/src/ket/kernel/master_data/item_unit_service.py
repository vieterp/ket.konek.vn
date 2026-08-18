"""Đơn vị quy đổi của mã hàng — thao tác trên bảng con (FR-SYS-041).

Dịch vụ riêng chứ không nhét vào `MasterDataService`, cùng lập luận đã ghi ở
`bank_account_service.py`: bảng này không phải danh mục (không cây, không mã duy
nhất theo chi nhánh, không "ngừng theo dõi thay cho xóa").

Ở đây có **hai** luật hợp nhất, cho hai danh mục khác nhau, vì `item_units` mang
một ràng buộc duy nhất theo **cả hai** cột danh mục của nó — `UNIQUE (item_id,
unit_id)`. Gộp hai mã hàng và gộp hai đơn vị tính đều đụng nó, theo hai cách
khác nhau, nên mỗi bên có một hook riêng thay vì một hook đoán xem mình đang
được gọi cho danh mục nào. Cả hai đặt **cạnh dịch vụ của chính bảng con**, đúng
lý do H64 đưa `MergeHook` vào registry.

Dịch vụ **không** tự mở transaction — nhận `Session` của request, cùng hợp đồng
với mọi dịch vụ kernel khác.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    ItemBaseUnitMissingError,
    ItemUnitDuplicatesBaseError,
    MasterDataMergeRefusedError,
    MasterDataNotFoundError,
)
from ket.kernel.master_data.models.item import ITEM_TABLE_NAME, Item
from ket.kernel.master_data.models.item_unit import ITEM_UNIT_TABLE_NAME, ItemUnit
from ket.kernel.master_data.models.unit_of_measure import UNIT_OF_MEASURE_TABLE_NAME
from ket.kernel.persistence.versioning import require_row_version


class ItemUnitService:
    """Thêm, sửa, xóa đơn vị quy đổi của một mã hàng."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return ITEM_UNIT_TABLE_NAME

    def list_for(self, item_id: int) -> Sequence[ItemUnit]:
        """Đơn vị quy đổi của một mã hàng, tỷ lệ lớn trước.

        Tỷ lệ giảm dần chứ không theo tên đơn vị: người dùng đọc bảng này để thấy
        "thùng 24, lố 12" — thứ tự theo độ lớn là thứ tự họ nghĩ về nó, và nó
        không đổi khi ai đó sửa tên đơn vị.
        """
        return (
            self._session.execute(
                select(ItemUnit)
                .where(ItemUnit.item_id == item_id)
                .order_by(ItemUnit.factor.desc(), ItemUnit.unit_id)
            )
            .scalars()
            .all()
        )

    def get(self, row_id: int, *, item_id: int) -> ItemUnit:
        """Một dòng, **kèm** điều kiện nó thuộc đúng mã hàng đang mở.

        `item_id` bắt buộc chứ không tùy chọn, cùng lập luận đã ghi ở
        `PartnerBankAccountService.get`: đường dẫn HTTP mang cả hai id, và không
        đối chiếu chúng thì `/items/7/units/91` trả về dòng của mã hàng 12 — một
        đường đọc vòng qua mọi phép kiểm đặt ở mã hàng chủ.
        """
        row = self._session.get(ItemUnit, row_id)
        if row is None or row.item_id != item_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy đơn vị quy đổi của mã hàng",
                entity_type=self.entity_type,
                entity_id=row_id,
                item_id=item_id,
            )
        return row

    def add(self, *, item_id: int, unit_id: int, factor: Decimal) -> ItemUnit:
        """Thêm một đơn vị quy đổi."""
        self._ensure_convertible(item_id, unit_id)
        row = ItemUnit(item_id=item_id, unit_id=unit_id, factor=factor)
        self._session.add(row)
        self._session.flush()
        return row

    def update(
        self,
        row_id: int,
        *,
        item_id: int,
        expected_row_version: int,
        unit_id: int,
        factor: Decimal,
    ) -> ItemUnit:
        """Sửa một dòng — nhận **trọn** giá trị mới, không phải phần chênh lệch.

        Đổi được cả `unit_id`: một lần chọn nhầm đơn vị ("lố" thay vì "thùng")
        phải sửa được ngay tại dòng đó. Xóa rồi thêm lại cũng cho kết quả ấy
        nhưng để lại hai dòng nhật ký nói sai chuyện đã xảy ra.
        """
        row = self.get(row_id, item_id=item_id)
        require_row_version(
            current=row.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        self._ensure_convertible(item_id, unit_id)
        row.unit_id = unit_id
        row.factor = factor
        self._session.flush()
        return row

    def delete(self, row_id: int, *, item_id: int) -> None:
        """Xóa hẳn một đơn vị quy đổi.

        Xóa thật chứ không "ngừng theo dõi" — xem đầu `models/item_unit.py`:
        chứng từ chép `factor` vào dòng của nó, nên dòng ở đây không phải thứ
        chứng từ cũ trỏ tới.
        """
        self._session.delete(self.get(row_id, item_id=item_id))
        self._session.flush()

    def _ensure_convertible(self, item_id: int, unit_id: int) -> None:
        """Mã hàng phải có đơn vị chính, và đơn vị quy đổi phải **khác** nó.

        Hai phép kiểm mà DB không diễn đạt được: cả hai so một cột của bảng này
        với một cột của **dòng khác ở bảng khác**, thứ `CHECK` không làm được.

        Điều gì làm nó đủ an toàn ở tầng ứng dụng: `base_unit_id` chốt một lần ở
        đường sửa danh mục (H69) **và** đường ghi thứ hai vào cột đó — gộp hai đơn
        vị tính — chỉ chạy khi hai đơn vị đo được là bằng nhau
        (`UnitOfMeasureMergeHook`). Bản đầu tiên của lát này chỉ viện dẫn vế thứ
        nhất, và review C1 chứng minh vế ấy **một mình thì sai**: `merge_service`
        trỏ `base_unit_id` sang đơn vị đích bằng câu `UPDATE` chung, nên cột chốt
        một lần vẫn đổi giá trị được. Hai vế cộng lại mới đúng.
        """
        item = self._session.get(Item, item_id)
        if item is None:  # pragma: no cover - router nạp mã hàng chủ trước khi gọi
            raise MasterDataNotFoundError(
                "Không tìm thấy mã hàng", entity_type=ITEM_TABLE_NAME, entity_id=item_id
            )
        if item.base_unit_id is None:
            raise ItemBaseUnitMissingError(
                "Mã hàng chưa có đơn vị tính chính nên không khai được đơn vị quy đổi",
                entity_type=ITEM_TABLE_NAME,
                entity_id=item_id,
            )
        if item.base_unit_id == unit_id:
            raise ItemUnitDuplicatesBaseError(
                "Đơn vị này đang là đơn vị tính chính của mã hàng, tỷ lệ của nó luôn là 1",
                entity_type=ITEM_TABLE_NAME,
                entity_id=item_id,
                unit_id=unit_id,
            )


class ItemUnitOfItemMergeHook:
    """Hợp nhất đơn vị quy đổi khi gộp **hai mã hàng** (FR-SYS-016).

    Hai luật, theo đúng thứ tự chúng phải chạy:

    1. **Đơn vị chính phải giống nhau, nếu không thì từ chối cả lần gộp** (H71).
       Chuyển một dòng tỷ lệ sang bản ghi có đơn vị chính khác là giữ nguyên con
       số và đổi nghĩa của nó — đúng lỗi mà H69 chặn ở đường sửa, chỉ đến bằng
       đường khác. Và sâu hơn thế: hai mã hàng không cùng đơn vị chính thì số tồn
       của chúng không cộng được, nên chúng **không phải** một bản ghi bị khai hai
       lần. Từ chối ở đây là câu trả lời đúng, không phải một hạn chế kỹ thuật.
    2. **Đơn vị khai ở cả hai bên**: bản ghi được **giữ lại** là bản quyết định,
       nên tỷ lệ của nó thắng và dòng của nguồn bị bỏ. Cùng luật đã áp cho cờ mặc
       định của tài khoản ngân hàng. Hai tỷ lệ khác nhau cho cùng một đơn vị là
       dấu hiệu **một trong hai đã sai**, và người gộp đang nói bản đích là bản
       đúng.

    Không có luật nào cho "trùng tỷ lệ nhưng khác đơn vị" — đó là hai dòng hợp lệ
    và cả hai đi theo bản đích.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        source = session.get(Item, source_id)
        target = session.get(Item, target_id)
        if source is None or target is None:  # pragma: no cover - merge_records nạp trước
            raise MasterDataNotFoundError(
                "Không tìm thấy mã hàng của lần gộp", entity_type=ITEM_TABLE_NAME
            )
        if source.base_unit_id != target.base_unit_id:
            raise MasterDataMergeRefusedError(
                "Hai mã hàng có đơn vị tính chính khác nhau nên không gộp được — "
                "số tồn của chúng không cùng đơn vị đo",
                entity_type=ITEM_TABLE_NAME,
                entity_id=source_id,
                reason="base_unit_differs",
            )
        target_units = {
            row.unit_id
            for row in session.execute(select(ItemUnit).where(ItemUnit.item_id == target_id))
            .scalars()
            .all()
        }
        for row in (
            session.execute(select(ItemUnit).where(ItemUnit.item_id == source_id)).scalars().all()
        ):
            if row.unit_id in target_units:
                # Xóa qua ORM để có vết trong `audit_log` (FR-NFR-012); số dòng
                # ở đây đếm bằng số đơn vị của một mã hàng, tức vài dòng.
                session.delete(row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển: mọi bất biến đã đúng trước đó.

        Khai rỗng thay vì bỏ khỏi hợp đồng `MergeHook`: hook nào cũng có hai nửa
        thì `merge_records` không phải hỏi "hook này có nửa sau không", và một
        `Protocol` hai phương thức mà nơi cài chỉ có một là chỗ để `hasattr` mọc
        lên trong dịch vụ gộp.
        """


class UnitOfMeasureMergeHook:
    """Hợp nhất khi gộp **hai đơn vị tính** (FR-SYS-016) — chiều còn lại của `item_units`.

    Vì sao danh mục đơn vị tính cũng cần hook: `UNIQUE (item_id, unit_id)` chứa
    **cả** `unit_id`, nên gộp đơn vị "cái" vào "chiếc" sẽ đụng ràng buộc ở mọi mã
    hàng đã khai tỷ lệ cho cả hai — và đó là ca thường gặp, vì hai đơn vị trùng
    nghĩa thường được khai lẫn lộn trên cùng một mã hàng.

    **Nhưng việc chính của hook này là từ chối, không phải hợp nhất** (review C1).
    Gộp hai đơn vị tính là đường ghi **thứ hai** vào `items.base_unit_id` — câu
    `UPDATE` chung của `merge_service` trỏ cột đó sang đơn vị đích — nên nó đi
    vòng qua chính điều H69 chốt một lần để bảo vệ. Nếu hai đơn vị **không** thật
    sự bằng nhau thì mọi tỷ lệ quy đổi của mọi mã hàng lấy đơn vị nguồn làm đơn vị
    chính đổi **nghĩa** mà không đổi **số**: bản đầu tiên của lát này để lọt một
    ca sai 1000 lần trên một mã hàng mà lần gộp không hề nhắc tới.

    Cơ sở để từ chối nằm sẵn trong dữ liệu: một dòng `item_units` **là** một lời
    khẳng định về tỷ lệ giữa hai đơn vị. Ba hình dạng của lời khẳng định ấy, và cả
    ba đều nói "nguồn ≡ đích" khi và chỉ khi tỷ lệ bằng 1:

    * mã hàng khai **cả hai** đơn vị: hai tỷ lệ cùng quy về đơn vị chính của nó,
      nên chúng phải bằng nhau;
    * đơn vị chính là **đích**, mã hàng khai tỷ lệ cho **nguồn**: tỷ lệ phải là 1;
    * đơn vị chính là **nguồn**, mã hàng khai tỷ lệ cho **đích**: tỷ lệ phải là 1.

    Lệch ở bất kỳ hình dạng nào ⇒ từ chối cả lần gộp (`unit_factor_conflicts`),
    cùng khuôn H71 đã áp cho hai mã hàng khác đơn vị chính, chỉ đổi chiều. Đây là
    câu trả lời **đúng**, không phải hạn chế kỹ thuật: hai đơn vị lệch nhau thì
    gộp chúng là xóa một phép quy đổi có thật khỏi hệ thống.

    Khi mọi lời khẳng định đều nói 1:1, phần còn lại là dọn dẹp — và **cả** phần
    dọn dẹp nằm ở `before_move`, kể cả dòng chỉ trở thành vô nghĩa *sau* khi
    `base_unit_id` đổi. Đoán trước được vì tập dòng ấy xác định từ trước lần
    `UPDATE`, và làm sớm giữ cho `MergeReport.moved` khỏi đếm những dòng vừa bị
    xóa là "đã chuyển" (review L-1).
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        for item_id, base_unit_id, source_row, target_row in self._touched_items(
            session, source_id=source_id, target_id=target_id
        ):
            self._refuse_when_units_differ(
                item_id=item_id,
                base_unit_id=base_unit_id,
                source_row=source_row,
                target_row=target_row,
                source_id=source_id,
                target_id=target_id,
            )
            # Ba nhánh loại trừ lẫn nhau: `_ensure_convertible` cấm một mã hàng
            # khai tỷ lệ cho chính đơn vị chính của nó, nên đơn vị chính là đích
            # ⇒ không có `target_row`, và là nguồn ⇒ không có `source_row`.
            if source_row is not None and target_row is not None:
                # Cùng một đơn vị khai hai lần — bỏ dòng của bên bị gộp đi. Xóa
                # qua ORM để có vết trong `audit_log` (FR-NFR-012).
                session.delete(source_row)
            elif base_unit_id == target_id and source_row is not None:
                session.delete(source_row)
            elif base_unit_id == source_id and target_row is not None:
                session.delete(target_row)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có việc gì sau khi chuyển — xem `before_move`."""

    @staticmethod
    def _touched_items(
        session: Session, *, source_id: int, target_id: int
    ) -> list[tuple[int, int | None, ItemUnit | None, ItemUnit | None]]:
        """Mã hàng có dính tới một trong hai đơn vị, kèm hai dòng quy đổi của nó."""
        pairs = session.execute(
            select(ItemUnit, Item.base_unit_id)
            .join(Item, Item.id == ItemUnit.item_id)
            .where(
                or_(
                    ItemUnit.unit_id.in_((source_id, target_id)),
                    Item.base_unit_id.in_((source_id, target_id)),
                )
            )
        ).all()
        grouped: dict[int, tuple[int | None, ItemUnit | None, ItemUnit | None]] = {}
        for row, base_unit_id in pairs:
            _, source_row, target_row = grouped.get(row.item_id, (base_unit_id, None, None))
            if row.unit_id == source_id:
                source_row = row
            elif row.unit_id == target_id:
                target_row = row
            grouped[row.item_id] = (base_unit_id, source_row, target_row)
        return [
            (item_id, base_unit_id, source_row, target_row)
            for item_id, (base_unit_id, source_row, target_row) in grouped.items()
        ]

    @staticmethod
    def _refuse_when_units_differ(
        *,
        item_id: int,
        base_unit_id: int | None,
        source_row: ItemUnit | None,
        target_row: ItemUnit | None,
        source_id: int,
        target_id: int,
    ) -> None:
        """Dữ liệu đang khẳng định hai đơn vị **khác** nhau thì không gộp được."""
        measured: Decimal | None = None
        if source_row is not None and target_row is not None:
            if source_row.factor != target_row.factor:
                measured = source_row.factor / target_row.factor
        elif base_unit_id == target_id and source_row is not None and source_row.factor != 1:
            measured = source_row.factor
        elif base_unit_id == source_id and target_row is not None and target_row.factor != 1:
            measured = target_row.factor
        if measured is None:
            return
        raise MasterDataMergeRefusedError(
            "Dữ liệu đang khai hai đơn vị này quy đổi cho nhau theo tỷ lệ khác 1 nên "
            "chúng không phải một đơn vị — hãy sửa tỷ lệ quy đổi trước nếu đúng là trùng",
            entity_type=UNIT_OF_MEASURE_TABLE_NAME,
            entity_id=source_id,
            reason="unit_factor_conflicts",
            item_id=item_id,
            measured_factor=str(measured),
        )
