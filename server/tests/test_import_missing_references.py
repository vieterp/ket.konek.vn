"""Tự tạo danh mục còn thiếu khi nhập liệu (lát 3C-2, FR-NFR-062, nợ L1 của 3C-1).

Lát 3C-1 **từ chối** giao tính năng này, và lý do nó nêu vẫn đúng nguyên: một
lượt nhập vật tư tự tạo ra đơn vị tính và kho là ghi vào **danh mục khác** với
dữ liệu duy nhất là một cái mã — và với chính vật tư hàng hóa thì bản ghi sinh ra
như vậy còn không hợp lệ (H76). Nó cũng đi vòng qua lớp quyền per-danh-mục của H48.

Lát này giao, nhưng chỉ sau khi dựng ba hàng rào đúng vào ba lý do ấy. Tệp test
chia theo ba hàng rào đó, cộng một nhóm cho bất biến "bước kiểm không ghi gì".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import branch_ids, ensure_branches, unique_code
from import_support import (
    LAST_JOB_ID,
    import_source,
    minimal_row,
    spec_of,
    workbook_bytes,
)
from ket.kernel.auditing.models import AuditAction, AuditLog
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.report import ImportReport, MissingReferenceMode
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.models.warehouse import Warehouse
from ket.kernel.master_data.registry import REGISTRY
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

ITEMS = "items"
UNITS = "units_of_measure"
WAREHOUSES = "warehouses"

BRANCH_FOR_AUTOCREATE = "CN_TUTAO"
BRANCH_OTHER = "CN_TUTAO_KHAC"


@pytest.fixture
def scope(dataset_alpha: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())


def _item_row(code: str, *, unit: str | None = None, warehouse: str | None = None) -> list[object]:
    spec = spec_of(ITEMS)
    descriptor = template_for(spec)
    row = minimal_row(spec, code, f"Vật tư {code}")
    for index, column in enumerate(descriptor.columns):
        if column.field == "nature":
            row[index] = "goods"
        elif column.field == "base_unit_code":
            row[index] = unit
        elif column.field == "warehouse_code":
            row[index] = warehouse
    return row


def _run(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
    rows: list[list[object]],
    *,
    missing_reference: MissingReferenceMode = MissingReferenceMode.ERROR,
    allow_create_in: frozenset[str] = frozenset(),
    commit: bool = False,
) -> ImportReport:
    return import_source(
        session_factory,
        dataset,
        scope,
        tmp_path,
        ITEMS,
        workbook_bytes(ITEMS, rows),
        commit=commit,
        missing_reference=missing_reference,
        allow_create_in=allow_create_in,
    )


# ------------------------------------- hàng rào 1: người dùng chọn tường minh


def test_a_missing_code_is_still_an_error_by_default(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Mặc định **không** tự tạo — cùng lý do H80 đặt cho chế độ ghi đè.

    Ghi vào một danh mục khác danh mục người dùng đang mở là thứ họ phải chọn,
    không phải thứ xảy ra vì họ quên khai một mã.
    """
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(unique_code("VT_M"), unit=unique_code("DVT_THIEU"))],
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.reference_not_found"}
    assert report.created_references == {}


def test_the_validation_pass_counts_what_it_would_create_without_creating_it(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """FR-SYS-081: bước kiểm **không ghi gì** — kể cả ở chế độ tự tạo.

    Đây là bất biến trung tâm của cả khung nhập liệu, và tự tạo là đường dễ phá
    nó nhất: chỉ cần gọi nhầm `create_missing` thay vì `count_missing` ở một
    nhánh. Con số vẫn phải có, vì nó chính là thứ người dùng đọc trước khi bấm
    ghi ("sẽ tạo thêm 2 đơn vị tính").
    """
    unit = unique_code("DVT_DEM")
    warehouse = unique_code("KHO_DEM")
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(unique_code("VT_DEM"), unit=unit, warehouse=warehouse)],
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS, WAREHOUSES}),
    )
    assert report.is_valid, report.errors
    assert report.created_references == {UNITS: 1, WAREHOUSES: 1}
    assert not report.committed

    with unit_of_work(session_factory, scope) as session:
        assert session.scalar(select(UnitOfMeasure).where(UnitOfMeasure.code == unit)) is None
        assert session.scalar(select(Warehouse).where(Warehouse.code == warehouse)) is None


