"""Gộp hai bản ghi danh mục trùng nhau (FR-SYS-016).

Dữ liệu chuyển từ phần mềm cũ sang luôn có bản trùng: cùng một nhà cung cấp
nhập hai lần, một lần "Cty TNHH ABC" một lần "CONG TY TNHH ABC". Sửa bằng tay
nghĩa là mở từng chứng từ, và số dư của bản bị bỏ thì không ai chuyển được.

**Danh sách bảng phải sửa đọc từ chính cơ sở dữ liệu**, không viết tay: tới
phase 9 có hàng chục bảng trỏ vào `partners`, và một danh sách viết tay sẽ thiếu
đúng bảng mà phase sau vừa thêm — hậu quả là gộp xong còn sót chứng từ trỏ vào
một id đã xóa, tức khóa ngoại chặn ngay, hoặc tệ hơn nếu ai đó gỡ khóa ngoại.

Không ghép chuỗi vào SQL: tên bảng và tên cột lấy từ `pg_catalog` đi vào cấu
trúc `sqlalchemy.table()/column()`, nơi phương ngữ tự trích dẫn định danh. Nhờ
vậy tệp này **không** cần một dòng miễn trừ trong
`tests/test_no_sql_string_interpolation.py` — hàng rào chính chống leo sang
dataset khác (xem docstring của tệp test đó) vẫn nguyên vẹn.

**Ba bảng tham chiếu "mềm"** — mang cặp `(entity_type, entity_id)` thay vì khóa
ngoại — không nằm trong danh sách khóa ngoại nên phải xử riêng, mỗi bảng một
chính sách khai tường minh ở `SOFT_REFERENCE_POLICY`. Bảng mềm thứ tư ra đời ở
phase sau sẽ làm đỏ `test_merge_service.py` cho tới khi có người quyết định.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy import column, delete, select, table, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.attachments.models import ATTACHMENT_TABLE_NAME, Attachment
from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AUDIT_TABLE_NAME, AuditAction
from ket.kernel.errors import MasterDataMergeRefusedError
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.registry import MergeHook
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.master_data.usage import (
    MASTER_DATA_USAGE_TABLE_NAME,
    MasterDataUsage,
    record_use,
    usage_count_of,
)
from ket.kernel.security.models import Branch
from ket.kernel.security.rls import validate_identifier


class SoftReferencePolicy(StrEnum):
    """Việc phải làm với một bảng trỏ vào danh mục bằng `(entity_type, entity_id)`."""

    MOVE_COUNTER = "move_counter"
    """Bộ đếm — cộng dồn vào đích rồi bỏ dòng nguồn."""

    REFUSE_WHEN_PRESENT = "refuse_when_present"
    """Chặn gộp khi nguồn còn dòng — người dùng phải xử lý trước."""

    KEEP_HISTORY = "keep_history"
    """Cố ý không đụng: đây là lịch sử, nó nói về bản ghi nguồn và phải tiếp tục nói."""


SOFT_REFERENCE_POLICY: Final[dict[str, SoftReferencePolicy]] = {
    MASTER_DATA_USAGE_TABLE_NAME: SoftReferencePolicy.MOVE_COUNTER,
    ATTACHMENT_TABLE_NAME: SoftReferencePolicy.REFUSE_WHEN_PRESENT,
    AUDIT_TABLE_NAME: SoftReferencePolicy.KEEP_HISTORY,
}
"""Chính sách cho từng bảng tham chiếu mềm, kèm lý do:

* `master_data_usage` — bộ đếm dẫn xuất; cộng dồn để đích giữ đúng "đang dùng ở
  bao nhiêu dòng chứng từ", và để `delete` bên dưới mở khóa cho bản ghi nguồn.
* `attachments` — **chặn** thay vì chuyển. Hai lý do, và lý do thứ nhất là bài
  học đắt nhất của lần review lát này: phép chặn chỉ trung thực khi người gộp
  nhìn thấy **mọi** dòng, vì bảng này bật RLS theo chi nhánh. Đó là lý do
  `merge_records` đòi phạm vi toàn công ty (`_ensure_company_wide_scope`) trước
  khi hỏi câu này — bản đầu tiên không đòi, và một tệp của chi nhánh khác lặng
  lẽ thành tệp mồ côi trỏ vào id đã xóa. Lý do thứ hai: ràng buộc
  `uq_attachments_live` chặn đúng lúc cả hai bản ghi đang đính **cùng một tệp**,
  tức trường hợp thường gặp nhất khi hai bản ghi là bản trùng của nhau; chuyển
  tự động ở đó nghĩa là phải tự quyết xóa một dòng đính kèm, việc mà người dùng
  phải nhìn thấy.
