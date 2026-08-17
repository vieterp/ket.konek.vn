"""Một dịch vụ cho **mọi** danh mục (FR-SYS-010..018, BR-SYS-01/02).

Hai mươi danh mục dùng chung một bộ luật: mã duy nhất theo phạm vi, cây có cấp,
ngừng theo dõi thay cho xóa, không xóa được khi đã có người dùng. Viết hai mươi
lần thì luật thứ tư sẽ thiếu ở danh mục thứ mười một, và không có gì báo — bảng
vẫn chạy, chỉ là một mã hàng đã lên hóa đơn biến mất khỏi báo cáo năm ngoái.

Dịch vụ **không** tự mở transaction: nó nhận `Session` đang mở của request
(`kernel/persistence/unit_of_work.py`). Đó là điều kiện để tạo một danh mục ở
giữa một luồng nhập chứng từ ("tự tạo danh mục còn thiếu", FR-NFR-062) mà vẫn
hoặc thành công cả hai hoặc không có gì.

Ngoài phạm vi lát này (thuộc 3B): registry ánh xạ `{type}` → model cho router
generic, gộp bản ghi, khung nhập/xuất Excel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, func, inspect, select, text
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    MasterDataCycleError,
    MasterDataGroupNotPostableError,
    MasterDataInUseError,
    MasterDataNotFoundError,
    MasterDataParentScopeError,
)
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.tree_path import (
    ROOT_LEVEL,
    child_path,
    is_descendant_of,
    level_of,
    like_prefix,
    move_subtree_params,
    move_subtree_sql,
)
from ket.kernel.master_data.usage import ensure_deletable
from ket.kernel.persistence.sequences import reserve_id
from ket.kernel.persistence.versioning import require_row_version


@dataclass(frozen=True)
class Page[RowT: MasterDataRow]:
    """Một trang kết quả kèm **tổng trước khi cắt trang**.

    `total` là thứ không suy ra được từ `items`: màn hình cần nó để vẽ thanh cuộn
    và để nói "1–100 trong 3.412". Trả về cùng nhau chứ không hai lời gọi, vì hai
    lời gọi ở hai transaction khác nhau cho ra một `total` không khớp trang.
    """

    items: Sequence[RowT]
    total: int


class MasterDataService[ModelT: MasterDataRow]:
    """Thao tác dùng chung trên một bảng danh mục cụ thể.

    Tham số kiểu ràng buộc vào `MasterDataRow`, nên `MasterDataService(session,
    CostObject).create(...)` trả về `CostObject` chứ không `Any` — đó là điều
    kiện để mypy strict (LD-13) còn kiểm được gì trên đoạn mã dùng chung cho hai
    mươi danh mục.
    """

    def __init__(self, session: Session, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    @property
    def entity_type(self) -> str:
        """Tên bảng — khóa dùng trong `master_data_usage` và `audit_log`."""
        return str(self._model.__tablename__)

    # ------------------------------------------------------------------ đọc

    def get(self, record_id: int) -> ModelT:
        record = self._session.get(self._model, record_id)
        if record is None:
            raise MasterDataNotFoundError(
                "Không tìm thấy bản ghi danh mục",
                entity_type=self.entity_type,
                entity_id=record_id,
            )
        return record

    def resolve_by_code(self, code: str, *, branch_id: int | None = None) -> ModelT:
        """Tra theo mã, **ưu tiên bản riêng của chi nhánh** rồi mới tới bản dùng chung.

        Hai bản cùng mã tồn tại song song là hợp lệ (FR-SYS-018, xem
        `master_data_table_args`), nên thứ tự ưu tiên phải nằm ở đúng một chỗ —
        chỗ này. Để mỗi nơi gọi tự quyết là cách hai màn hình cùng một mã cho ra
        hai bản ghi khác nhau.

        `NULLS LAST` chứ không `ORDER BY branch_id DESC`: cần "có chi nhánh
        trước, dùng chung sau", không phải "chi nhánh id lớn trước".
        """
        statement = (
            select(self._model).where(self._model.code == code).where(self._visible_to(branch_id))
        )
        if branch_id is not None:
            statement = statement.order_by(self._model.branch_id.desc().nulls_last())

        record = self._session.execute(statement.limit(1)).scalars().first()
        if record is None:
            raise MasterDataNotFoundError(
                "Không tìm thấy danh mục theo mã",
                entity_type=self.entity_type,
                code=code,
                branch_id=branch_id,
            )
        return record

    def _visible_to(self, branch_id: int | None) -> ColumnElement[bool]:
        """Điều kiện "chi nhánh này được thấy dòng nào" (FR-SYS-018).

        Dùng ở **mọi** đường đọc, không chỉ ở `resolve_by_code` (sửa sau review
        M1). Trước đó `children_of`/`subtree_of` trả về cả danh mục **riêng** của
        chi nhánh khác, trong khi `resolve_by_code` thì lọc — hai đường đọc trên
        cùng một bảng với hai phạm vi khác nhau, tức là màn hình cây và ô tra
        cứu cho hai câu trả lời khác nhau về cùng một câu hỏi.

        Bảng danh mục cố ý **không** bật RLS (`branch_id IS NULL` = dùng chung,
        mà policy chi nhánh sẽ giấu đúng những dòng đó), nên đây là lớp lọc duy
        nhất — lý do nó phải nằm ở một chỗ và được mọi đường đọc gọi.

        `branch_id=None` nghĩa là "chỉ phần dùng chung", không phải "thấy tất".
        """
        shared = self._model.branch_id.is_(None)
        if branch_id is None:
            return shared
        return shared | (self._model.branch_id == branch_id)

    def children_of(
        self,
        parent_id: int | None,
        *,
        branch_id: int | None = None,
        flag_column: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[ModelT]:
        statement = (
            select(self._model).where(self._visible_to(branch_id)).order_by(self._model.code)
        )
        statement = statement.where(
            self._model.parent_id == parent_id
            if parent_id is not None
            else self._model.parent_id.is_(None)
        )
        return self._page(self._with_flag(statement, flag_column), limit=limit, offset=offset)

    def _with_flag(
        self, statement: Select[tuple[ModelT]], flag_column: str | None
    ) -> Select[tuple[ModelT]]:
        """Lọc theo một cột boolean của danh mục (`CatalogFlag`).

        Nhận **tên cột** chứ không một biểu thức dựng sẵn: tên đến từ registry,
        và `_ensure_mapped_column` là chỗ duy nhất trong tệp này biết cột nào có
        thật. Nhận biểu thức thì mỗi nơi gọi tự dựng lấy — tức tự quyết luôn cả
        việc kiểm hay không kiểm.

        Nút **nhóm** cũng bị lọc theo cờ, có chủ đích: một nhóm chỉ chứa nhà cung
        cấp không có việc gì trên cây khách hàng, và giữ nó lại sẽ vẽ ra những
        nhánh rỗng.
        """
        if flag_column is None:
            return statement
        self._ensure_mapped_column(flag_column)
        return statement.where(inspect(self._model).columns[flag_column].is_(True))

    def subtree_of(
        self,
        record_id: int,
        *,
        branch_id: int | None = None,
        flag_column: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> Page[ModelT]:
        """Nút đó và toàn bộ con cháu, theo thứ tự cây.

        `ORDER BY path` cho ra đúng thứ tự duyệt trước (cha rồi tới con) mà giao
        diện cây cần, không phải sắp lại ở Python.
        """
        record = self.get(record_id)
        statement = (
            select(self._model)
            .where(self._model.path.like(like_prefix(record.path)))
            .where(self._visible_to(branch_id))
            .order_by(self._model.path)
        )
        return self._page(self._with_flag(statement, flag_column), limit=limit, offset=offset)

    def _page(
        self, statement: Select[tuple[ModelT]], *, limit: int | None, offset: int
    ) -> Page[ModelT]:
        """Cắt một trang và đếm tổng bằng **hai** câu lệnh.

        Không dùng `count(*) OVER ()` gộp vào cùng câu: cửa sổ đó chạy trên mọi
        dòng của kết quả **trước** khi `LIMIT` cắt, nên nó bỏ mất đúng cái lợi mà
        `LIMIT` mang lại trên danh mục vật tư mười nghìn dòng. Hai câu lệnh cùng
        chạy trong một transaction nên chúng thấy cùng một ảnh chụp dữ liệu —
        `total` không lệch với trang trả về.
        """
        total_statement: Select[tuple[int]] = select(func.count()).select_from(
            statement.order_by(None).subquery()
        )
        total = self._session.execute(total_statement).scalar_one()
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        return Page(items=self._session.execute(statement).scalars().all(), total=total)

    def count_children(self, record_id: int) -> int:
        """Đếm con **không** lọc chi nhánh, có chủ đích.

        Nó phục vụ đúng một việc: quyết định có cho xóa nút cha hay không. Một
        nhánh con thuộc chi nhánh khác vẫn là nhánh con thật, và cho xóa vì
        người thực hiện không thấy nó là cách mất dữ liệu của người khác.
        """
        statement: Select[tuple[int]] = select(func.count()).select_from(self._model)
        return self._session.execute(
            statement.where(self._model.parent_id == record_id)
        ).scalar_one()

    # ----------------------------------------------------------------- ghi

    def create(
        self,
        *,
        code: str,
        name: str,
        name_en: str | None = None,
        parent_id: int | None = None,
        is_group: bool = False,
        branch_id: int | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Thêm một bản ghi, tự dựng `path`/`level` từ cha.

        Khóa chính lấy trước từ sequence thay vì `INSERT` rồi `UPDATE` lại
        `path`: `path` chứa chính id của bản ghi, nên đường "chèn rồi sửa" tạo ra
        một bản ghi mới toanh đã mang `row_version = 2` cùng một dòng nhật ký
        "vừa tạo xong đã sửa" — hai thứ nhiễu mà kiểm toán viên phải tự loại
        suốt vòng đời sản phẩm.

        `extra` (cột riêng của danh mục — `CatalogSpec.extra_fields`) đặt **ở
        đây**, trước `flush()`, chính vì lý do trên: gọi `create` rồi `update`
        để đặt cột riêng sẽ tái lập đúng cái mà đoạn trên dựng ra để tránh, và
        chỉ cho những danh mục có cột riêng — tức là một nửa danh mục có nhật ký
        sạch còn nửa kia thì không.
        """
        parent = self.get(parent_id) if parent_id is not None else None
        self._ensure_parent_scope(parent, branch_id)

        record_id = self._reserve_id()
        record = self._model(
            id=record_id,
            code=code,
            name=name,
            name_en=name_en,
            parent_id=parent_id,
            is_group=is_group,
            branch_id=branch_id,
            path=child_path(parent.path if parent else None, record_id),
            level=(parent.level + 1) if parent else ROOT_LEVEL,
        )
        for field, value in (extra or {}).items():
            self._ensure_mapped_column(field)
            setattr(record, field, value)
        self._session.add(record)
        self._session.flush()
        return record

    def update(
        self,
        record_id: int,
        *,
        expected_row_version: int,
        code: str | None = None,
        name: str | None = None,
        name_en: str | None = None,
        is_active: bool | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> ModelT:
        """Sửa một bản ghi tại chỗ. `None` = không đụng tới trường đó.

        **Một** đường ghi cho cả phần mô tả lẫn cờ "Ngừng theo dõi", vì hai thứ
        đó nằm trên cùng một form và được lưu bằng cùng một cú bấm. Tách làm hai
        lời gọi thì lời gọi thứ hai mang `row_version` đã cũ đi một nhịp — người
        dùng nhận `409` do chính thao tác của mình gây ra.

        Đổi `code` **không** đổi `uid` (RT-19). Đó là toàn bộ lý do `uid` tồn
        tại: mã là dữ liệu người dùng sửa được, còn việc nối dữ liệu giữa các bản
        cài thì không được đứt mỗi lần ai đó chuẩn hóa lại bảng mã.

        `extra` là cột riêng của danh mục (`CatalogSpec.extra_fields`) — tên
        trường được kiểm là **cột thật đã ánh xạ** trước khi gán. Không kiểm thì
        một trường thừa trong thân request sẽ lặng lẽ thành thuộc tính Python
        trên đối tượng ORM: nó qua được cả `flush()` mà không có gì lưu, và màn
        hình hiện đúng giá trị vừa nhập cho tới lần tải lại đầu tiên.
        """
        record = self.get(record_id)
        require_row_version(
            current=record.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        if code is not None:
            record.code = code
        if name is not None:
            record.name = name
        if name_en is not None:
            record.name_en = name_en
        if is_active is not None:
            record.is_active = is_active
        for field, value in (extra or {}).items():
            self._ensure_mapped_column(field)
            setattr(record, field, value)
        self._session.flush()
        return record

    def rename(
        self,
        record_id: int,
        *,
        expected_row_version: int,
        name: str | None = None,
        name_en: str | None = None,
        code: str | None = None,
    ) -> ModelT:
        """Chỉ sửa phần mô tả — lối tắt đọc được cho đường nhập liệu và gộp bản ghi."""
        return self.update(
            record_id,
            expected_row_version=expected_row_version,
            code=code,
            name=name,
            name_en=name_en,
        )

    def set_active(self, record_id: int, *, active: bool, expected_row_version: int) -> ModelT:
        """Bật/tắt "Ngừng theo dõi" (FR-SYS-012).

        Chỉ đổi đúng nút được chọn, **không** lan xuống con cháu: nhóm "Chi phí
        bán hàng" ngừng theo dõi không có nghĩa là mọi khoản mục bên dưới cũng
        ngừng, và một thao tác lan ngầm xuống hàng trăm dòng là thao tác không
        ai hoàn tác được bằng cách bấm lại.
        """
        return self.update(record_id, expected_row_version=expected_row_version, is_active=active)

    def _ensure_mapped_column(self, field: str) -> None:
        """Tên trường phải là một cột đã ánh xạ của chính model này."""
        if field not in inspect(self._model).columns:
            raise ValueError(
                f"{self.entity_type} không có cột {field!r} — `CatalogSpec.extra_fields` "
                "đã lệch khỏi model ORM"
            )

    def move(self, record_id: int, *, new_parent_id: int | None, expected_row_version: int) -> None:
        """Chuyển một nút (và cả nhánh dưới nó) sang cha khác — **một** câu UPDATE.

        Sau khi chạy, mọi đối tượng đang nằm trong session đều bị làm hết hạn:
        câu `UPDATE` đi thẳng xuống DB nên `path`, `level` và `row_version` của
        hàng loạt dòng đã đổi mà bản sao trong bộ nhớ không hay biết. Không làm
        hết hạn thì lần ghi kế tiếp trong cùng request sẽ gửi lên một
        `row_version` cũ và nhận `409` — một xung đột do chính mình tạo ra.
        """
        record = self.get(record_id)
        require_row_version(
            current=record.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        new_parent = self.get(new_parent_id) if new_parent_id is not None else None
        self._ensure_parent_scope(new_parent, record.branch_id)

        if new_parent is not None and is_descendant_of(new_parent.path, record.path):
            raise MasterDataCycleError(
                "Không chuyển được một nhóm vào bên trong chính nhánh con của nó",
                entity_type=self.entity_type,
                entity_id=record_id,
                new_parent_id=new_parent_id,
            )

        old_path = record.path
        new_path = child_path(new_parent.path if new_parent else None, record_id)
        if new_path == old_path:
            return

        record.parent_id = new_parent_id
        self._session.flush()
        statement = move_subtree_sql(self.entity_type)
        self._session.execute(
            text(statement),
            move_subtree_params(old_path=old_path, new_path=new_path),
        )
        self._session.expire_all()

    def delete(self, record_id: int) -> None:
        """Xóa thật — chỉ khi chưa ai dùng và không còn con (BR-SYS-02).

        Kiểm con **trước** kiểm tham chiếu vì thông điệp hữu ích hơn: người dùng
        thấy ngay việc phải làm trước (chuyển nhánh con đi) thay vì đọc một câu
        về số chứng từ đang dùng rồi vẫn bị chặn ở bước sau.
        """
        record = self.get(record_id)
        if self.count_children(record_id) > 0:
            raise MasterDataInUseError(
                "Không xóa được vì bản ghi còn nhóm con — hãy chuyển hoặc xóa nhánh con trước",
                entity_type=self.entity_type,
                entity_id=record_id,
            )
        ensure_deletable(self._session, entity_type=self.entity_type, entity_id=record_id)
        self._session.delete(record)
        self._session.flush()

    # ------------------------------------------------------------- bất biến

    def ensure_postable(self, record: ModelT) -> None:
        """Chặn hạch toán vào nút nhóm hoặc nút đã ngừng theo dõi.

        Gọi từ posting engine (phase 4) và từ mọi màn hình chứng từ. Đặt ở đây
        chứ không ở mỗi nơi nhập liệu vì đây là luật của **danh mục**, và luật
        nằm cạnh dữ liệu thì phase sau không phải nhớ để chép lại.
        """
        if record.is_group:
            raise MasterDataGroupNotPostableError(
                "Đây là nút nhóm, chỉ dùng để cộng tổng — hãy chọn một mục con",
                entity_type=self.entity_type,
                entity_id=record.id,
            )
        if not record.is_active:
            raise MasterDataGroupNotPostableError(
                "Bản ghi đã ngừng theo dõi, không dùng cho chứng từ mới",
                entity_type=self.entity_type,
                entity_id=record.id,
            )

    def _ensure_parent_scope(self, parent: ModelT | None, branch_id: int | None) -> None:
        """Cha phải nhìn thấy được từ phạm vi của con.

        Cha dùng chung thì mọi chi nhánh gắn vào được; cha riêng chi nhánh A thì
        chỉ bản ghi của chi nhánh A gắn vào được. Không kiểm chỗ này thì một
        danh mục dùng chung có thể treo dưới một nhánh mà phần lớn công ty không
        được thấy — nó hiện trong danh sách phẳng nhưng biến mất trên cây.
        """
        if parent is None or parent.branch_id is None:
            return
        if parent.branch_id != branch_id:
            raise MasterDataParentScopeError(
                "Nhóm cha thuộc chi nhánh khác nên không gắn vào được",
                entity_type=self.entity_type,
                parent_branch_id=parent.branch_id,
                branch_id=branch_id,
            )

    def _reserve_id(self) -> int:
        return reserve_id(self._session, self.entity_type)


def depth_of(record: MasterDataRow) -> int:
    """Cấp thật của một bản ghi, tính lại từ `path` — dùng để kiểm chéo `level`."""
    return level_of(record.path)