def test_committing_creates_the_missing_records_and_links_them(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Bản ghi sinh ra phải **dùng được ngay**: đúng mã, tên tạm, và được trỏ tới.

    Khóa ngoại là phần dễ hụt nhất: tạo xong danh mục đích mà câu `INSERT` của
    bảng chính vẫn tra không ra thì mọi dòng thành lỗi "không tìm thấy mã" ở đúng
    lượt vừa tạo chúng.
    """
    unit = unique_code("DVT_TAO")
    code = unique_code("VT_TAO")
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(code, unit=unit)],
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
        commit=True,
    )
    assert report.is_valid and report.committed, report.errors
    assert report.created_references == {UNITS: 1}

    with unit_of_work(session_factory, scope) as session:
        created = session.scalar(select(UnitOfMeasure).where(UnitOfMeasure.code == unit))
        assert created is not None
        assert created.name == unit, "tên đặt bằng chính mã để nhìn là biết cần đặt lại"
        assert created.is_active and not created.is_group
        assert created.path == f"{created.id}.", "bản ghi sinh ra phải là nút gốc hợp lệ"
        item = session.scalar(select(spec_of(ITEMS).model).where(spec_of(ITEMS).model.code == code))
        assert item is not None
        assert item.base_unit_id == created.id


def test_the_same_missing_code_on_many_rows_creates_one_record(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Một tệp 10.000 dòng nhắc cùng một đơn vị tính hàng nghìn lần — vẫn một bản ghi."""
    unit = unique_code("DVT_LAP")
    rows = [_item_row(unique_code(f"VT_L{index}"), unit=unit) for index in range(3)]
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        rows,
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
        commit=True,
    )
    assert report.created_references == {UNITS: 1}
    with unit_of_work(session_factory, scope) as session:
        found = session.scalars(select(UnitOfMeasure).where(UnitOfMeasure.code == unit)).all()
    assert len(found) == 1


# --------------------------- hàng rào 2: chỉ danh mục tự tạo được


def test_which_catalogs_are_auto_creatable_is_pinned() -> None:
    """Ghim danh sách hiện tại — không phải vì con số, mà vì **chiều trôi**.

    `auto_creatable` suy từ registry: không cột riêng bắt buộc, và không luật
    liên-trường. Cả hai điều kiện chỉ siết chặt thêm theo thời gian, nên một
    danh mục **thôi** tự tạo được là thay đổi vô hại còn một danh mục **bắt đầu**
    tự tạo được là thứ phải có người nhìn.

    Vật tư hàng hóa và đối tác nằm ở phía "không", và đó chính là hai ca mà lát
    3C-1 nêu ra khi từ chối giao tính năng này.
    """
    auto = {spec.slug for spec in REGISTRY.specs() if spec.auto_creatable}
    assert UNITS in auto and WAREHOUSES in auto
    assert ITEMS not in auto, "vật tư hàng hóa cần tính chất và đơn vị chính (H76)"
    assert "partners" not in auto, "đối tác phải là khách hoặc NCC, nếu không nó vô hình"