* `audit_log` — bất biến (FR-NFR-013, RT-02). Vết cũ nói "người dùng X sửa đối
  tác #7"; viết lại thành #12 là sửa lịch sử, và vai trò runtime cũng không có
  quyền `UPDATE` trên bảng này.
"""


@dataclass(frozen=True)
class MovedReference:
    """Một cột khóa ngoại đã được trỏ lại, kèm số dòng thật sự đổi."""

    table: str
    column: str
    rows: int


@dataclass(frozen=True)
class MergeReport:
    """Kết quả một lần gộp — đủ để màn hình nói rõ vừa có gì thay đổi."""

    entity_type: str
    source_id: int
    source_code: str
    target_id: int
    moved: tuple[MovedReference, ...]

    @property
    def total_rows(self) -> int:
        return sum(item.rows for item in self.moved)


def foreign_keys_to(session: Session, table_name: str) -> tuple[tuple[str, str], ...]:
    """Mọi `(bảng, cột)` có khóa ngoại trỏ vào `<table_name>.id` **trong schema hiện tại**.

    Hỏi `pg_catalog` chứ không `information_schema`: bản `information_schema`
    của cùng câu hỏi phải nối bốn view và chậm hơn hẳn, còn kết quả thì y hệt.
    Giới hạn theo `current_schema()` là điều bắt buộc, không phải tối ưu — mỗi
    dữ liệu kế toán là một schema (LD-15), và một truy vấn không giới hạn sẽ trả
    về bảng của doanh nghiệp khác.

    Chỉ khóa ngoại trỏ vào **một** cột `id`: danh mục không có khóa ngoại phức
    hợp nào, và một khóa hai cột trỏ vào đây sẽ là dấu hiệu của một thiết kế
    khác hẳn — im lặng bỏ qua nó thì tốt hơn là sửa nửa vời.
    """
    statement = text(
        """
        SELECT src.relname AS table_name, att.attname AS column_name
        FROM pg_constraint AS con
        JOIN pg_class AS src ON src.oid = con.conrelid
        JOIN pg_class AS tgt ON tgt.oid = con.confrelid
        JOIN pg_attribute AS att
          ON att.attrelid = con.conrelid AND att.attnum = con.conkey[1]
        JOIN pg_attribute AS tgt_att
          ON tgt_att.attrelid = con.confrelid AND tgt_att.attnum = con.confkey[1]
        WHERE con.contype = 'f'
          AND tgt.relname = :table_name
          AND tgt_att.attname = 'id'
          AND array_length(con.conkey, 1) = 1
          AND con.connamespace = current_schema()::regnamespace
        ORDER BY src.relname, att.attname
        """
    )
    rows = session.execute(statement, {"table_name": table_name}).all()
    return tuple((str(row.table_name), str(row.column_name)) for row in rows)


def merge_records[ModelT: MasterDataRow](
    session: Session,
    model: type[ModelT],
    *,
    source_id: int,
    target_id: int,
    actor_branch_ids: frozenset[int],
    hooks: Sequence[MergeHook] = (),
) -> MergeReport:
    """Chuyển mọi tham chiếu từ bản ghi nguồn sang đích, rồi xóa nguồn.

    **Gộp là thao tác toàn công ty** — đó là quyết định ngữ nghĩa của lát này,
    và `actor_branch_ids` là chỗ nó được ép. Lý do: mọi phép kiểm và mọi câu
    `UPDATE` ở đây chạy dưới RLS theo phạm vi chi nhánh của người gọi, nên người
    chỉ được gán một chi nhánh sẽ **không nhìn thấy** chứng từ và tệp đính kèm
    của chi nhánh khác đang trỏ vào bản ghi nguồn. Trước khi có phép kiểm này,
    hậu quả là một tệp đính kèm mồ côi trỏ vào id đã xóa mà không ràng buộc nào
    nổ (review lát này, C1).

    Thứ tự các bước không đổi được: bộ đếm phải chuyển **trước** khi xóa nguồn,
    vì chính bộ đếm là thứ `delete` hỏi để quyết định có cho xóa hay không. Và
    xóa nguồn phải là bước cuối — mọi phép kiểm phía trên còn cần đọc nó.

    Cả hàm chạy trong transaction của người gọi (một request = một transaction,
    FR-NFR-003). Bất kỳ bước nào ném thì không có gì được ghi: không có trạng
    thái "gộp được một nửa", thứ mà thao tác này tuyệt đối không được để lại.
    """
    service = MasterDataService(session, model)
    entity_type = service.entity_type
    validate_identifier(entity_type)

    if source_id == target_id:
        raise MasterDataMergeRefusedError(
            "Bản ghi nguồn và bản ghi đích là một",
            entity_type=entity_type,
            entity_id=source_id,
            reason="same_record",
        )

    _ensure_company_wide_scope(session, entity_type, actor_branch_ids=actor_branch_ids)

    source = service.get(source_id)
    target = service.get(target_id)

    _ensure_scope_covers(entity_type, source=source, target=target)
    _ensure_target_is_not_a_group(entity_type, target=target)
    _ensure_no_children(service, entity_type, source_id=source_id)
    _ensure_no_attachments(session, entity_type, source_id=source_id)

    # Đọc trước khi chuyển: `_move_foreign_keys` làm hết hạn mọi đối tượng đang
    # nằm trong session, nên mọi lần đọc sau đó là một vòng truy vấn nữa.
    source_code = source.code
    target_branch_id = target.branch_id

    for hook in hooks:
        hook.before_move(session, source_id=source_id, target_id=target_id)

    moved = _move_foreign_keys(session, entity_type, source_id=source_id, target_id=target_id)
    _move_usage_counter(session, entity_type, source_id=source_id, target_id=target_id)

    for hook in hooks:
        hook.after_move(session, target_id=target_id)

    service.delete(source_id)

    record_action(
        session,
        entity_type=entity_type,
        entity_id=str(target_id),
        action=AuditAction.UPDATED,
        branch_id=target_branch_id,
        new_values={
            "merged_from_id": source_id,
            "merged_from_code": source_code,
            "moved_rows": sum(item.rows for item in moved),
            "moved_columns": ", ".join(f"{item.table}.{item.column}" for item in moved if item.rows)
            or None,
        },
    )
    return MergeReport(
        entity_type=entity_type,
        source_id=source_id,
        source_code=source_code,
        target_id=target_id,
        moved=moved,
    )


def _ensure_company_wide_scope(
    session: Session, entity_type: str, *, actor_branch_ids: frozenset[int]
) -> None:
    """Người gộp phải được gán **mọi** chi nhánh của dữ liệu kế toán.

    Thô hơn một phép kiểm tinh tế, và trung thực hơn: mọi câu lệnh của lần gộp
    chạy dưới RLS, nên phạm vi của người gọi **chính là** phần dữ liệu mà lần
    gộp nhìn thấy. Người chỉ được gán chi nhánh A gộp một đối tác đang có chứng
    từ ở chi nhánh B sẽ hoặc bỏ sót (bảng tham chiếu mềm — không ràng buộc nào
    nổ), hoặc đổ giữa chừng với một lỗi khóa ngoại thô. Cả hai đều tệ hơn một
    câu từ chối nêu đúng việc phải làm.

    Đọc `branches` được vì bảng đó **không** bật RLS (H39, migration `0001`): nó
    là danh mục chi nhánh, không phải dữ liệu phát sinh.

    Hệ quả vận hành có chủ đích: gộp là việc của người có phạm vi toàn công ty
    (kế toán trưởng, quản trị), không phải của kế toán viên một chi nhánh — đúng
    với mức rủi ro của một thao tác không hoàn tác được.
    """
    all_branches = set(session.execute(select(Branch.id)).scalars().all())
    outside = all_branches - actor_branch_ids
    if not outside:
        return
    raise MasterDataMergeRefusedError(
        "Gộp bản ghi là thao tác toàn công ty — người thực hiện phải được gán mọi chi nhánh",
        entity_type=entity_type,
        reason="scope_incomplete",
        missing_branches=len(outside),
    )


def _ensure_scope_covers(entity_type: str, *, source: MasterDataRow, target: MasterDataRow) -> None:
    """Đích phải nhìn thấy được từ mọi nơi nguồn đang được dùng (FR-SYS-018).

    Nguồn dùng chung toàn công ty (`branch_id IS NULL`) mà đích chỉ thuộc chi
    nhánh A: chứng từ của chi nhánh B đang trỏ vào nguồn sẽ trỏ sang một bản ghi
    mà chính B không được thấy — dữ liệu vẫn đúng dưới DB nhưng biến mất khỏi
    màn hình, kiểu hỏng khó lần nhất.

    Chiều ngược lại thì cho phép: gộp bản riêng của chi nhánh A **vào** bản dùng
    chung chỉ mở rộng tầm nhìn.
    """
    if target.branch_id is None or target.branch_id == source.branch_id:
        return
    raise MasterDataMergeRefusedError(
        "Bản ghi đích thuộc phạm vi hẹp hơn bản ghi nguồn nên không nhận được "
        "các tham chiếu đang có",
        entity_type=entity_type,
        entity_id=source.id,
        reason="target_scope_narrower",
    )


def _ensure_target_is_not_a_group(entity_type: str, *, target: MasterDataRow) -> None:
    """Không gộp vào một nút **nhóm** (review H-2).

    Nhóm chỉ để gom cây; nó không lên chứng từ và không nhận giá trị nào
    (`ensure_catalog_choice` → `master_data.group_not_postable`). Chuyển tham
    chiếu **vào** nó tạo ra đúng trạng thái mà mọi đường ghi khác đã từ chối, và
    lần này thì không có đường sửa lại:

    * bảng con đi theo bản ghi nguồn (tài khoản ngân hàng của đối tác, đơn vị quy
      đổi và mã quy cách của vật tư) rơi vào một bản ghi mà router bảng con trả
      `404` — chúng không đọc, không sửa, không xóa được bằng endpoint nào, chỉ
      chết theo `CASCADE` khi ai đó xóa nhóm;
    * khóa ngoại trỏ tới danh mục (`items.base_unit_id`, `items.warehouse_id`,
      `partners.payment_term_id`, `employees.bank_id`) cắm vào một nút nhóm — và
      những cột chốt một lần như `base_unit_id` thì không sửa lại được.

    Nguồn là nhóm thì không cần chặn riêng: `_ensure_no_children` đã loại nhóm có
    con, còn một nhóm rỗng thì không có tham chiếu nào để chuyển.
    """
    if not target.is_group:
        return
    raise MasterDataMergeRefusedError(
        "Bản ghi đích là một nhóm — hãy chọn một mục con làm bản ghi giữ lại",
        entity_type=entity_type,
        entity_id=target.id,
        reason="target_is_group",
    )


def _ensure_no_children[ModelT: MasterDataRow](
    service: MasterDataService[ModelT], entity_type: str, *, source_id: int
) -> None:
    """Nguồn còn nhánh con thì chặn — cùng luật với xóa (BR-SYS-02).

    Gộp không kéo theo nhánh con: "gộp hai nhà cung cấp trùng" và "chuyển cả một
    nhóm sang nhánh khác" là hai ý định khác nhau, và đoán nhầm ý định thứ hai
    sẽ dời hàng chục bản ghi bằng một thao tác không hoàn tác được. Đường chuyển
    nhánh đã có sẵn (`PUT .../parent`).
    """
    if service.count_children(source_id) == 0:
        return
    raise MasterDataMergeRefusedError(
        "Bản ghi nguồn còn nhóm con — hãy chuyển nhánh con sang nơi khác trước khi gộp",
        entity_type=entity_type,
        entity_id=source_id,
        reason="source_has_children",
    )


def _ensure_no_attachments(session: Session, entity_type: str, *, source_id: int) -> None:
    """Nguồn còn tệp đang đính thì chặn — xem `SOFT_REFERENCE_POLICY`."""
    attached = session.execute(
        select(Attachment.id)
        .where(Attachment.entity_type == entity_type)
        .where(Attachment.entity_id == str(source_id))
        .where(Attachment.detached_at.is_(None))
        .limit(1)
    ).first()
    if attached is None:
        return
    raise MasterDataMergeRefusedError(
        "Bản ghi nguồn còn tệp đính kèm — hãy gỡ hoặc đính lại sang bản ghi đích trước khi gộp",
        entity_type=entity_type,
        entity_id=source_id,
        reason="source_has_attachments",
    )


def _move_foreign_keys(
    session: Session, entity_type: str, *, source_id: int, target_id: int
) -> tuple[MovedReference, ...]:
    """Trỏ lại mọi cột khóa ngoại đang chỉ vào nguồn.

    Một câu `UPDATE` cho mỗi cột, không vòng lặp theo dòng (LD-14): số dòng ở
    đây là số dòng chứng từ của một đối tác qua nhiều năm.
    """
    moved: list[MovedReference] = []
    for table_name, column_name in foreign_keys_to(session, entity_type):
        validate_identifier(table_name)
        validate_identifier(column_name)
        target_table = table(table_name, column(column_name))
        referring = target_table.c[column_name]
        # Qua `session.connection()` chứ không `session.execute`: chỉ
        # `CursorResult` mới có `rowcount`, và số dòng đã chuyển là thứ báo cáo
        # gửi về màn hình — không đếm được thì thao tác không hoàn tác được này
        # chỉ nói "xong" mà không nói xong cái gì. Cùng transaction, cùng vai trò
        # dataset: `session.connection()` trả về đúng kết nối session đang dùng.
        session.flush()
        statement = (
            update(target_table).where(referring == source_id).values({column_name: target_id})
        )
        # Savepoint chứ không `try` trần: ở PostgreSQL một `IntegrityError` làm
        # hỏng cả transaction, nên không có điểm quay lui thì câu lệnh kế tiếp
        # (và cả thông điệp lỗi tử tế bên dưới) không chạy được nữa. Cùng lối
        # `attachments/service.py` và `periods/service.py` đã đi.
        try:
            with session.begin_nested():
                result = session.connection().execute(statement)
        except IntegrityError as conflict:
            raise _conflicting_child_rows(
                conflict, entity_type=entity_type, table_name=table_name
            ) from conflict
        moved.append(MovedReference(table=table_name, column=column_name, rows=result.rowcount))
    # Câu `UPDATE` đi thẳng xuống DB nên bản sao trong session không hay biết —
    # cùng lý do `MasterDataService.move` phải làm hết hạn sau khi chuyển nhánh.
    session.expire_all()
    return tuple(moved)


def _conflicting_child_rows(
    conflict: IntegrityError, *, entity_type: str, table_name: str
) -> MasterDataMergeRefusedError:
    """`IntegrityError` lúc chuyển khóa ngoại → một câu nêu **việc phải làm**.

    Nguyên nhân luôn là một: cả hai bản ghi có dòng con mà ràng buộc duy nhất
    của bảng con coi là trùng nhau sau khi gộp (hai tài khoản ngân hàng cùng số,
    hai định mức cùng vật tư…). Để `IntegrityError` nổi lên tầng chung thì người
    dùng nhận *"Giá trị này đã có bản ghi khác sử dụng"* kèm tên một chỉ mục nội
    bộ — đúng thứ mà `MasterDataMergeRefusedError` được dựng ra để thay thế ở ba
    ca từ chối còn lại.

    Danh mục có bảng con mang bất biến riêng thì khai `MergeHook` để **gộp** hai
    bên thay vì đụng ràng buộc (xem `registry.MergeHook`); chỗ này là lưới an
    toàn cho bảng con chưa khai hook nào.
    """
    return MasterDataMergeRefusedError(
        f"Hai bản ghi có dữ liệu trùng nhau ở bảng {table_name} nên không gộp thẳng được — "
        "hãy gỡ bớt một bên rồi gộp lại",
        entity_type=entity_type,
        reason="conflicting_child_rows",
        child_table=table_name,
    )


def _move_usage_counter(
    session: Session, entity_type: str, *, source_id: int, target_id: int
) -> None:
    """Cộng bộ đếm của nguồn vào đích rồi bỏ dòng nguồn.

    Bộ đếm là dữ liệu dẫn xuất từ số dòng chứng từ vừa được trỏ lại, nên nó phải
    đi theo đúng số đó. Không cộng thì đích trông như chưa ai dùng và xóa được
    ngay sau khi vừa nhận toàn bộ chứng từ của nguồn.
    """
    count = usage_count_of(session, entity_type=entity_type, entity_id=source_id)
    if count == 0:
        return
    record_use(session, entity_type=entity_type, entity_id=target_id, delta=count)
    session.execute(
        delete(MasterDataUsage).where(
            MasterDataUsage.entity_type == entity_type,
            MasterDataUsage.entity_id == source_id,
        )
    )
    session.flush()
