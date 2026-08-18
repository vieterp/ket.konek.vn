"""Một lượt nhập liệu từ đầu tới cuối: đọc → đệm → kiểm → (ghi).

Cả hai job (kiểm và ghi) chạy **cùng** hàm này; khác nhau đúng một tham số
`commit`. Đó là cách H85 được bảo đảm chứ không chỉ được hứa: bước ghi không có
đường nào bỏ qua phép kiểm, vì nó không có đoạn mã riêng nào để bỏ qua.

Vì sao kiểm lại thay vì tin báo cáo cũ: giữa lúc bấm "Kiểm tra dữ liệu" và lúc
bấm "Nhập khẩu", một người khác đổi được chính danh mục mà tệp tra cứu tới —
ngừng theo dõi một đơn vị tính, đổi mã một nhóm cha, hoặc tạo trước một mã mà
tệp cũng có. Báo cáo cũ khi ấy mô tả một trạng thái không còn tồn tại. Vì kiểm
là SQL tập hợp trên bảng đệm nên chạy lại gần như miễn phí, còn cái giá của
việc *không* chạy lại là một lượt ghi tạo ra đúng những dòng mà hệ thống vừa từ
chối.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import IO, Any, Final
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    and_,
    cast,
    exists,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.errors import ImportParentUnresolvableError
from ket.kernel.excel.descriptors import TemplateDescriptor, template_for
from ket.kernel.excel.missing_references import (
    count_missing,
    create_missing,
    eligible_columns,
)
from ket.kernel.excel.models import ImportStagingRow
from ket.kernel.excel.reader import ensure_structure, iter_rows
from ket.kernel.excel.report import ImportMode, ImportReport, MissingReferenceMode
from ket.kernel.excel.sql import (
    boolean_expression,
    cell,
    cell_or_null,
    code_matches,
    column_source,
    editable_columns,
    extra_columns,
    is_active_expression,
    keep_existing,
    owned_by,
    table_for,
    visible_to,
)
from ket.kernel.excel.staging import StagingTable
from ket.kernel.jobs.registry import JobCancelled, JobContext
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.tree_path import ROOT_LEVEL

PROGRESS_LOADED: Final[int] = 40
PROGRESS_VALIDATED: Final[int] = 80
PROGRESS_DONE: Final[int] = 100
"""Ba mốc tiến độ. Đọc tệp là phần lâu nhất (nó chạy ở Python) nên nó chiếm phần
lớn thanh; kiểm và ghi đều chỉ là một nhúm câu SQL.

Giữa các mốc, thân job vẫn báo tiến độ **theo lô** (xem `_batch_pulse`): lượt
`report` không chỉ là thanh phần trăm — nó là nhịp gia hạn lease. Ba mốc cố định
từng là ba nhịp duy nhất, và một pha chạy quá 60 giây (lease mặc định) là đủ để
reaper coi worker đã chết rồi giao job cho tiến trình khác (audit phase 1–3, C1).
"""

ESTIMATED_ROWS_FOR_PROGRESS: Final[int] = 10_000
"""Mẫu số ước lượng cho thanh tiến độ pha đọc, khi chưa biết tổng số dòng.

Chỉ ảnh hưởng con số phần trăm hiển thị (5..39%), không ảnh hưởng nhịp gia hạn
lease — nhịp là "mỗi lô", bất kể phần trăm tính ra bao nhiêu. Lấy đúng trần
FR-NFR-043 nên tệp nhỏ tiến chậm hơn thực tế một chút rồi nhảy về 40% khi đọc
xong — vô hại; chiều ngược lại (ước lượng thấp, phần trăm vượt 40 giữa chừng)
mới là thứ gây hiểu nhầm."""

MAX_TREE_DEPTH: Final[int] = 25
"""Số vòng tối đa khi tạo bản ghi theo cấp cây.