def test_a_missing_code_in_a_catalog_that_cannot_be_auto_created_stays_an_error(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Xin tự tạo vào một danh mục không tự tạo được thì **không** im lặng bỏ qua.

    Người dùng phải thấy đúng thứ họ còn thiếu, chứ không phải một lượt nhập
    "thành công" đã lặng lẽ bỏ rơi một nửa yêu cầu của họ.
    """
    spec = spec_of("partners")
    descriptor = template_for(spec)
    row = minimal_row(spec, unique_code("DT_M"), "Đối tác thử")
    missing_term = unique_code("DKTT_THIEU")
    for index, column in enumerate(descriptor.columns):
        if column.field == "payment_term_code":
            row[index] = missing_term
    report = import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        "partners",
        workbook_bytes("partners", [row]),
        commit=True,
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({"payment_terms"}),
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.reference_not_found"}


def test_a_missing_parent_code_is_never_auto_created(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Mã nhóm cha gõ sai phải là lỗi, không thành một nút rác.

    Nhóm cha trỏ về **chính** danh mục đang nhập, và tệp vốn được phép khai cả
    cây trong một lượt. Tự tạo ở đó biến mọi lỗi chính tả trong cột nhóm cha
    thành một nút mới nằm ở cấp gốc — và cây danh mục thì không ai đọc lại từng
    nút để phát hiện.
    """
    spec = spec_of(WAREHOUSES)
    row = minimal_row(spec, unique_code("KHO_C"), "Kho con")
    descriptor = template_for(spec)
    for index, column in enumerate(descriptor.columns):
        if column.field == "parent_code":
            row[index] = unique_code("KHO_CHA_SAI")
    report = import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        WAREHOUSES,
        workbook_bytes(WAREHOUSES, [row]),
        commit=True,
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({WAREHOUSES}),
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.reference_not_found"}
    # Con số trong báo cáo không được mâu thuẫn với danh sách lỗi ngay cạnh nó
    # (review vòng 2, L3): `count_missing` chạy trên cột nhóm cha sẽ hứa "sẽ tạo
    # thêm 1 kho" cho đúng cái mã vừa bị báo là không tìm thấy.
    assert report.created_references == {}


# ------------------------------- hàng rào 3: quyền trên từng danh mục đích


def test_a_catalog_the_endpoint_did_not_authorise_is_not_written_to(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Thân job chỉ tạo trong danh sách endpoint đã duyệt (khuôn H89).

    Không có hàng rào này thì tự tạo là một đường vòng qua lớp quyền
    per-danh-mục mà H48 dựng ra: ai được nhập vật tư sẽ ghi được vào danh mục kho
    mà không cần quyền nào trên kho. Ở đây `allow_create_in` chỉ có đơn vị tính,
    nên mã kho còn thiếu vẫn phải là dòng lỗi.
    """
    unit = unique_code("DVT_Q")
    warehouse = unique_code("KHO_Q")
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(unique_code("VT_Q"), unit=unit, warehouse=warehouse)],
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
        commit=True,
    )
    assert not report.is_valid
    assert {error.code for error in report.errors} == {"import.reference_not_found"}
    with unit_of_work(session_factory, scope) as session:
        assert session.scalar(select(Warehouse).where(Warehouse.code == warehouse)) is None
        assert session.scalar(select(UnitOfMeasure).where(UnitOfMeasure.code == unit)) is None, (
            "một lượt nhập còn dòng lỗi thì không được để lại bản ghi nào"
        )


def test_nothing_is_created_when_the_mode_is_error_even_if_allowed(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Danh sách đã duyệt **không** tự nó bật tính năng — chế độ mới bật.

    Hai điều kiện độc lập, và endpoint luôn gửi danh sách đã duyệt kèm mọi lượt
    nhập. Nếu chỉ danh sách là đủ thì mọi lượt nhập của một kế toán trưởng (người
    có quyền trên mọi danh mục) sẽ lặng lẽ tự tạo.
    """
    unit = unique_code("DVT_TAT")
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(unique_code("VT_TAT"), unit=unit)],
        allow_create_in=frozenset({UNITS, WAREHOUSES}),
        commit=True,
    )
    assert not report.is_valid
    assert report.created_references == {}
    with unit_of_work(session_factory, scope) as session:
        assert session.scalar(select(UnitOfMeasure).where(UnitOfMeasure.code == unit)) is None


