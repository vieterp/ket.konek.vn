"""Tự tạo bản ghi còn thiếu ở danh mục được trỏ tới (FR-NFR-062, nợ L1 của 3C-1).

Một tệp vật tư hàng hóa 10.000 dòng trỏ tới hàng chục đơn vị tính và kho. Bắt
người dùng khai trước từng cái ở một lượt nhập riêng là chia một việc liền mạch
thành ba, và họ sẽ khai thiếu đúng cái thứ mười một rồi phải chạy lại cả vòng.

**Ba hàng rào**, vì đây là đường ghi vào một danh mục **khác** danh mục người
dùng đang nhập — đúng chỗ lát 3C-1 đã từ chối giao tính năng này:

1. **Người dùng chọn tường minh.** `MissingReferenceMode.CREATE`, không phải mặc
   định, và báo cáo của lượt kiểm nói trước "sẽ tạo thêm 12 đơn vị tính".
2. **Chỉ danh mục tự tạo được.** `CatalogSpec.auto_creatable` suy từ registry, và
   nó loại chính những danh mục mà một bản ghi "chỉ có mã" sẽ không hợp lệ —
   vật tư hàng hóa (H76) và đối tác. Xem docstring của nó.
3. **Quyền trên từng danh mục đích.** Endpoint kiểm `master.{đích}.create` rồi
   ghi danh sách đã duyệt vào tham số job; thân job chỉ tạo trong danh sách ấy.
   Đúng khuôn H89 — hai chỗ phải khớp, và tham số "thừa" chính là thứ buộc chúng
   khớp. Không có hàng rào này thì tự tạo là một đường vòng qua lớp quyền
   per-danh-mục mà H48 dựng ra.

**Lượt kiểm không ghi gì** (FR-SYS-081). Nó **đếm** số bản ghi sẽ phải tạo; lượt
ghi mới tạo thật. Đó là lý do `count_missing` và `create_missing` là hai hàm chứ
không một hàm có cờ: một cờ đặt sai ở nhánh nào đó biến bước kiểm thành bước ghi.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Integer,
    Select,
    Text,
    cast,
    exists,
    func,
    literal,
    select,
    values,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import column as sql_column

from ket.kernel.excel.descriptors import ColumnDescriptor, TemplateDescriptor
from ket.kernel.excel.models import ImportStagingRow
from ket.kernel.excel.sql import cell_or_null, table_of, visible_to
from ket.kernel.identifiers import uuid7
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.tree_path import ROOT_LEVEL


def eligible_columns(
    descriptor: TemplateDescriptor, allowed: frozenset[str]
) -> tuple[tuple[str, ColumnDescriptor], ...]:
    """Cột tra cứu được phép tự tạo trong lượt này.

    Ba điều kiện cùng lúc, và cột nào trượt một điều kiện thì mã thiếu của nó vẫn
    là **dòng lỗi** như trước:

    * không phải cột mã nhóm cha — nhóm cha trỏ về chính danh mục đang nhập, và
      tệp vốn được phép khai cả cây trong một lượt. Tự tạo ở đó biến một mã gõ
      sai thành một nút rác thay vì một lỗi;
    * danh mục đích **tự tạo được** (`CatalogSpec.auto_creatable`);
    * danh mục đích nằm trong `allowed` — danh sách mà endpoint đã kiểm quyền.
    """
    found: list[tuple[str, ColumnDescriptor]] = []
    for column in descriptor.references:
        slug = column.reference_slug
        if column.target_field == "parent_id" or slug is None:
            continue
        target = CATALOG_REGISTRY.get(slug)
        if target is None or not target.auto_creatable or slug not in allowed:
            continue
        # Trả kèm `slug` đã thu hẹp kiểu: chỗ gọi không phải khẳng định lại
        # `reference_slug is not None` mà chính vòng lặp này vừa kiểm.
        found.append((slug, column))
    return tuple(found)


def _missing_codes(column: ColumnDescriptor, job_id: UUID, branch_id: int | None) -> Select[Any]:
    """Mã **khác nhau** mà ô tra cứu nhắc tới nhưng danh mục đích chưa có.

    `DISTINCT` vì một tệp 10.000 dòng vật tư nhắc tới cùng một đơn vị tính hàng
    nghìn lần, và mỗi mã chỉ được tạo một bản ghi.

    Phép so "đã có chưa" dùng `visible_to` chứ không `owned_by`: câu hỏi là "lượt
    nhập này **tra ra** được mã đó không", và `sql.reference_id` — thứ sẽ tra nó
    lúc ghi — cũng lọc bằng `visible_to`. Dùng phạm vi ghi ở đây sẽ tạo thêm một
    bản ghi riêng chi nhánh cho một mã dùng chung mà lượt nhập vốn tra ra được.
    """
    target = table_of(column.reference_slug)
    code = cell_or_null(column.field)
    already_there = exists(
        select(target.c.id).where(target.c.code == code).where(visible_to(target, branch_id))
    )
    return (
        select(code.label("code"))
        .select_from(ImportStagingRow)
        .where(ImportStagingRow.job_id == job_id)
        .where(code.is_not(None))
        .where(~already_there)
        .distinct()
    )


def count_missing(
    session: Session,
    descriptor: TemplateDescriptor,
    job_id: UUID,
    *,
    branch_id: int | None,
    allowed: frozenset[str],
) -> dict[str, int]:
    """`{slug đích: số bản ghi sẽ tạo}` — con số của lượt **kiểm**, không ghi gì."""
    counts: dict[str, int] = {}
    for slug, column in eligible_columns(descriptor, allowed):
        found = int(
            session.execute(
                select(func.count()).select_from(
                    _missing_codes(column, job_id, branch_id).subquery("missing")
                )
            ).scalar_one()
        )
        if found:
            counts[slug] = counts.get(slug, 0) + found
    return counts


def create_missing(
    session: Session,
    descriptor: TemplateDescriptor,
    job_id: UUID,
    *,
    branch_id: int | None,
    allowed: frozenset[str],
) -> dict[str, int]:
    """Tạo bản ghi tối thiểu cho từng mã còn thiếu. Trả về số đã tạo theo danh mục.

    Chạy **trước** khi ghi danh mục chính, trong cùng transaction: câu `INSERT`
    của bảng chính tra mã thành khóa ngoại bằng `sql.reference_id`, nên bản ghi
    phải có mặt trước đó. Cùng transaction nghĩa là một lỗi ở bảng chính cũng
    cuốn theo các bản ghi vừa tạo — không để lại đơn vị tính mồ côi của một lượt
    nhập đã thất bại.

    Bản ghi sinh ra luôn ở **cấp gốc** (`parent_id` rỗng): không có thông tin nào
    trong tệp nói nó thuộc nhóm nào, và đoán một nhóm là đoán sai ở đúng chỗ
    người dùng sẽ không nghĩ tới việc kiểm lại.

    Con số trả về là số mã **lượt này định tạo**; trong ca đua hiếm (một lượt
    nhập khác vừa tạo cùng mã, xem `ON CONFLICT` ở `_insert_minimal_rows`), số
    tạo thật có thể ít hơn một vài đơn vị — báo cáo khi ấy dư chứ không thiếu,
    và bản ghi thì vẫn tồn tại đúng như báo cáo nói.
    """
    created: dict[str, int] = {}
    for slug, column in eligible_columns(descriptor, allowed):
        codes = [str(row[0]) for row in session.execute(_missing_codes(column, job_id, branch_id))]
        if not codes:
            continue
        _insert_minimal_rows(session, slug, codes, branch_id=branch_id)
        created[slug] = created.get(slug, 0) + len(codes)
    return created


def _insert_minimal_rows(
    session: Session, slug: str, codes: list[str], *, branch_id: int | None
) -> None:
    """Một câu `INSERT ... SELECT` cho cả danh sách mã của một danh mục đích.

    `path` là `NOT NULL` và giá trị của nó chứa chính `id` mà `SERIAL` cấp, nên
    không có đường "chèn trước, cập nhật sau": bản đầu làm vậy và đổ ngay ở
    `NotNullViolation`. `nextval` vì thế nằm trong một truy vấn con dẫn xuất để
    giá trị dùng được **hai lần** — khóa chính, và phần đuôi của `path`. Gọi
    `nextval` hai lần cho hai số khác nhau: `path` trỏ vào một id không tồn tại
    và cây gãy ở chỗ không ai kiểm. Cùng cái bẫy mà `pipeline._new_rows_source`
    đã ghi lại.

    Dấu chấm cuối của `path` không phải trang trí: thiếu nó thì tiền tố `1.4`
    khớp luôn nút `1.40`, và một nhánh lạ lọt vào mọi báo cáo tổng hợp theo nhóm.

    Chỉ liệt kê những cột nó thật sự đặt: cột riêng của danh mục (nếu có) rơi về
    **ngầm định của chính cột**, khác hẳn câu `INSERT` của bảng chính — chỗ đó
    liệt kê mọi cột nên phải tự lo phần ngầm định (xem `sql._with_default`).

    `uid` sinh ở Python (RT-19): PostgreSQL 16 chỉ có `gen_random_uuid()`, cho ra
    v4 ngẫu nhiên đều — chạy trót lọt và mất đúng tính tăng dần theo thời gian mà
    cột này tồn tại vì nó.
    """
    table = table_of(slug)
    wanted = values(
        sql_column("code", table.c.code.type),
        sql_column("uid", postgresql.UUID(as_uuid=True)),
        name="wanted",
    ).data([(code, uuid7()) for code in codes])
    numbered = select(
        wanted.c.code,
        wanted.c.uid,
        func.nextval(func.pg_get_serial_sequence(table.name, "id")).label("new_id"),
    ).subquery("numbered")
    # `ON CONFLICT DO NOTHING` nhắm đúng chỉ mục duy-nhất **một phần** theo phạm
    # vi của lượt nhập (BR-SYS-01 là hai chỉ mục partial, không phải một ràng
    # buộc chung — xem `master_data/base.py`). Không có nó, hai lượt nhập chạy
    # đồng thời cùng thiếu một mã sẽ cùng qua phép đếm `_missing_codes` (dòng
    # của nhau chưa commit nên chưa nhìn thấy) rồi cùng `INSERT` — lượt thua
    # nhận `IntegrityError` thô và cả job đổ (audit phase 1–3, H1). Với DO
    # NOTHING, PostgreSQL bắt lượt sau chờ lượt trước phân định rồi lặng lẽ bỏ
    # qua mã đã có; câu `INSERT` của bảng chính tra khóa ngoại **sau** thời điểm
    # đó nên tra ra đúng bản ghi vừa thắng cuộc.
    if branch_id is None:
        conflict_kwargs: dict[str, Any] = {
            "index_elements": [table.c.code],
            "index_where": table.c.branch_id.is_(None),
        }
    else:
        conflict_kwargs = {
            "index_elements": [table.c.branch_id, table.c.code],
            "index_where": table.c.branch_id.is_not(None),
        }
    session.execute(
        postgresql.insert(table)
        .on_conflict_do_nothing(**conflict_kwargs)
        .from_select(
            [
                "id",
                "uid",
                "code",
                "name",
                "path",
                "level",
                "is_group",
                "is_active",
                "branch_id",
            ],
            select(
                numbered.c.new_id,
                numbered.c.uid,
                numbered.c.code,
                # Tên bằng chính mã: bản ghi sinh ra nhìn là biết cần đặt lại tên.
                numbered.c.code,
                func.concat(cast(numbered.c.new_id, Text()), "."),
                literal(ROOT_LEVEL),
                literal(False),
                literal(True),
                literal(branch_id, type_=Integer),
            ),
        )
    )


def authorised_targets(spec: CatalogSpec, descriptor: TemplateDescriptor) -> tuple[str, ...]:
    """Danh mục đích mà endpoint phải kiểm quyền `create` trước khi cho tự tạo.

    Tính từ chính `descriptor` nên nó không thể lệch với thứ `eligible_columns`
    sẽ chấp nhận sau đó — hai danh sách rời là chỗ để endpoint kiểm quyền trên
    một danh mục còn thân job ghi vào một danh mục khác (đúng lỗ hổng C1/H89).
    """
    del spec
    return tuple(
        sorted(
            {
                column.reference_slug
                for column in descriptor.references
                if column.reference_slug is not None
                and column.target_field != "parent_id"
                and (target := CATALOG_REGISTRY.get(column.reference_slug)) is not None
                and target.auto_creatable
            }
        )
    )
