"""Nhập liệu danh mục: kiểm dữ liệu set-based rồi ghi (lát 3C-1, bước 12–15).

Chạy thẳng `run_import` trên PostgreSQL thật thay vì đi vòng qua worker: mọi luật
của lát này nằm trong hàm đó, và một test đi qua hàng đợi sẽ đo thêm cả vòng đời
job — thứ `test_job_queue_lifecycle.py` đã đo rồi. Phần HTTP (tệp mẫu, hai lớp
quyền, thứ tự route) nằm ở `test_import_api.py`.

Bốn nhóm khẳng định, theo đúng thứ tự rủi ro:

* **Không ghi khi còn lỗi** (FR-SYS-081) — bất biến trung tâm của cả lát.
* **Cây dựng đúng** — `path`/`level` khi nhóm cha nằm trong chính tệp đó.
* **Trường chốt một lần** (H81) — đường ghi thứ ba vào `nature`/`base_unit_id`.
* **Chế độ** (H80) — `create_only` từ chối mã đã có, `create_and_update` thì sửa.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol

import pytest
from openpyxl import Workbook
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import branch_ids, ensure_branches, unique_code
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import ImportParentUnresolvableError, ImportSourceNotValidatedError
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.job import VALIDATE_IMPORT, ImportCommitParams, run_commit
from ket.kernel.excel.models import ImportStagingRow
from ket.kernel.excel.pipeline import run_import
from ket.kernel.excel.report import ImportMode, ImportReport
from ket.kernel.jobs import queue
from ket.kernel.jobs.builtin import SLOW_TASK
from ket.kernel.jobs.models import JobStatus
from ket.kernel.jobs.registry import JobContext
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.item import Item
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.models.warehouse import Warehouse
from ket.kernel.master_data.registry import REGISTRY, CatalogSpec
from ket.kernel.persistence.types import AuditValues
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db


class RunImport(Protocol):
    """Chữ ký của fixture `run` — một lượt nhập trên PostgreSQL thật.

    Protocol chứ không `Callable[..., ImportReport]`: `...` tắt luôn phần kiểm
    tham số, mà bốn tham số của nó (`mode`, `commit`, `branch_id`) chính là thứ
    mỗi test chọn khác nhau, và gõ nhầm một tên sẽ lặng lẽ rơi vào giá trị mặc
    định — tức test xanh trong khi nó đo một chế độ khác chế độ nó nói.
    """

    def __call__(
        self,
        slug: str,
        rows: list[list[object]],
        *,
        mode: ImportMode = ImportMode.CREATE_ONLY,
        commit: bool = False,
        branch_id: int | None = None,
    ) -> ImportReport: ...


WAREHOUSES = "warehouses"
ITEMS = "items"
UNITS = "units_of_measure"


@dataclass
class FakeProgress:
    """Bản giả của `JobProgress` — ghi lại thay vì chạm DB.

    Đủ cho lát này: thân job chỉ **báo** tiến độ, nó không đọc lại gì. Bản thật
    dùng một connection riêng và đã có test của nó ở `test_worker_runner.py`.
    """

    reports: list[tuple[int, str | None]]
    cancelled: bool = False

    def report(self, percent: int, message: str | None = None) -> None:
        self.reports.append((percent, message))

    def cancel_requested(self) -> bool:
        return self.cancelled

    def checkpoint(self, state: AuditValues) -> None:  # pragma: no cover — job này không dùng
        raise AssertionError("nhập liệu không khai `checkpointed`")


def _spec(slug: str) -> CatalogSpec:
    spec = REGISTRY.get(slug)
    assert spec is not None
    return spec


def _workbook(slug: str, rows: list[list[object]]) -> BytesIO:
    """Tệp đúng cấu trúc mẫu, kèm các dòng đã cho."""
    descriptor = template_for(_spec(slug))
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = descriptor.sheet_name
    sheet.append(list(descriptor.headers))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


BRANCH_FOR_SCOPE = "CN_IMPORT_SCOPE"


@pytest.fixture
def a_branch(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> int:
    """Một chi nhánh thật, để đo phạm vi **ghi** của lượt nhập (H86)."""
    ensure_branches(session_factory, dataset_alpha, [BRANCH_FOR_SCOPE])
    return branch_ids(session_factory, dataset_alpha, [BRANCH_FOR_SCOPE])[BRANCH_FOR_SCOPE]


@pytest.fixture
def scope(dataset_alpha: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    app_engine: Engine,
    tmp_path: Path,
) -> RunImport:
    """Chạy một lượt nhập trong một transaction thật, trả về báo cáo.

    Mỗi lượt có `job_id` riêng và một dòng `jobs` thật: bảng đệm có khóa ngoại
    tới `jobs`, nên một id bịa ra sẽ đổ ở ràng buộc chứ không ở luật đang đo.
    """
    assert app_engine is not None

    def _run(
        slug: str,
        rows: list[list[object]],
        *,
        mode: ImportMode = ImportMode.CREATE_ONLY,
        commit: bool = False,
        branch_id: int | None = None,
    ) -> ImportReport:
        with unit_of_work(session_factory, scope) as session:
            job = queue.enqueue(session, job_type=SLOW_TASK, params={}, requested_by=1)
            job_id = job.id
        with unit_of_work(session_factory, scope) as session:
            context = JobContext(
                job_id=job_id,
                session=session,
                progress=FakeProgress(reports=[]),
                attempt=1,
                dataset_schema=dataset_alpha.schema_name,
                branch_id=branch_id,
                requested_by=1,
                storage_root=tmp_path,
            )
            return run_import(
                context,
                spec=_spec(slug),
                source=_workbook(slug, rows),
                file_name="nhap-lieu.xlsx",
                content_hash="0" * 64,
                mode=mode,
                commit=commit,
            )

    return _run


def _warehouse_row(code: str, name: str, parent: str | None = None) -> list[object]:
    return [code, name, None, parent, None, None]


def _warehouses(
    session_factory: sessionmaker[Session], scope: RequestScope, codes: list[str]
) -> dict[str, Warehouse]:
    with unit_of_work(session_factory, scope) as session:
        return {
            row.code: row
            for row in session.scalars(select(Warehouse).where(Warehouse.code.in_(codes))).all()
        }


# ------------------------------------------------- bất biến: không ghi khi lỗi


def test_a_file_with_errors_writes_nothing(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """FR-SYS-081: bắt buộc kiểm trước khi ghi — và "kiểm" nghĩa là **chặn**.

    Dòng hai hợp lệ, dòng ba thiếu tên. Nếu bước ghi xử lý từng dòng độc lập thì
    dòng hai sẽ được tạo; nó **không được**, vì người dùng đọc "2/3 dòng lỗi" và
    sẽ sửa tệp rồi nộp lại cả tệp.
    """
    good = unique_code("KHO_OK")
    report = run(
        WAREHOUSES,
        [_warehouse_row(good, "Kho tốt"), [unique_code("KHO_BAD"), None, None, None, None, None]],
        commit=True,
    )
    assert not report.is_valid
    assert report.error_rows == 1
    assert not report.committed
    assert not _warehouses(session_factory, scope, [good])


def test_the_report_names_the_row_and_the_column(run: RunImport) -> None:
    """FR-SYS-081: "nguyên nhân lỗi theo từng dòng", đủ để sửa mà không phải dò."""
    report = run(WAREHOUSES, [[unique_code("K"), None, None, None, None, None]])
    (error,) = report.errors
    assert error.row == 2
    assert error.column is not None and error.column.startswith("Tên")
    assert error.code == "import.required"


def test_a_value_too_long_is_reported_with_the_value(run: RunImport) -> None:
    """FR-SYS-083: kiểm độ dài tối đa từng trường."""
    report = run(WAREHOUSES, [[unique_code("K"), "x" * 300, None, None, None, None]])
    codes = {error.code for error in report.errors}
    assert "import.too_long" in codes


def test_two_rows_with_the_same_code_flag_only_the_second(run: RunImport) -> None:
    """Dòng đầu của nhóm trùng là dòng hợp lệ — báo cả nhóm là hai lỗi cho một việc sửa."""
    code = unique_code("KHO_DUP")
    report = run(
        WAREHOUSES,
        [_warehouse_row(code, "Kho một"), _warehouse_row(code, "Kho hai")],
    )
    duplicates = [error for error in report.errors if error.code == "import.duplicate_in_file"]
    assert [error.row for error in duplicates] == [3]


def test_an_unknown_parent_code_is_an_error(run: RunImport) -> None:
    report = run(
        WAREHOUSES,
        [_warehouse_row(unique_code("K"), "Kho con", "KHONG_TON_TAI_" + uuid.uuid4().hex[:6])],
    )
    assert {error.code for error in report.errors} == {"import.reference_not_found"}


# ------------------------------------------------------------- ghi & cây


def test_a_clean_file_is_written_and_counted(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    codes = [unique_code("KHO_A"), unique_code("KHO_B")]
    report = run(
        WAREHOUSES,
        [_warehouse_row(codes[0], "Kho A"), _warehouse_row(codes[1], "Kho B")],
        commit=True,
    )
    assert report.is_valid and report.committed
    assert report.create_rows == 2 and report.update_rows == 0
    written = _warehouses(session_factory, scope, codes)
    assert set(written) == set(codes)
    assert written[codes[0]].name == "Kho A"


def test_a_parent_declared_in_the_same_file_builds_the_tree(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Người dùng khai cả cây trong một lần nhập (H79, ghi chú của `parent_code`).

    Kiểm `path`/`level` chứ không chỉ `parent_id`: `path` là thứ mọi truy vấn
    nhánh đọc, và nó được dựng bằng chính id vừa cấp — nếu `nextval` bị gọi hai
    lần thì `parent_id` vẫn đúng trong khi `path` trỏ vào một id không tồn tại.
    """
    group = unique_code("NHOM")
    child = unique_code("KHO_CON")
    report = run(
        WAREHOUSES,
        [
            [group, "Nhóm kho", None, None, "x", None],
            _warehouse_row(child, "Kho con", group),
        ],
        commit=True,
    )
    assert report.is_valid and report.committed, report.errors

    rows = _warehouses(session_factory, scope, [group, child])
    parent, kid = rows[group], rows[child]
    assert parent.is_group is True
    assert kid.parent_id == parent.id
    assert kid.level == parent.level + 1
    assert kid.path == f"{parent.path}{kid.id}."