def test_an_existing_code_is_not_created_twice(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Mã đã có thì không tạo thêm — kể cả khi chế độ tự tạo đang bật."""
    unit = unique_code("DVT_CO")
    assert import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        UNITS,
        workbook_bytes(UNITS, [minimal_row(spec_of(UNITS), unit, "Cái")]),
        commit=True,
    ).committed
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(unique_code("VT_CO"), unit=unit)],
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
        commit=True,
    )
    assert report.is_valid and report.committed, report.errors
    assert report.created_references == {}
    with unit_of_work(session_factory, scope) as session:
        found = session.scalars(select(UnitOfMeasure).where(UnitOfMeasure.code == unit)).all()
    assert len(found) == 1


def test_auto_created_records_roll_back_with_the_rest_of_a_failed_import(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Cùng transaction: lượt nhập đổ thì đơn vị tính vừa tạo cũng biến mất.

    Không có tính chất này thì một lượt nhập thất bại để lại rác trong một danh
    mục **khác** — thứ người dùng không nghĩ tới việc đi dọn, vì họ chỉ nhớ mình
    vừa nhập vật tư.

    Dựng ca đổ bằng chu trình `parent_code` (`ImportParentUnresolvableError`):
    nó là lỗi ném ra **sau** khi các bản ghi tra cứu đã được tạo, tức đúng cửa sổ
    cần đo.
    """
    unit = unique_code("DVT_ROLL")
    left, right = unique_code("VT_CYC_A"), unique_code("VT_CYC_B")
    descriptor = template_for(spec_of(ITEMS))
    rows = []
    for code, parent in ((left, right), (right, left)):
        row = _item_row(code, unit=unit)
        for index, column in enumerate(descriptor.columns):
            if column.field == "parent_code":
                row[index] = parent
        rows.append(row)

    with pytest.raises(Exception):  # noqa: B017 — lớp lỗi đã có test riêng ở pipeline
        _run(
            session_factory,
            dataset_alpha,
            scope,
            tmp_path,
            rows,
            missing_reference=MissingReferenceMode.CREATE,
            allow_create_in=frozenset({UNITS}),
            commit=True,
        )
    with unit_of_work(session_factory, scope) as session:
        assert session.scalar(select(UnitOfMeasure).where(UnitOfMeasure.code == unit)) is None


@pytest.mark.db
def test_an_auto_created_record_belongs_to_the_importing_branch(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Bản ghi tự tạo mang chi nhánh của người nhập, không phải phần dùng chung.

    Review vòng 2, M5: đột biến `literal(branch_id)` → `literal(None)` sống sót.
    Hậu quả nếu nó là thật: kế toán **chi nhánh** bấm nhập vật tư và lặng lẽ tạo
    ra đơn vị tính **dùng chung toàn công ty** — mọi chi nhánh khác nhìn thấy, và
    không ai sửa được từ chi nhánh (chính luật `_shared_record_errors` vừa thêm
    ở lát này sẽ chặn họ).
    """
    ensure_branches(session_factory, dataset_alpha, [BRANCH_FOR_AUTOCREATE])
    branch = branch_ids(session_factory, dataset_alpha, [BRANCH_FOR_AUTOCREATE])[
        BRANCH_FOR_AUTOCREATE
    ]
    unit = unique_code("DVT_CN")
    report = import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        ITEMS,
        workbook_bytes(ITEMS, [_item_row(unique_code("VT_CN"), unit=unit)]),
        commit=True,
        branch_id=branch,
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
    )
    assert report.is_valid and report.committed, report.errors

    with unit_of_work(session_factory, scope) as session:
        created = session.scalar(select(UnitOfMeasure).where(UnitOfMeasure.code == unit))
    assert created is not None
    assert created.branch_id == branch, "đơn vị tính tự tạo rơi vào phần dùng chung toàn công ty"


@pytest.mark.db
def test_a_code_owned_by_another_branch_is_still_created_for_this_one(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Mã của chi nhánh **khác** không phải là "đã có" với chi nhánh này.

    Review vòng 2, M5: đột biến bỏ `visible_to` khỏi `_missing_codes` sống sót.
    Hậu quả nếu nó là thật: mã do chi nhánh khác sở hữu bị coi là đã có → không
    tạo → nhưng `reference_id` (vẫn lọc `visible_to`) tra ra `NULL`, và vì lỗi
    tra cứu **bị bỏ qua** ở chế độ tự tạo, dòng qua bước kiểm sạch rồi bước ghi
    đổ ở ràng buộc `NOT NULL`.
    """
    ensure_branches(session_factory, dataset_alpha, [BRANCH_FOR_AUTOCREATE, BRANCH_OTHER])
    ids = branch_ids(session_factory, dataset_alpha, [BRANCH_FOR_AUTOCREATE, BRANCH_OTHER])
    unit = unique_code("DVT_CN2")
    assert import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        UNITS,
        workbook_bytes(UNITS, [minimal_row(spec_of(UNITS), unit, "Cái của chi nhánh kia")]),
        commit=True,
        branch_id=ids[BRANCH_OTHER],
    ).committed

    report = import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        ITEMS,
        workbook_bytes(ITEMS, [_item_row(unique_code("VT_CN2"), unit=unit)]),
        commit=True,
        branch_id=ids[BRANCH_FOR_AUTOCREATE],
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
    )
    assert report.is_valid and report.committed, report.errors
    assert report.created_references == {UNITS: 1}

    with unit_of_work(session_factory, scope) as session:
        owners = {
            row.branch_id
            for row in session.scalars(
                select(UnitOfMeasure).where(UnitOfMeasure.code == unit)
            ).all()
        }
    assert owners == {ids[BRANCH_OTHER], ids[BRANCH_FOR_AUTOCREATE]}