Là **cận trên của vòng lặp**, không phải một luật nghiệp vụ: mỗi vòng tạo trọn
một cấp, nên một tệp cây 6 cấp dừng sau 7 vòng. Cận này tồn tại để dữ liệu dị
thường không biến vòng lặp thành vô hạn. Bằng độ sâu mà `PATH_MAX_LENGTH` cho
phép, tức sâu hơn nhiều lần mức 5 cấp mà FR-SYS-011 đòi."""


def run_import(
    context: JobContext,
    *,
    spec: CatalogSpec,
    source: IO[bytes],
    file_name: str,
    content_hash: str,
    mode: ImportMode,
    commit: bool,
    missing_reference: MissingReferenceMode = MissingReferenceMode.ERROR,
    allow_create_in: frozenset[str] = frozenset(),
) -> ImportReport:
    """Chạy trọn một lượt nhập trong transaction của job.

    `source` được đọc **hai lần** (kiểm cấu trúc, rồi đọc dòng) nên nó phải tua
    lại được — mọi nơi gọi đều mở tệp từ kho định địa chỉ theo nội dung, tức một
    tệp thật trên đĩa.
    """
    descriptor = template_for(spec)
    ensure_structure(source, descriptor)
    source.seek(0)

    staging = StagingTable(context.session, context.job_id, descriptor, table_for(spec.model))
    # **Một** phép tính "danh mục nào sẽ được tự tạo", dùng chung cho bước bỏ
    # qua lỗi tra cứu, bước đánh giá luật liên-trường, bước đếm và bước tạo.
    # Bản đầu để `_reference_errors` tự lọc bằng `allow_create_in` thô, nên một
    # danh mục **không** tự tạo được (đối tác → điều khoản thanh toán) vẫn bị bỏ
    # qua lỗi: lượt nhập báo "hợp lệ" rồi ghi một khóa ngoại rỗng.
    requested = allow_create_in if missing_reference is MissingReferenceMode.CREATE else frozenset()
    autocreate = frozenset(slug for slug, _ in eligible_columns(descriptor, requested))
    report = ImportReport(
        catalog=spec.slug,
        mode=mode,
        missing_reference=missing_reference,
        file_name=file_name,
        content_hash=content_hash,
        total_rows=0,
    )
    try:
        report.total_rows = staging.load(
            iter_rows(source, descriptor), on_batch=_batch_pulse(context)
        )
        context.progress.report(PROGRESS_LOADED, f"Đã đọc {report.total_rows} dòng")

        branch_id = context.branch_id
        failed_rows: set[int] = set()
        for error in staging.errors(
            spec,
            mode=mode,
            branch_id=branch_id,
            autocreate=autocreate,
            on_check=_check_pulse(context),
        ):
            failed_rows.add(error.row)
            report.add(error)
        report.error_rows = len(failed_rows)
        report.create_rows, report.update_rows = staging.counts(spec, branch_id=branch_id)
        if mode is ImportMode.CREATE_ONLY:
            # Ở chế độ chỉ-tạo-mới, mọi dòng trùng mã đã là dòng lỗi — con số
            # "sẽ cập nhật" không mô tả gì sắp xảy ra. Để nguyên nó sẽ là một
            # con số đúng về mặt truy vấn và sai về mặt ý nghĩa.
            report.update_rows = 0
        # Lượt **kiểm** chỉ đếm; lượt **ghi** mới tạo thật (FR-SYS-081 nói bước
        # kiểm không ghi gì). Hai hàm chứ không một hàm có cờ: một cờ đặt sai ở
        # nhánh nào đó biến bước kiểm thành bước ghi.
        report.created_references = count_missing(
            context.session,
            descriptor,
            context.job_id,
            branch_id=branch_id,
            allowed=autocreate,
        )
        context.progress.report(PROGRESS_VALIDATED, f"Đã kiểm {report.total_rows} dòng")

        if commit and report.is_valid:
            report.created_references = create_missing(
                context.session,
                descriptor,
                context.job_id,
                branch_id=branch_id,
                allowed=autocreate,
            )
            _record_created_references(context, report=report)
            _write(
                context.session,
                spec,
                descriptor,
                context.job_id,
                branch_id=branch_id,
                mode=mode,
                on_level=_level_pulse(context),
            )
            report.committed = True
            _record_import(context, spec=spec, report=report)
        context.progress.report(PROGRESS_DONE, _summary(report))
        return report
    finally:
        # Kể cả khi job hỏng giữa chừng: bảng đệm là nơi duy nhất trong dataset
        # giữ một bản sao nguyên văn dữ liệu người dùng ngoài bản ghi nghiệp vụ.
        staging.clear()


def _batch_pulse(context: JobContext) -> Callable[[int], None]:
    """Nhịp tại ranh giới lô của pha đọc: kiểm cờ hủy, rồi gia hạn lease.

    Hai việc trong một nhịp, đúng thứ tự đó:

    * **Hủy trước** — `request_cancel` hứa "worker dừng ở ranh giới lô gần
      nhất", và trước lượt vá này lời hứa đó chỉ đúng với job chưa bắt đầu
      (audit C2). Ném `JobCancelled` ở đây làm transaction của thân job rollback
      trọn — không dòng đệm nào, bản ghi nào được giữ lại.
    * **Gia hạn sau** — `report` vừa là tiến độ vừa là heartbeat; gọi mỗi lô
      (500 dòng) là thừa đủ dày so với lease 60 giây, và đủ thưa để hai lượt
      ghi DB một lô không thành gánh nặng.
    """

    def pulse(loaded: int) -> None:
        if context.progress.cancel_requested():
            raise JobCancelled(f"Tác vụ {context.job_id} bị hủy ở ranh giới lô {loaded} dòng")
        # Số học nguyên thuần — cổng ADR-015 cấm float ở mọi mã nghiệp vụ, kể
        # cả một thanh phần trăm: ngoại lệ nhỏ là chỗ ngoại lệ lớn chui vào.
        capped = min(loaded, ESTIMATED_ROWS_FOR_PROGRESS)
        percent = 5 + (capped * (PROGRESS_LOADED - 6)) // ESTIMATED_ROWS_FOR_PROGRESS
        context.progress.report(percent, f"Đang đọc… {loaded} dòng")

    return pulse


def _check_pulse(context: JobContext) -> Callable[[], None]:
    """Nhịp giữa các **nhóm phép kiểm** của pha kiểm — cùng hợp đồng với hai nhịp kia.

    Phần trăm nhích dần trong dải (40..79) nhưng không mang nghĩa tiến độ chính
    xác (số nhóm phép kiểm thay đổi theo danh mục); thứ có nghĩa là **nhịp** —
    mỗi lượt gọi là một lần gia hạn lease và một lần kiểm cờ hủy (review lát vá
    audit, M1: tệp cây lớn làm pha kiểm dài mà bản trước không có nhịp nào).
    """
    ticked = 0

    def pulse() -> None:
        nonlocal ticked
        if context.progress.cancel_requested():
            raise JobCancelled(f"Tác vụ {context.job_id} bị hủy giữa pha kiểm dữ liệu")
        ticked += 1
        percent = min(PROGRESS_LOADED + ticked, PROGRESS_VALIDATED - 1)
        context.progress.report(percent, "Đang kiểm dữ liệu…")

    return pulse


def _level_pulse(context: JobContext) -> Callable[[int], None]:
    """Nhịp tại ranh giới **cấp cây** của pha ghi — cùng hợp đồng với `_batch_pulse`.

    Mỗi cấp là một câu `INSERT … SELECT`; giữa hai câu là chỗ duy nhất đứng
    được. Một câu lệnh *đơn lẻ* chạy quá lease thì không nhịp nào cứu được —
    giới hạn đó thuộc về `_fence_before_commit` của worker: mất lease giữa
    chừng thì cả transaction rollback thay vì commit đè.
    """

    def pulse(level: int) -> None:
        if context.progress.cancel_requested():
            raise JobCancelled(
                f"Tác vụ {context.job_id} bị hủy ở ranh giới cấp cây thứ {level} của pha ghi"
            )
        context.progress.report(
            min(PROGRESS_VALIDATED + level, PROGRESS_DONE - 1), f"Đang ghi… cấp {level}"
        )

    return pulse


def _summary(report: ImportReport) -> str:
    if not report.is_valid:
        return f"{report.error_rows}/{report.total_rows} dòng có lỗi — chưa ghi gì"
    if not report.committed:
        return f"Hợp lệ: sẽ tạo mới {report.create_rows}, cập nhật {report.update_rows}"
    return f"Đã tạo mới {report.create_rows}, cập nhật {report.update_rows}"


def _record_import(context: JobContext, *, spec: CatalogSpec, report: ImportReport) -> None:
    """Một dòng nhật ký cho cả lượt nhập (H84).

    Đường ghi của nhập liệu là `INSERT … SELECT` đi qua Core, không qua unit-of-
    work, nên hai móc dựng `audit_log` không chạy. Thay vì 10.000 dòng "rỗng →
    giá trị" chôn mất một lần sửa tay thật, ghi **một** dòng trỏ vào bằng chứng:
    tệp đã kiểm vẫn nằm nguyên trong kho định địa chỉ theo nội dung, và
    `content_hash` ở đây chính là địa chỉ của nó.
    """
    record_action(
        context.session,
        entity_type=spec.entity_type,
        entity_id=str(context.job_id),
        action=AuditAction.IMPORTED,
        new_values={
            "catalog": spec.slug,
            "mode": report.mode.value,
            "file_name": report.file_name,
            "content_hash": report.content_hash,
            "total_rows": report.total_rows,
            "create_rows": report.create_rows,
            "update_rows": report.update_rows,
        },
    )


def _record_created_references(context: JobContext, *, report: ImportReport) -> None:
    """Một dòng nhật ký cho **mỗi** danh mục đích được tự tạo (FR-NFR-062, M3).

    Đường ghi của `create_missing` là `INSERT ... SELECT` qua Core, nên hai móc
    dựng `audit_log` không chạy — giống hệt lý do H84 đã dựng `_record_import`.
    Nhưng dòng của H84 mang `entity_type` của danh mục **chính**, nên câu hỏi
    "ai tạo đơn vị tính này, lúc nào" không có câu trả lời ở đâu: màn hình lịch
    sử của `units_of_measure` không thấy gì.

    Một dòng cho mỗi danh mục đích chứ không mỗi bản ghi: cùng cân nhắc H84 —
    bốn mươi dòng "rỗng → giá trị" chôn mất một lần sửa tay thật, còn `job_id`
    thì trỏ thẳng vào lượt nhập đã sinh ra chúng, và lượt ấy trỏ tiếp vào tệp
    trong kho định địa chỉ theo nội dung.
    """
    for slug, created in sorted(report.created_references.items()):
        spec = CATALOG_REGISTRY.get(slug)
        if spec is None:  # pragma: no cover — `eligible_columns` đã tra được nó
            continue
        record_action(
            context.session,
            entity_type=spec.entity_type,
            entity_id=str(context.job_id),
            action=AuditAction.IMPORTED,
            new_values={
                "catalog": slug,
                "created_by_import_of": report.catalog,
                "content_hash": report.content_hash,
                "created_rows": created,
            },
        )


def _write(
    session: Session,
    spec: CatalogSpec,
    descriptor: TemplateDescriptor,
    job_id: UUID,
    *,
    branch_id: int | None,
    mode: ImportMode,
    on_level: Callable[[int], None] | None = None,
) -> None:
    """Ghi dòng đã kiểm vào bảng danh mục (H82).

    Tạo mới chạy **theo từng cấp cây**: một dòng có `parent_code` trỏ tới một
    dòng khác trong cùng tệp chỉ dựng được `path` khi cha của nó đã có id. Vòng
    lặp dừng khi một vòng không tạo thêm được dòng nào — số vòng vì thế bằng độ
    sâu cây thật, không phải số dòng: một tệp 10.000 dòng cây 6 cấp chạy 6 câu
    `INSERT`.
    """
    remaining = _rows_to_create(session, spec, job_id, branch_id=branch_id)
    for level in range(MAX_TREE_DEPTH):
        if remaining == 0:
            break
        if on_level is not None:
            on_level(level + 1)
        _insert_one_level(session, spec, descriptor, job_id, branch_id=branch_id)
        after = _rows_to_create(session, spec, job_id, branch_id=branch_id)
        if after == remaining:
            # Một vòng không tạo thêm được dòng nào trong khi vẫn còn dòng chờ:
            # `parent_code` của chúng tạo chu trình (A→B→A, hoặc một dòng tự trỏ
            # vào chính mã của nó). Bước kiểm theo dòng không bắt được loại này —
            # nó là câu hỏi về **đồ thị**, không về từng dòng.
            raise ImportParentUnresolvableError(
                "Có dòng khai mã nhóm cha vòng lại chính nhánh của nó — hãy kiểm tra "
                "lại cột mã nhóm cha",
                remaining=remaining,
            )
        remaining = after
    if remaining != 0:
        # Thoát vòng vì hết lượt chứ không vì hết việc: cây sâu hơn mức cho phép.
        raise ImportParentUnresolvableError(
            f"Cây danh mục trong tệp sâu hơn {MAX_TREE_DEPTH} cấp — hãy tách thành nhiều lượt nhập",
            remaining=remaining,
            max_depth=MAX_TREE_DEPTH,
        )
    if mode is ImportMode.CREATE_AND_UPDATE:
        _update_existing_rows(session, spec, descriptor, job_id, branch_id=branch_id)


def _rows_to_create(
    session: Session, spec: CatalogSpec, job_id: UUID, *, branch_id: int | None
) -> int:
    """Số dòng đệm **chưa có** bản ghi tương ứng — tức số dòng câu `INSERT` phải tạo.

    Đếm trước rồi đối chiếu sau, thay vì tin vòng lặp tự dừng đúng chỗ: vòng lặp
    dừng khi một lượt không tạo thêm gì, và điều đó đúng cả khi *không còn dòng
    nào* lẫn khi *những dòng còn lại không bao giờ tạo được*.
    """
    table = table_for(spec.model)
    return int(
        session.execute(
            select(func.count())
            .select_from(ImportStagingRow)
            .outerjoin(table, code_matches(table, branch_id))
            .where(ImportStagingRow.job_id == job_id)
            .where(table.c.id.is_(None))
        ).scalar_one()
    )


def _new_rows_source(
    spec: CatalogSpec,
    descriptor: TemplateDescriptor,
    job_id: UUID,
    *,
    branch_id: int | None,
) -> Select[tuple[Any, ...]]:
    """Truy vấn sinh ra các dòng sẽ được chèn ở **một** cấp cây.

    Ba mệnh đề quyết định "cấp này gồm dòng nào":

    * `LEFT JOIN parent` — cha đã tồn tại trong bảng danh mục (do vòng trước tạo,
      hoặc vốn đã có sẵn).
    * `parent_code IS NULL OR parent.id IS NOT NULL` — dòng gốc luôn tới lượt;
      dòng có cha chỉ tới lượt khi cha đã có id.
    * `NOT EXISTS existing` — mã chưa có trong danh mục. Ở chế độ cập nhật, dòng
      trùng mã thuộc phần `UPDATE`, không phải phần `INSERT`.

    `nextval` nằm trong một truy vấn con dẫn xuất để giá trị của nó dùng được
    **hai lần** (khóa chính, và phần đuôi của `path`). Gọi `nextval` hai lần sẽ
    cho hai số khác nhau — `path` trỏ vào một id không tồn tại, và cây gãy ở
    đúng chỗ không ai kiểm.
    """
    table = table_for(spec.model)
    parent = table.alias("parent")
    existing = table.alias("existing")
    sequence = f"{table.name}_id_seq"

    numbered = (
        select(
            ImportStagingRow.id.label("staging_id"),
            func.nextval(func.pg_get_serial_sequence(table.name, "id")).label("new_id"),
        )
        .where(ImportStagingRow.job_id == job_id)
        .subquery("numbered")
    )
    del sequence

    parent_visible = and_(
        parent.c.code == cell_or_null("parent_code"),
        visible_to(parent, branch_id),
    )
    # Cùng luật với `code_matches` (H86): phạm vi **ghi** là chi nhánh đang thao
    # tác, không phải phạm vi đọc. Dùng `visible_to` ở đây thì đứng ở chi nhánh A
    # mà nhập một mã đã có ở phần dùng chung sẽ không tạo được bản ghi riêng của
    # A — đúng thứ FR-SYS-018 cho phép và `POST /master/{slug}` vẫn làm được.
    already_there = exists(
        select(existing.c.id).where(
            and_(existing.c.code == cell("code"), owned_by(existing, branch_id))
        )
    )

    columns: list[ColumnElement[Any]] = [
        numbered.c.new_id,
        table_for(ImportStagingRow).c.uid,
        # Không ép kiểu: cả ba là `varchar`, và `CAST(... AS varchar(n))` **cắt**
        # thay vì báo lỗi. Gán thẳng `text` thì PostgreSQL nổ với "value too long"
        # — xem `sql._typed`.
        cell_or_null("code"),
        cell_or_null("name"),
        cell_or_null("name_en"),
        parent.c.id,
        literal(branch_id),
        # Bản SQL của `tree_path.child_path`: path của cha (rỗng nếu là nút gốc)
        # nối id rồi nối **một dấu chấm cuối**. Dấu chấm cuối không phải trang
        # trí — thiếu nó thì tiền tố `'1.4'` khớp luôn nút `'1.40'`, và một nhánh
        # lạ lọt vào mọi báo cáo tổng hợp theo nhóm. `PATH_PATTERN` (`^([0-9]+\.)+$`)
        # canh đúng hình dạng đó, và nó đã bắt được bản đầu của dòng này.
        func.concat(func.coalesce(parent.c.path, ""), cast(numbered.c.new_id, Text()), "."),
        func.coalesce(parent.c.level + 1, ROOT_LEVEL),
        boolean_expression("is_group"),
        is_active_expression(),
        literal(1),
    ]
    columns.extend(
        column_source(column, branch_id, table.c[column.target_field or column.field])
        for column in extra_columns(descriptor)
    )

    return (
        select(*columns)
        .select_from(ImportStagingRow)
        .join(numbered, numbered.c.staging_id == ImportStagingRow.id)
        .outerjoin(parent, parent_visible)
        .where(ImportStagingRow.job_id == job_id)
        .where(or_(cell_or_null("parent_code").is_(None), parent.c.id.is_not(None)))
        .where(~already_there)
    )


def _insert_one_level(
    session: Session,
    spec: CatalogSpec,
    descriptor: TemplateDescriptor,
    job_id: UUID,
    *,
    branch_id: int | None,
) -> None:
    """Tạo mọi dòng **mà cha của nó đã có id** — một cấp cây, một câu lệnh.

    **Không** trả về số dòng: `rowcount` của một `INSERT ... SELECT` là `-1` ở
    driver này, nên nó chưa bao giờ là một tín hiệu dùng được. Bản đầu dừng vòng
    lặp bằng chính con số đó và vì thế chạy đủ `MAX_TREE_DEPTH` lượt mỗi lần —
    vô hại về kết quả, nhưng nó đốt `nextval` cho mọi dòng ở mỗi lượt và giấu
    mất ca chu trình. Người gọi đếm phần việc còn lại bằng `_rows_to_create`.

    `path` dựng từ `path` của cha nối với id vừa cấp, đúng công thức
    `tree_path.child_path`; `level` từ `level` của cha. Id lấy trước bằng
    `nextval` ngay trong câu `SELECT`, nên không có đường "chèn rồi `UPDATE` lại
    path" — thứ mà `MasterDataService.create` đã bác ở lát 3A vì nó sinh ra bản
    ghi mới toanh mang `row_version = 2` cùng một dòng nhật ký "vừa tạo xong đã
    sửa".

    Dòng chưa tới lượt (cha còn nằm trong tệp, chưa được tạo) đơn giản là không
    khớp mệnh đề `WHERE` của vòng này và sẽ khớp ở vòng sau.
    """
    target = table_for(spec.model)
    names = [
        "id",
        "uid",
        "code",
        "name",
        "name_en",
        "parent_id",
        "branch_id",
        "path",
        "level",
        "is_group",
        "is_active",
        "row_version",
    ]
    names.extend(column.target_field or column.field for column in extra_columns(descriptor))
    source = _new_rows_source(spec, descriptor, job_id, branch_id=branch_id)
    session.execute(insert(target).from_select(names, source))


def _update_existing_rows(
    session: Session,
    spec: CatalogSpec,
    descriptor: TemplateDescriptor,
    job_id: UUID,
    *,
    branch_id: int | None,
) -> None:
    """Cập nhật bản ghi đã có — chỉ ở chế độ `create_and_update` (H80).

    `row_version` tăng một, như mọi lượt ghi khác: màn hình đang mở bản ghi này
    phải nhận `409` ở lần lưu kế tiếp (FR-NFR-005) chứ không được ghi đè lặng lẽ
    thứ vừa nhập. Cột nào được sửa thì `sql.editable_columns` quyết định, và lý
    do loại từng nhóm được ghi ngay tại đó.
    """
    table = table_for(spec.model)
    editable = editable_columns(spec, descriptor)
    if not editable:
        return
    assignments: dict[str, ColumnElement[Any]] = {
        (column.target_field or column.field): keep_existing(
            column, branch_id, table.c[column.target_field or column.field]
        )
        for column in editable
    }
    assignments["row_version"] = table.c.row_version + 1
    session.execute(
        update(table)
        .values(**assignments)
        .where(ImportStagingRow.job_id == job_id)
        .where(code_matches(table, branch_id))
    )
