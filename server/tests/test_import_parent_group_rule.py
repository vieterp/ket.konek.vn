"""Mã nhóm cha phải trỏ vào một **nhóm** — luật chung của mọi đường ghi cây.

`MasterDataService._ensure_parent_is_group` chặn đường API (xem
`test_master_data_tree.py`); nhập Excel không đi qua service nên có phép kiểm
riêng trong `staging._parent_not_group_errors`. Bộ test này đo đường nhập, cả
hai lối phân giải của mã nhóm cha: đã có trong danh mục, và khai trong chính tệp.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import unique_code
from import_support import import_source, spec_of, workbook_bytes
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.report import ImportReport
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

SLUG = "warehouses"


@pytest.fixture
def scope(dataset_alpha: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())


def _row(
    code: str, name: str, *, parent_code: str | None = None, is_group: str | None = None
) -> list[object]:
    descriptor = template_for(spec_of(SLUG))
    values: list[object] = [None] * len(descriptor.columns)
    for index, column in enumerate(descriptor.columns):
        if column.field == "code":
            values[index] = code
        elif column.field == "name":
            values[index] = name
        elif column.field == "parent_code":
            values[index] = parent_code
        elif column.field == "is_group":
            values[index] = is_group
    return values


def _import(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
    rows: list[list[object]],
) -> ImportReport:
    return import_source(
        session_factory, dataset, scope, tmp_path, SLUG, workbook_bytes(SLUG, rows)
    )


def test_a_parent_code_that_is_a_leaf_in_the_catalog_is_an_error(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    leaf_code = unique_code("KHOLA")
    with unit_of_work(session_factory, scope) as session:
        MasterDataService(session, spec_of(SLUG).model).create(
            code=leaf_code, name="Kho lá", is_group=False
        )

    report = _import(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [_row(unique_code("KHO"), "Kho con", parent_code=leaf_code)],
    )

    assert report.error_rows == 1
    assert [error.code for error in report.errors] == ["import.parent_not_group"]


def test_a_parent_declared_in_the_file_must_be_marked_as_a_group(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Dòng khai mã cha trong tệp nhưng bỏ trống cột "Là nhóm" → lỗi ở dòng con.

    Ô trống nghĩa là "không phải nhóm" — cùng cách đọc với `boolean_expression`
    của bước ghi, nên phép kiểm và câu `INSERT` không thể lệch nhau.
    """
    parent_code = unique_code("CHA")
    child_code = unique_code("CON")

    report = _import(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [
            _row(parent_code, "Cha nhưng là lá", is_group=None),
            _row(child_code, "Con", parent_code=parent_code),
        ],
    )

    assert report.error_rows == 1
    assert [error.code for error in report.errors] == ["import.parent_not_group"]


def test_a_real_group_parent_passes_both_resolutions(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Đối chứng dương: nhóm trong danh mục lẫn nhóm khai trong tệp đều hợp lệ."""
    group_in_catalog = unique_code("NHOMDB")
    with unit_of_work(session_factory, scope) as session:
        MasterDataService(session, spec_of(SLUG).model).create(
            code=group_in_catalog, name="Nhóm sẵn có", is_group=True
        )

    group_in_file = unique_code("NHOMTEP")
    report = _import(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        [
            _row(group_in_file, "Nhóm trong tệp", is_group="x"),
            _row(unique_code("KHO"), "Con của nhóm sẵn có", parent_code=group_in_catalog),
            _row(unique_code("KHO"), "Con của nhóm trong tệp", parent_code=group_in_file),
        ],
    )

    assert report.error_rows == 0, [error.message for error in report.errors]