@pytest.mark.db
def test_auto_created_records_leave_an_audit_trail_on_their_own_catalog(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """ "Ai tạo đơn vị tính này, lúc nào" phải có câu trả lời (review vòng 2, M3).

    Đường ghi của `create_missing` là `INSERT ... SELECT` qua Core nên hai móc
    dựng `audit_log` không chạy, và dòng `IMPORTED` của H84 mang `entity_type`
    của danh mục **chính** — nên màn hình lịch sử của `units_of_measure` không
    thấy gì. Đây là đường ghi duy nhất trong dự án tạo bản ghi danh mục mà không
    để lại vết, đúng ngược lý do H84 tồn tại.
    """
    unit = unique_code("DVT_VET")
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_item_row(unique_code("VT_VET"), unit=unit)],
        missing_reference=MissingReferenceMode.CREATE,
        allow_create_in=frozenset({UNITS}),
        commit=True,
    )
    assert report.is_valid and report.committed, report.errors

    # Lọc theo **chính lượt nhập này**, không lấy "dòng mới nhất": `audit_log`
    # dùng chung cả phiên và `_record_import` cũng ghi dòng `IMPORTED` mang
    # `entity_type='units_of_measure'` mỗi lần một test khác nhập danh mục ấy.
    # Bản đầu lấy `entries[-1]` không `ORDER BY` — nó xanh nhờ **thứ tự thu thập
    # tệp test**, và đỏ tái hiện được khi đổi thứ tự (R3-H4).
    job_id = LAST_JOB_ID[-1]
    with unit_of_work(session_factory, scope) as session:
        entries = session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "units_of_measure")
            .where(AuditLog.action == AuditAction.IMPORTED.value)
            .where(AuditLog.entity_id == str(job_id))
            .order_by(AuditLog.id)
        ).all()
    assert entries, "danh mục đích không có dòng nhật ký nào cho lượt tự tạo"
    (entry,) = entries
    assert entry.new_values is not None
    assert entry.new_values["created_by_import_of"] == ITEMS
    assert entry.new_values["created_rows"] == 1