def test_children_may_appear_before_their_parent_in_the_file(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Thứ tự dòng trong tệp không phải thứ tự tạo — vòng lặp chạy theo cấp cây."""
    group = unique_code("NHOM2")
    child = unique_code("KHO_CON2")
    report = run(
        WAREHOUSES,
        [
            _warehouse_row(child, "Kho con", group),
            [group, "Nhóm kho", None, None, "x", None],
        ],
        commit=True,
    )
    assert report.is_valid and report.committed, report.errors
    rows = _warehouses(session_factory, scope, [group, child])
    assert rows[child].parent_id == rows[group].id


def test_imported_rows_get_a_uuid7(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """RT-19: `uid` phải là v7 ở **mọi** đường ghi, kể cả nhập liệu.

    PostgreSQL 16 chỉ có `gen_random_uuid()` (v4), nên một câu `INSERT` tự sinh
    uid sẽ chạy trót lọt và âm thầm mất tính tăng dần theo thời gian — thứ cột
    này tồn tại vì nó.
    """
    code = unique_code("KHO_UID")
    run(WAREHOUSES, [_warehouse_row(code, "Kho uid")], commit=True)
    row = _warehouses(session_factory, scope, [code])[code]
    assert row.uid.version == 7


def test_is_active_defaults_to_true_when_the_column_is_blank(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Cột "Còn theo dõi" ngược chiều mọi cột boolean khác.

    Dùng chung biểu thức boolean thường sẽ khiến cả tệp được tạo ở trạng thái
    ngừng theo dõi — danh mục rỗng trên mọi màn hình, không lỗi nào nổ.
    """
    code = unique_code("KHO_ACT")
    run(WAREHOUSES, [_warehouse_row(code, "Kho mặc định")], commit=True)
    row = _warehouses(session_factory, scope, [code])[code]
    assert row.is_active is True


def test_is_active_false_is_honoured(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    code = unique_code("KHO_INACT")
    run(WAREHOUSES, [[code, "Kho ngừng", None, None, None, "không"]], commit=True)
    row = _warehouses(session_factory, scope, [code])[code]
    assert row.is_active is False


def test_the_staging_table_is_empty_after_the_run(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Bảng đệm giữ nguyên văn dữ liệu người dùng — nó không được ở lại."""
    run(WAREHOUSES, [_warehouse_row(unique_code("KHO_TMP"), "Kho tạm")], commit=True)
    with unit_of_work(session_factory, scope) as session:
        remaining = session.execute(select(ImportStagingRow.id).limit(1)).first()
    assert remaining is None


# ------------------------------------------------------------------ chế độ


def test_create_only_refuses_a_code_that_already_exists(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """H80: mặc định không ghi đè, và nói rõ cách bật chế độ ghi đè."""
    code = unique_code("KHO_EXIST")
    run(WAREHOUSES, [_warehouse_row(code, "Bản đầu")], commit=True)

    report = run(WAREHOUSES, [_warehouse_row(code, "Bản sau")], commit=True)
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.code_exists"}
    row = _warehouses(session_factory, scope, [code])[code]
    assert row.name == "Bản đầu", "chế độ chỉ-tạo-mới không được sửa gì"


def test_create_and_update_rewrites_the_existing_row(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """H80: đường cập nhật tồn tại — tiêu chí "xuất rồi nhập lại, 0 bản ghi trùng"."""
    code = unique_code("KHO_UPD")
    run(WAREHOUSES, [_warehouse_row(code, "Tên cũ")], commit=True)
    before = _warehouses(session_factory, scope, [code])[code]

    report = run(
        WAREHOUSES,
        [_warehouse_row(code, "Tên mới")],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    assert report.is_valid and report.committed
    assert report.create_rows == 0 and report.update_rows == 1

    after = _warehouses(session_factory, scope, [code])[code]
    assert after.name == "Tên mới"
    assert after.id == before.id, "cập nhật, không phải tạo bản ghi thứ hai"
    assert after.row_version == before.row_version + 1, "FR-NFR-005: màn hình đang mở phải nhận 409"
    assert after.uid == before.uid, "RT-19: `uid` không đổi khi bản ghi được sửa"


def test_create_only_reports_zero_updates(run: RunImport) -> None:
    """Con số "sẽ cập nhật" phải mô tả thứ sắp xảy ra, không phải thứ truy vấn đếm được."""
    code = unique_code("KHO_CNT")
    run(WAREHOUSES, [_warehouse_row(code, "Bản đầu")], commit=True)
    report = run(WAREHOUSES, [_warehouse_row(code, "Bản sau")])
    assert report.update_rows == 0


def test_a_clean_file_reports_the_split_before_committing(run: RunImport) -> None:
    """H80: người dùng bấm ghi **vì** con số này, nên nó phải đúng lúc chưa ghi."""
    existing = unique_code("KHO_SPLIT")
    run(WAREHOUSES, [_warehouse_row(existing, "Đã có")], commit=True)

    report = run(
        WAREHOUSES,
        [_warehouse_row(existing, "Sửa"), _warehouse_row(unique_code("KHO_NEW"), "Mới")],
        mode=ImportMode.CREATE_AND_UPDATE,
    )
    assert report.is_valid and not report.committed
    assert report.create_rows == 1 and report.update_rows == 1


# ------------------------------------------- trường chốt một lần (H81) & cây con


def _item_row(
    code: str,
    name: str,
    *,
    nature: str | None = None,
    base_unit: str | None = None,
    warehouse: str | None = None,
    parent: str | None = None,
    is_group: str | None = None,
) -> list[object]:
    """Một dòng của tệp mẫu vật tư hàng hóa, theo đúng thứ tự cột."""
    return [code, name, None, parent, is_group, None, warehouse, None, nature, base_unit]


@pytest.fixture
def a_unit(run: RunImport) -> str:
    code = unique_code("DVT")
    run(UNITS, [[code, "Cái", None, None, None, None]], commit=True)
    return code


def test_a_create_only_field_cannot_be_changed_by_import(
    run: RunImport, a_unit: str, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """H81: bảng tính là đường ghi **thứ ba** vào `nature`/`base_unit_id`.

    H69 chặn đường sửa (trường vắng mặt trong thân request) và H73 chặn đường
    gộp. Nếu nhập liệu không bị chặn thì cả hai phép kiểm kia chỉ còn là nghi
    thức — đổi đơn vị chính của một mã hàng chỉ cần một lượt nhập ở chế độ cập
    nhật, và mọi tỷ lệ quy đổi đã khai sai lặng lẽ: con số giữ nguyên, nghĩa của
    nó đổi.
    """
    other_unit = unique_code("DVT2")
    run(UNITS, [[other_unit, "Thùng", None, None, None, None]], commit=True)

    code = unique_code("VT")
    created = run(
        ITEMS,
        [_item_row(code, "Vật tư", nature="goods", base_unit=a_unit)],
        commit=True,
    )
    assert created.is_valid and created.committed, created.errors

    report = run(
        ITEMS,
        [_item_row(code, "Vật tư", nature="goods", base_unit=other_unit)],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.create_only_field_changed"}
    assert not report.committed

    with unit_of_work(session_factory, scope) as session:
        item = session.scalar(select(Item).where(Item.code == code))
        assert item is not None
        unit = session.get(UnitOfMeasure, item.base_unit_id)
        assert unit is not None and unit.code == a_unit, "đơn vị chính không được đổi"


def test_an_unchanged_create_only_field_is_not_an_error(run: RunImport, a_unit: str) -> None:
    """Gửi lại **đúng** giá trị đang có thì không phải một lần đổi.

    Đây là ca của tiêu chí "xuất rồi nhập lại: 0 lỗi": tệp xuất ra mang cả cột
    chốt một lần, nên một phép kiểm ngây thơ ("cột này có mặt = lỗi") sẽ làm
    vòng khép kín xuất→nhập không bao giờ chạy được.
    """
    code = unique_code("VT_SAME")
    run(ITEMS, [_item_row(code, "Vật tư", nature="goods", base_unit=a_unit)], commit=True)
    report = run(
        ITEMS,
        [_item_row(code, "Tên mới", nature="goods", base_unit=a_unit)],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    assert report.is_valid, report.errors
    assert report.update_rows == 1


# ------------------------------------------------------------ hiệu năng (bước 15)


def test_ten_thousand_rows_import_within_the_budget(run: RunImport) -> None:
    """FR-NFR-043: 10.000 dòng ≤ 5 phút, có ngưỡng cứng trong CI.

    Ngưỡng cứng chứ không một phép đo ghi lại rồi thôi: bảng §Risk của phase 3
    gọi tên đúng cách hỏng — "import 10k dòng dùng ORM từng dòng → chậm gấp 20×",
    và nó là một **thói quen** chứ không một sự cố, nên thứ chặn được nó phải là
    một test đỏ chứ không một dòng ghi chú.

    Đo cả **kiểm lẫn ghi** trong một lượt: đó là thứ người dùng chờ. Con số thật
    trên máy lập trình cách ngưỡng rất xa, và chính khoảng cách đó khiến test này
    còn hữu ích khi ai đó thay SQL tập hợp bằng một vòng lặp.
    """
    rows = [
        _warehouse_row(f"PERF{index:05d}_{uuid.uuid4().hex[:4]}", f"Kho {index}")
        for index in range(10_000)
    ]
    started = time.monotonic()
    report = run(WAREHOUSES, rows, commit=True)
    elapsed = time.monotonic() - started

    assert report.is_valid and report.committed, report.errors[:5]
    assert report.create_rows == 10_000
    assert elapsed < 300, f"nhập 10.000 dòng mất {elapsed:.1f}s, ngưỡng FR-NFR-043 là 300s"


# --------------------------------------------- duyệt TOÀN BỘ registry (C4)


MINIMUM_VALID_CELLS: dict[str, dict[str, object]] = {
    # Ba danh mục có ràng buộc `CHECK` liên-trường mà một dòng "chỉ mã và tên"
    # không thỏa. Khai ở đây thay vì nới lỏng phép khẳng định: nếu phase 5 thêm
    # một danh mục có `CHECK` riêng thì test này đỏ, và người thêm phải nói ra
    # đâu là dòng hợp lệ tối thiểu của nó — đúng thứ người viết tệp mẫu cần biết.
    "partners": {"is_customer": "x"},  # CHECK (is_group OR is_customer OR is_vendor)
    "payment_terms": {"due_days": "0"},  # CHECK (due_days >= 0), NOT NULL
    "items": {"nature": "service"},  # dịch vụ: không cần đơn vị chính, không cần kho
}


def _minimal_row(spec: CatalogSpec) -> list[object]:
    """Dòng hợp lệ **tối thiểu** của một danh mục bất kỳ.

    Mã và tên cho mọi danh mục, cộng phần khai thêm ở `MINIMUM_VALID_CELLS` cho
    danh mục nào có ràng buộc liên-trường. Mọi ô còn lại để trống — đó là điều
    kiện để câu `INSERT` vẫn phải lập kế hoạch với **đầy đủ** cột, tức là vẫn
    bắt được lỗi ép kiểu dù giá trị rỗng.
    """
    descriptor = template_for(spec)
    extra = MINIMUM_VALID_CELLS.get(spec.slug, {})
    values: list[object] = [None] * len(descriptor.columns)
    for index, column in enumerate(descriptor.columns):
        if column.field == "code":
            values[index] = unique_code(f"SW{spec.slug[:5].upper()}")
        elif column.field == "name":
            values[index] = f"Bản ghi thử {spec.slug}"
        elif column.field in extra:
            values[index] = extra[column.field]
    return values


@pytest.mark.parametrize("slug", [spec.slug for spec in REGISTRY.specs()])
def test_every_registered_catalog_can_actually_be_imported(run: RunImport, slug: str) -> None:
    """Mọi danh mục trong registry phải nhập được — duyệt hết, không chọn mẫu.

    Test này tồn tại vì một lỗi cụ thể: `column_source` trả `text` cho mọi cột
    không phải khóa ngoại hay boolean, và PostgreSQL **không** ép ngầm
    `text → integer` ở vị trí gán. Câu lệnh vì thế hỏng **lúc lập kế hoạch** —
    không phụ thuộc dữ liệu, kể cả khi mọi ô của cột đó để trống — nên bốn danh
    mục (`asset_types`, `partners`, `payment_terms`, `tool_types`) không nhập
    được dòng nào, trong khi bước kiểm vẫn báo "hợp lệ".

    Bộ test đầu tiên của lát này chỉ chạm ba danh mục, và cả ba tình cờ không có
    cột số. Đó là đúng dạng điểm mù đã ghi ở lát 3B-1: **mã dùng chung cho N thực
    thể, test chỉ chạy vài hình dạng**. Tham số hóa từ chính registry là cách duy
    nhất khiến danh mục thêm ở phase 5 và phase 9 cũng được canh mà không ai phải
    nhớ thêm một dòng.
    """
    spec = _spec(slug)
    report = run(slug, [_minimal_row(spec)], commit=True)
    assert report.is_valid, report.errors
    assert report.committed and report.create_rows == 1


# ------------------------------------------- hồi quy cho các lỗi review bắt được


def test_a_parent_cycle_fails_loudly_instead_of_writing_nothing(run: RunImport) -> None:
    """C2: hai dòng trỏ cha vào nhau → job hỏng, **không** báo "đã tạo 2 dòng".

    Bản đầu thoát vòng lặp rồi đi tiếp, nên báo cáo nói `committed=True,
    create_rows=2` trong khi DB không nhận dòng nào — mất dữ liệu **im lặng**,
    thứ tệ nhất mà một khung nhập liệu có thể làm.

    Chu trình là câu hỏi về **đồ thị** nên bước kiểm theo dòng không bắt được;
    chỗ bắt nó là lúc ghi, và cách bắt là đếm phần việc còn lại thay vì tin
    `rowcount` (giá trị đó là `-1` cho `INSERT ... SELECT`).
    """
    left, right = unique_code("CYC_A"), unique_code("CYC_B")
    with pytest.raises(ImportParentUnresolvableError):
        run(
            WAREHOUSES,
            [
                _warehouse_row(left, "Kho A", right),
                _warehouse_row(right, "Kho B", left),
            ],
            commit=True,
        )


def test_a_row_that_is_its_own_parent_fails_loudly(run: RunImport) -> None:
    """Cùng gốc với chu trình, chỉ ngắn hơn: một dòng trỏ vào chính mã của nó."""
    code = unique_code("SELF")
    with pytest.raises(ImportParentUnresolvableError):
        run(WAREHOUSES, [_warehouse_row(code, "Kho tự trỏ", code)], commit=True)


def test_a_branch_import_never_touches_a_company_shared_record(
    run: RunImport,
    session_factory: sessionmaker[Session],
    scope: RequestScope,
    a_branch: int,
) -> None:
    """C3/H5: phạm vi **ghi** là chi nhánh đang thao tác, không phải phạm vi đọc.

    `master_data_table_args` cố ý cho phép mã `X` dùng chung tồn tại song song
    `X` riêng của một chi nhánh (hai chỉ mục duy nhất tách theo điều kiện). Với
    `visible_to` làm điều kiện ghi, một ô mã khớp **cả hai** dòng và câu `UPDATE`
    sửa cả hai — lượt nhập của một chi nhánh ghi đè dữ liệu của cả công ty. Bảng
    danh mục không có RLS (H39) nên không có lớp nào khác chặn lại.
    """
    code = unique_code("KHO_SHARED")
    run(WAREHOUSES, [_warehouse_row(code, "Bản dùng chung")], commit=True)

    report = run(
        WAREHOUSES,
        [_warehouse_row(code, "Bản của chi nhánh")],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
        branch_id=a_branch,
    )
    assert report.is_valid, report.errors
    # Mã đã có ở phần dùng chung **không** phải "mã đã tồn tại" với chi nhánh:
    # nó tạo bản ghi riêng, đúng bằng thứ `POST /master/{slug}` vẫn cho phép.
    assert report.create_rows == 1 and report.update_rows == 0

    with unit_of_work(session_factory, scope) as session:
        rows = {
            row.branch_id: row.name
            for row in session.scalars(select(Warehouse).where(Warehouse.code == code)).all()
        }
    assert rows[None] == "Bản dùng chung", "bản dùng chung không được đụng tới"
    assert rows[a_branch] == "Bản của chi nhánh"


def test_is_group_cannot_be_flipped_by_an_import(run: RunImport) -> None:
    """H88/H1: `is_group` chốt một lần, y như `nature` và `base_unit_id`.

    Bật cờ này trên một nút đang có con biến một nhóm thành nút hạch toán được
    **mà vẫn có nhánh con** — trạng thái không đường ghi nào khác tạo ra nổi, và
    là đúng thứ `ensure_postable` của posting engine (phase 4) dựa vào.
    """
    code = unique_code("GRP")
    run(WAREHOUSES, [[code, "Nhóm kho", None, None, "x", None]], commit=True)
    # Ô ghi rõ "không" — chứ không để trống. Ô trống nghĩa là "không nói gì về
    # cột này" (H87) và vì thế đúng ra **không** phải một lần đổi.
    report = run(
        WAREHOUSES,
        [[code, "Nhóm kho", None, None, "không", None]],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.create_only_field_changed"}


def test_a_blank_cell_keeps_the_existing_value(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """H87: ô trống nghĩa là "tôi không nói gì về cột này", không phải "hãy xóa".

    Vòng làm việc thường gặp nhất là xuất ra, sửa vài cột, nhập lại — với cách
    hiểu ngược lại thì một lần dán hụt vài cột xóa trắng hàng nghìn bản ghi mà
    không có cảnh báo nào.
    """
    code = unique_code("KHO_EN")
    run(WAREHOUSES, [[code, "Kho", "Warehouse", None, None, None]], commit=True)
    run(
        WAREHOUSES,
        [[code, "Kho đổi tên", None, None, None, None]],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    row = _warehouses(session_factory, scope, [code])[code]
    assert row.name == "Kho đổi tên"
    assert row.name_en == "Warehouse", "ô để trống không được xóa giá trị đang có"


def test_a_blank_not_null_column_falls_back_to_its_default(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Cột `NOT NULL` có ngầm định: ô trống rơi về ngầm định, không ghi `NULL`.

    Câu `INSERT` liệt kê mọi cột tường minh nên một ô trống **đè lên** `DEFAULT`
    của cột — từ khóa `DEFAULT` không dùng được ở vị trí biểu thức trong `SELECT`.
    `payment_terms.discount_days` khai `NOT NULL default 0`, nên trước khi sửa,
    mọi lượt nhập điều khoản thanh toán bỏ trống cột đó đều đổ với
    `NotNullViolation` sau khi bước kiểm đã báo "hợp lệ".
    """
    code = unique_code("DK")
    spec = _spec("payment_terms")
    row_values = _minimal_row(spec)
    row_values[0] = code
    report = run("payment_terms", [row_values], commit=True)
    assert report.is_valid and report.committed, report.errors
    with unit_of_work(session_factory, scope) as session:
        row = session.scalar(select(PaymentTerm).where(PaymentTerm.code == code))
    assert row is not None
    assert row.discount_days == 0 and row.due_days == 0


def _validate_params() -> dict[str, object]:
    return {
        "catalog": "payment_terms",
        "content_hash": "0" * 64,
        "file_name": "x.xlsx",
        "mode": ImportMode.CREATE_ONLY.value,
    }


def test_commit_refuses_a_validation_job_of_a_different_catalog(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """C1: leo quyền `view` → `create` giữa các danh mục.

    Endpoint `/master/{slug}/import/commit` xét quyền tạo trên `{slug}`, còn thân
    job đọc danh mục từ **báo cáo của lượt kiểm**. Không có phép so giữa hai giá
    trị ấy thì ai có quyền *xem* một danh mục (đủ để chạy lượt kiểm) cộng quyền
    *tạo* trên **một** danh mục bất kỳ sẽ ghi được vào mọi danh mục còn lại.

    Đo ở tầng job chứ không tầng HTTP: đây là chỗ hai giá trị gặp nhau, và tầng
    HTTP thì cần một worker chạy xong lượt kiểm mới dựng được tình huống.
    """
    validated = ImportReport(
        catalog="payment_terms",
        mode=ImportMode.CREATE_ONLY,
        file_name="x.xlsx",
        content_hash="0" * 64,
        total_rows=1,
        create_rows=1,
    )
    with unit_of_work(session_factory, scope) as session:
        job = queue.enqueue(
            session, job_type=VALIDATE_IMPORT, params=_validate_params(), requested_by=1
        )
        job.status = JobStatus.DONE.value
        job.result = dict(validated.model_dump(mode="json"))
        job_id = job.id

    with unit_of_work(session_factory, scope) as session:
        context = JobContext(
            job_id=job_id,
            session=session,
            progress=FakeProgress(reports=[]),
            attempt=1,
            dataset_schema=dataset_alpha.schema_name,
            branch_id=None,
            requested_by=1,
            storage_root=tmp_path,
        )
        with pytest.raises(ImportSourceNotValidatedError):
            run_commit(context, ImportCommitParams(validation_job_id=job_id, catalog="warehouses"))


# ------------------------------------------------ hồi quy vòng 2 của review


def test_an_unknown_enum_value_is_refused_before_it_poisons_the_catalog(
    run: RunImport, a_unit: str
) -> None:
    """R2-1: một ô `Tính chất` gõ sai làm hỏng cả màn hình danh mục.

    Cột lưu ở `varchar` nên giá trị lạ **ghi được**, nhưng SQLAlchemy đọc nó
    thành `ItemNature` — sau đấy mọi lượt đọc `Item` qua ORM ném `LookupError`,
    `GET /api/v1/master/items` hỏng cho cả dữ liệu kế toán, và bản ghi sai không
    sửa hay xóa được từ giao diện vì mọi đường sửa cũng phải đọc nó lên trước.

    Hai lớp chặn nay: câu tiếng Việt ở bước kiểm (đo ở đây) và `CHECK` dưới DB
    (migration `0007`) cho những đường ghi không qua khung này.
    """
    report = run(
        ITEMS,
        [_item_row(unique_code("VT_ENUM"), "Vật tư", nature="khong_ton_tai", base_unit=a_unit)],
        commit=True,
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.value_not_allowed"}
    assert not report.committed


def test_a_blank_cell_does_not_reset_a_not_null_column_to_its_default(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """R2-2: bản sửa cho cột `NOT NULL` đã phá chính H87 ở đúng những cột đó.

    `keep_existing` bọc `coalesce(..., cột)` **quanh** `column_source`, mà cái sau
    cho cột `NOT NULL` đã là `coalesce(ô, ngầm_định)` — không bao giờ trả `NULL`,
    nên lớp ngoài là mã chết và ô để trống **đặt lại** cột về ngầm định:
    `due_days` 30 → 0.

    Ngầm định của cột là câu trả lời đúng cho một dòng **mới**; với dòng đã tồn
    tại thì câu trả lời đúng là giá trị nó đang mang.
    """
    spec = _spec("payment_terms")
    descriptor = template_for(spec)
    code = unique_code("DK_KEEP")

    filled = _minimal_row(spec)
    filled[0] = code
    for index, column in enumerate(descriptor.columns):
        if column.field == "due_days":
            filled[index] = "30"
        elif column.field == "discount_days":
            filled[index] = "10"
    assert run("payment_terms", [filled], commit=True).committed

    blank = _minimal_row(spec)
    blank[0] = code
    for index, column in enumerate(descriptor.columns):
        if column.field in {"due_days", "discount_days"}:
            blank[index] = None
        elif column.field == "name":
            blank[index] = "Tên mới"
    report = run("payment_terms", [blank], mode=ImportMode.CREATE_AND_UPDATE, commit=True)
    assert report.is_valid, report.errors

    with unit_of_work(session_factory, scope) as session:
        row = session.scalar(select(PaymentTerm).where(PaymentTerm.code == code))
    assert row is not None
    assert row.name == "Tên mới"
    assert (row.due_days, row.discount_days) == (30, 10), "ô trống không được đặt lại về ngầm định"


def test_a_blank_active_cell_does_not_revive_a_stopped_record(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Ô "Còn theo dõi" để trống ở đường **sửa** không được bật lại bản ghi đã ngừng.

    Trước bản sửa, ô trống nghĩa là `true` ở cả hai đường, nên một tệp dán hụt cột
    đó **bật lại** mọi bản ghi ai đó đã cho ngừng theo dõi — im lặng, và đúng thứ
    FR-SYS-012 dựng ra để người dùng chủ động rút một mã khỏi danh sách chọn.
    """
    code = unique_code("KHO_STOP")
    run(WAREHOUSES, [[code, "Kho", None, None, None, "không"]], commit=True)
    assert _warehouses(session_factory, scope, [code])[code].is_active is False

    run(
        WAREHOUSES,
        [[code, "Kho đổi tên", None, None, None, None]],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    row = _warehouses(session_factory, scope, [code])[code]
    assert row.name == "Kho đổi tên"
    assert row.is_active is False, "ô để trống không được bật lại bản ghi đã ngừng theo dõi"


def test_an_over_long_value_never_reaches_the_database_truncated(
    run: RunImport, session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """R2-3: `CAST(x AS varchar(n))` **cắt** thay vì nổ — và cắt xong thì lọt cả `CHECK`.

    `banks.swift_code` là `VARCHAR(11)` với `CHECK (length IN (8, 11))`. Một giá
    trị 20 ký tự bị cắt còn 11 sẽ **thỏa** ràng buộc ấy, nên dữ liệu sai đi thẳng
    vào DB mà không tầng nào kêu. Nay bước kiểm bắt nó bằng trần đọc từ chính cột.
    """
    spec = _spec("banks")
    descriptor = template_for(spec)
    row = _minimal_row(spec)
    row[0] = unique_code("NH")
    for index, column in enumerate(descriptor.columns):
        if column.field == "swift_code":
            row[index] = "A" * 20
    report = run("banks", [row], commit=True)

    assert not report.is_valid
    assert "import.too_long" in {error.code for error in report.errors}
    with unit_of_work(session_factory, scope) as session:
        assert session.scalar(select(Bank).where(Bank.code == row[0])) is None
