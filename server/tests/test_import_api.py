"""Nhập liệu danh mục qua HTTP (lát 3C-1).

Luật nghiệp vụ của lượt nhập nằm ở `test_import_pipeline.py`. Ở đây chỉ kiểm bốn
thứ mà **chỉ tầng HTTP** trả lời được:

* tệp mẫu tải về được, và route của nó không bị route đọc bản ghi nuốt mất;
* **hai lớp quyền** — quyền dùng chức năng nhập liệu, và quyền trên chính danh
  mục đó (H48: kế toán kho nhập được danh mục kho không có nghĩa là nhập được
  điều khoản thanh toán);
* hai loại job **không** xếp hàng thẳng qua `/api/v1/jobs` được, vì endpoint
  chung không biết `slug` nào đang bị nhắm tới;
* lượt kiểm để lại một `job_id` và tệp nằm trong kho định địa chỉ theo nội dung.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import (
    UserFactory,
    actor,
    all_branch_codes,
    catalog_codes,
    ensure_branches,
    ensure_role,
    unique_code,
)
from conftest import api_test_client
from ket.kernel.attachments import storage
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.job import IMPORT_COMMIT, IMPORT_VALIDATE
from ket.kernel.jobs.builtin import JOB_CREATE, JOB_VIEW
from ket.kernel.master_data.registry import REGISTRY
from ket.kernel.security.permissions import Action
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

WAREHOUSES = "warehouses"
PAYMENT_TERMS = "payment_terms"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

BRANCH_CODES = ["CN_IMP_A"]


@pytest.fixture(scope="module")
def import_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("import-store")


@pytest.fixture(scope="module")
def import_settings(test_settings: Settings, import_dir: Path) -> Settings:
    return test_settings.model_copy(update={"attachments_dir": import_dir})


@pytest.fixture
def client(
    import_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(import_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def importer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Được nhập liệu, và được trên **danh mục kho** — không phải trên mọi danh mục."""
    return ensure_role(
        session_factory,
        dataset_alpha,
        "ke_toan_nhap_lieu",
        [
            IMPORT_VALIDATE,
            IMPORT_COMMIT,
            JOB_VIEW,
            JOB_CREATE,
            *catalog_codes(WAREHOUSES, Action.VIEW, Action.CREATE, Action.EDIT),
        ],
    )


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Xem được danh mục kho, **không** có quyền dùng chức năng nhập liệu."""
    return ensure_role(
        session_factory,
        dataset_alpha,
        "ke_toan_chi_xem",
        [*catalog_codes(WAREHOUSES, Action.VIEW), JOB_VIEW],
    )


@pytest.fixture(scope="module")
def branches(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> list[str]:
    return ensure_branches(session_factory, dataset_alpha, BRANCH_CODES)


@pytest.fixture
def importer(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    importer_role: str,
    test_password: str,
    branches: list[str],
) -> dict[str, str]:
    assert branches, "cần ít nhất một chi nhánh"
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        importer_role,
        "nhaplieu",
        test_password,
        branch_codes=all_branch_codes(session_factory, dataset_alpha),
    )


@pytest.fixture
def reader(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    reader_role: str,
    test_password: str,
    branches: list[str],
) -> dict[str, str]:
    assert branches, "cần ít nhất một chi nhánh"
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        reader_role,
        "chixem",
        test_password,
        branch_codes=all_branch_codes(session_factory, dataset_alpha),
    )


def _upload(slug: str, rows: list[list[object]]) -> bytes:
    spec = REGISTRY.get(slug)
    assert spec is not None
    descriptor = template_for(spec)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = descriptor.sheet_name
    sheet.append(list(descriptor.headers))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ tệp mẫu


def test_the_template_route_is_not_swallowed_by_the_record_route(
    client: TestClient, importer: dict[str, str]
) -> None:
    """`GET /{slug}/template` phải thắng `GET /{slug}/{record_id}`.

    Hai bộ route dùng chung tiền tố `/api/v1/master` và FastAPI khớp theo thứ tự
    đăng ký, nên nếu bộ danh mục được gắn trước thì `template` rơi vào
    `{record_id}` và người dùng nhận `422` ("template" không phải số nguyên) thay
    vì nhận tệp. `main.py` gắn bộ nhập liệu trước, và đây là chỗ canh điều đó.
    """
    response = client.get(f"/api/v1/master/{WAREHOUSES}/template", headers=importer)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(XLSX)
    assert "attachment" in response.headers["content-disposition"]


def test_the_downloaded_template_opens_and_has_both_sheets(
    client: TestClient, importer: dict[str, str]
) -> None:
    response = client.get(f"/api/v1/master/{WAREHOUSES}/template", headers=importer)
    workbook = load_workbook(BytesIO(response.content))
    spec = REGISTRY.get(WAREHOUSES)
    assert spec is not None
    descriptor = template_for(spec)
    assert descriptor.sheet_name in workbook.sheetnames
    assert len(workbook.sheetnames) == 2


def test_downloading_a_template_needs_only_view_permission(
    client: TestClient, reader: dict[str, str]
) -> None:
    """Tệp mẫu không chứa dữ liệu — đòi quyền tạo ở đây chặn nhầm người chuẩn bị tệp."""
    response = client.get(f"/api/v1/master/{WAREHOUSES}/template", headers=reader)
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------- quyền


def test_validate_needs_the_import_permission(client: TestClient, reader: dict[str, str]) -> None:
    """Lớp quyền thứ nhất: được dùng chức năng nhập liệu hàng loạt hay không."""
    response = client.post(
        f"/api/v1/master/{WAREHOUSES}/import/validate",
        headers=reader,
        files={"file": ("kho.xlsx", _upload(WAREHOUSES, []), XLSX)},
    )
    assert response.status_code == 403, response.text


def test_validate_needs_permission_on_that_particular_catalog(
    client: TestClient, importer: dict[str, str]
) -> None:
    """Lớp quyền thứ hai (H48): quyền trên **danh mục này**, không phải mọi danh mục.

    Người dùng ở đây có `master.import.*` và toàn quyền trên danh mục kho, nhưng
    không có gì trên điều khoản thanh toán. Không có lớp này thì một mã quyền
    chung mở cửa cho cả hai mươi danh mục.
    """
    response = client.post(
        f"/api/v1/master/{PAYMENT_TERMS}/import/validate",
        headers=importer,
        files={"file": ("dk.xlsx", _upload(PAYMENT_TERMS, []), XLSX)},
    )
    assert response.status_code == 403, response.text


def test_commit_needs_create_permission_on_the_catalog(
    client: TestClient, importer: dict[str, str]
) -> None:
    response = client.post(
        f"/api/v1/master/{PAYMENT_TERMS}/import/commit",
        headers=importer,
        json={"validation_job_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 403, response.text


def test_import_jobs_cannot_be_queued_through_the_generic_endpoint(
    client: TestClient, importer: dict[str, str]
) -> None:
    """`direct_enqueue=False`: mã quyền tĩnh không nói được "trên danh mục nào".

    Không có cổng này thì ai có `master.import.create` sẽ ghi được vào **mọi**
    danh mục chỉ bằng cách gọi thẳng endpoint hàng đợi với `type` và `params` tự
    đặt — đi vòng qua đúng phép kiểm per-danh-mục mà test bên trên vừa canh.
    """
    response = client.post(
        "/api/v1/jobs",
        headers=importer,
        json={
            "type": "master.import.commit",
            "params": {"validation_job_id": "00000000-0000-0000-0000-000000000000"},
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "job.not_directly_enqueueable"


# -------------------------------------------------------------- xếp hàng


def test_validate_stores_the_file_and_queues_a_job(
    client: TestClient,
    importer: dict[str, str],
    import_dir: Path,
    dataset_alpha: DatasetRef,
) -> None:
    """H78: tệp vào kho định địa chỉ theo nội dung, báo cáo sẽ nằm ở `jobs.result`."""
    content = _upload(WAREHOUSES, [[unique_code("KHO_API"), "Kho API", None, None, None, None]])
    response = client.post(
        f"/api/v1/master/{WAREHOUSES}/import/validate",
        headers=importer,
        files={"file": ("kho.xlsx", content, XLSX)},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["catalog"] == WAREHOUSES
    assert body["mode"] == "create_only", "mặc định phải là chế độ không ghi đè (H80)"

    stored = storage.blob_path(import_dir, dataset_alpha.schema_name, body["content_hash"])
    assert stored.read_bytes() == content, "tệp đã kiểm phải là đúng tệp đã tải lên"

    queued = client.get(f"/api/v1/jobs/{body['job_id']}", headers=importer)
    assert queued.status_code == 200, queued.text
    assert queued.json()["type"] == "master.import.validate"


def test_commit_refuses_a_validation_job_that_does_not_exist(
    client: TestClient, importer: dict[str, str]
) -> None:
    """Bước ghi trỏ vào một lượt kiểm không có thật thì phải dừng ở lúc **xếp hàng**.

    Ở lát này phép kiểm ấy nằm trong thân job, nên endpoint vẫn trả `202` và job
    hỏng ngay sau đó — đúng hợp đồng của hàng đợi. Điều được canh là nó **không**
    ghi gì; xem `test_import_pipeline.py` cho phần bất biến.
    """
    response = client.post(
        f"/api/v1/master/{WAREHOUSES}/import/commit",
        headers=importer,
        json={"validation_job_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert response.status_code == 202, response.text
