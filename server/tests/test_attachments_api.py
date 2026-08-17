"""Tệp đính kèm đầu-cuối qua HTTP (bước 22 của phase 2, FR-NFR-053).

Test kho tệp đã kiểm tầng đĩa; ở đây kiểm **chuỗi**: quyền → dataset → RLS chi
nhánh → idempotency → byte đi ra đúng bằng byte đi vào. Bốn thứ chỉ hỏng ở mức
chuỗi và không mắt xích nào tự phát hiện được:

* một tệp đính ở chi nhánh A **không** hiện ra cho người của chi nhánh B — do
  RLS trên bảng gốc, không do bộ lọc trong câu truy vấn;
* hai dữ liệu kế toán không thấy tệp của nhau, cả trong DB lẫn trên đĩa;
* lượt gửi lại (mất mạng rồi bấm lại) không tạo bản ghi thứ hai;
* đường tải về **luôn** buộc tải xuống — một tệp HTML do người dùng tải lên
  không được chạy inline cùng origin với ứng dụng kế toán.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER, DATASET_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.attachments import storage
from ket.kernel.datasets.models import User
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security import role_service
from ket.kernel.security.models import Branch, Permission, Role, RolePermission
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.main import create_app
from ket.settings import Settings

pytestmark = pytest.mark.db

PASSWORD = "Ph1eu#Thu2026"
UserFactory = Callable[..., User]

ATTACHMENT_VIEW = permission_code(SYSTEM_MODULE, "attachment", Action.VIEW)
ATTACHMENT_CREATE = permission_code(SYSTEM_MODULE, "attachment", Action.CREATE)
ATTACHMENT_DELETE = permission_code(SYSTEM_MODULE, "attachment", Action.DELETE)
AUDIT_VIEW = permission_code(SYSTEM_MODULE, "audit_log", Action.VIEW)

CLERK_ROLE = "ke_toan_dinh_kem"
"""Vai trò đủ ba hành vi đính kèm + đọc nhật ký."""

READER_ROLE = "chi_xem_dinh_kem"
"""Chỉ `view`: dùng để chứng minh xem được không có nghĩa là đính được."""

BRANCH_CODES = ["CN_DK_A", "CN_DK_B"]

MAX_BYTES = 4096
"""Trần nhỏ cho bộ test này. Trần thật (25 MiB) không kiểm được bằng một tệp
thật trong test mà không làm chậm cả bộ."""

ENTITY_TYPE = "vouchers"
CONTENT = "Ủy nhiệm chi số 12/2026 — bản scan".encode()
CONTENT_HASH = sha256(CONTENT).hexdigest()
FILE_NAME = "Ủy nhiệm chi.pdf"


@pytest.fixture(scope="module")
def attachments_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("attachments")


@pytest.fixture(scope="module")
def attachment_settings(test_settings: Settings, attachments_dir: Path) -> Settings:
    return test_settings.model_copy(
        update={"attachments_dir": attachments_dir, "attachment_max_bytes": MAX_BYTES}
    )


@pytest.fixture
def client(
    attachment_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(attachment_settings)) as instance:
        yield instance


@pytest.fixture
def client_without_storage(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """Bản cài **chưa bật** đường đính kèm — mặc định của mọi bản cài mới."""
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


def _ensure_role(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    code: str,
    permissions: list[str],
) -> str:
    scope = RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            role = Role(code=code, name=f"Vai trò {code}")
            session.add(role)
            session.flush()
            for permission in permissions:
                permission_id = session.scalar(
                    select(Permission.id).where(Permission.code == permission)
                )
                assert permission_id is not None, f"chưa gieo mã quyền {permission}"
                session.add(
                    RolePermission(role_id=role.id, permission_id=permission_id, allow=True)
                )
    return code


@pytest.fixture(scope="module")
def clerk_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(
        session_factory,
        dataset_alpha,
        CLERK_ROLE,
        [ATTACHMENT_VIEW, ATTACHMENT_CREATE, ATTACHMENT_DELETE, AUDIT_VIEW],
    )


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return _ensure_role(session_factory, dataset_alpha, READER_ROLE, [ATTACHMENT_VIEW])


@pytest.fixture(scope="module")
def beta_clerk_role(session_factory: sessionmaker[Session], dataset_beta: DatasetRef) -> str:
    return _ensure_role(
        session_factory,
        dataset_beta,
        CLERK_ROLE,
        [ATTACHMENT_VIEW, ATTACHMENT_CREATE, ATTACHMENT_DELETE],
    )


@pytest.fixture(scope="module")
def attachment_branches(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> list[str]:
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        existing = set(
            session.scalars(select(Branch.code).where(Branch.code.in_(BRANCH_CODES))).all()
        )
        for code in BRANCH_CODES:
            if code not in existing:
                BranchService(session).create(code=code, name=f"Chi nhánh {code}")
    return BRANCH_CODES


@pytest.fixture(scope="module")
def beta_branch(session_factory: sessionmaker[Session], dataset_beta: DatasetRef) -> str:
    code = "CN_DK_BETA"
    scope = RequestScope(dataset_schema=dataset_beta.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        if session.scalar(select(Branch.id).where(Branch.code == code)) is None:
            BranchService(session).create(code=code, name="Chi nhánh Beta")
    return code


def _actor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    user_factory: UserFactory,
    role_code: str,
    prefix: str,
    *,
    branch_codes: list[str],
) -> dict[str, str]:
    """Người dùng có vai trò + chi nhánh, đã đăng nhập; trả headers dùng ngay."""
    user = user_factory(prefix)
    role_service.grant_role(
        session_factory,
        dataset_schema=dataset.schema_name,
        user_id=user.id,
        role_code=role_code,
        actor_user_id=user.id,
    )
    for branch_code in branch_codes:
        role_service.assign_branch(
            session_factory,
            dataset_schema=dataset.schema_name,
            user_id=user.id,
            branch_code=branch_code,
            actor_user_id=user.id,
            actor_branch_ids=None,
        )
    response = client.post(
        "/api/v1/auth/login", json={"username": user.username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['token']}",
        DATASET_HEADER: dataset.code,
    }


@pytest.fixture
def clerk(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    clerk_role: str,
    attachment_branches: list[str],
) -> dict[str, str]:
    """Kế toán viên của **một** chi nhánh — nên không phải gửi `X-Branch`."""
    return _actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        clerk_role,
        "dinhkem",
        branch_codes=[attachment_branches[0]],
    )


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    entity_id: str,
    content: bytes = CONTENT,
    file_name: str = FILE_NAME,
    media_type: str = "application/pdf",
    key: str | None = None,
    entity_type: str = ENTITY_TYPE,
) -> Response:
    return client.post(
        "/api/v1/attachments",
        headers={**headers, IDEMPOTENCY_HEADER: key or str(uuid4())},
        data={"entity_type": entity_type, "entity_id": entity_id},
        files={"file": (file_name, content, media_type)},
    )


def _entity_id() -> str:
    return uuid4().hex[:12]


# --------------------------------------------------------------------------
# Vòng đời: đính → xem → tải về → gỡ
# --------------------------------------------------------------------------


def test_upload_then_list_then_download_returns_the_exact_bytes(
    client: TestClient, clerk: dict[str, str], attachments_dir: Path, dataset_alpha: DatasetRef
) -> None:
    entity_id = _entity_id()

    created = _upload(client, clerk, entity_id=entity_id)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["content_hash"] == CONTENT_HASH
    assert body["byte_size"] == len(CONTENT)
    assert body["file_name"] == FILE_NAME
    assert body["media_type"] == "application/pdf"

    listed = client.get(
        "/api/v1/attachments",
        headers=clerk,
        params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [body["id"]]

    downloaded = client.get(f"/api/v1/attachments/{body['id']}/content", headers=clerk)
    assert downloaded.status_code == 200
    assert downloaded.content == CONTENT

    # Tệp nằm đúng nhánh thư mục của dataset này, tên sinh từ hash.
    assert (
        storage.blob_path(attachments_dir, dataset_alpha.schema_name, CONTENT_HASH).read_bytes()
        == CONTENT
    )


def test_the_download_always_forces_a_save_never_an_inline_render(
    client: TestClient, clerk: dict[str, str]
) -> None:
    """Chế độ trình duyệt LAN (v1.x) phục vụ web UI từ **chính origin này**.

    Một tệp HTML người dùng tải lên mà mở inline sẽ chạy script cùng origin với
    ứng dụng kế toán và đọc được phiên của người đang xem. `attachment` +
    `nosniff` cắt cả hai đường: buộc tải xuống, và không cho trình duyệt tự đoán
    lại kiểu nội dung từ byte đầu tệp.
    """
    entity_id = _entity_id()
    payload = b"<html><script>fetch('/api/v1/system/access')</script></html>"

    created = _upload(
        client,
        clerk,
        entity_id=entity_id,
        content=payload,
        file_name="bao-cao.html",
        media_type="text/html",
    )
    attachment_id = created.json()["id"]

    downloaded = client.get(f"/api/v1/attachments/{attachment_id}/content", headers=clerk)

    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert downloaded.headers["x-content-type-options"] == "nosniff"


def test_a_vietnamese_file_name_survives_the_round_trip(
    client: TestClient, clerk: dict[str, str]
) -> None:
    created = _upload(client, clerk, entity_id=_entity_id())
    attachment_id = created.json()["id"]

    downloaded = client.get(f"/api/v1/attachments/{attachment_id}/content", headers=clerk)

    # Dạng RFC 5987 là dạng duy nhất mang được dấu tiếng Việt; thiếu nó thì tệp
    # tải về mang một cái tên hỏng.
    assert "filename*=UTF-8''" in downloaded.headers["content-disposition"]
    assert "%E1%BB%A6" in downloaded.headers["content-disposition"]  # chữ "Ủ"


def test_a_file_name_with_path_separators_is_reduced_to_its_last_segment(
    client: TestClient, clerk: dict[str, str], attachments_dir: Path
) -> None:
    """Tên tệp không bao giờ chạm hệ tệp, nhưng vẫn phải sạch trước khi vào header."""
    payload = b"noi dung khac de co hash khac"

    created = _upload(
        client,
        clerk,
        entity_id=_entity_id(),
        content=payload,
        file_name="../../../etc/passwd",
    )

    assert created.json()["file_name"] == "passwd"
    assert not (attachments_dir.parent / "passwd").exists()


def test_detaching_hides_it_from_the_list_but_keeps_the_bytes(
    client: TestClient, clerk: dict[str, str], attachments_dir: Path, dataset_alpha: DatasetRef
) -> None:
    """Gỡ ≠ xóa: bản ghi khác có thể trỏ cùng nội dung, và vết gỡ phải tra ngược được."""
    entity_id = _entity_id()
    attachment_id = _upload(client, clerk, entity_id=entity_id).json()["id"]

    detached = client.delete(f"/api/v1/attachments/{attachment_id}", headers=clerk)
    assert detached.status_code == 200
    assert detached.json()["detached_at"] is not None

    listed = client.get(
        "/api/v1/attachments",
        headers=clerk,
        params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
    )
    assert listed.json()["items"] == []

    assert storage.blob_path(attachments_dir, dataset_alpha.schema_name, CONTENT_HASH).is_file()
    still_downloadable = client.get(f"/api/v1/attachments/{attachment_id}/content", headers=clerk)
    assert still_downloadable.status_code == 200, "gỡ nhầm thì vẫn phải lấy lại được tệp"


def test_detaching_twice_is_not_an_error(client: TestClient, clerk: dict[str, str]) -> None:
    attachment_id = _upload(client, clerk, entity_id=_entity_id()).json()["id"]

    first = client.delete(f"/api/v1/attachments/{attachment_id}", headers=clerk)
    second = client.delete(f"/api/v1/attachments/{attachment_id}", headers=clerk)

    assert first.status_code == second.status_code == 200
    # So **thời điểm**, không so chuỗi: cùng một giá trị `timestamptz` được trả
    # ở múi UTC khi nó vừa được ghi (đối tượng còn trong session) và ở múi của
    # cụm PostgreSQL khi nó được đọc lại. Hai chuỗi khác nhau, một thời điểm —
    # xem ghi chú ở báo cáo lát này về việc ghim `timezone=UTC` cho kết nối.
    assert datetime.fromisoformat(first.json()["detached_at"]) == datetime.fromisoformat(
        second.json()["detached_at"]
    )


# --------------------------------------------------------------------------
# Hợp đồng ghi (FR-NFR-004)
# --------------------------------------------------------------------------


def test_uploading_without_the_idempotency_header_is_refused(
    client: TestClient, clerk: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/attachments",
        headers=clerk,
        data={"entity_type": ENTITY_TYPE, "entity_id": _entity_id()},
        files={"file": (FILE_NAME, CONTENT, "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_missing"


def test_resending_the_same_upload_returns_the_first_attachment_with_200(
    client: TestClient, clerk: dict[str, str]
) -> None:
    """Bấm Đính kèm, mất mạng, bấm lại — đúng một tệp trong danh sách."""
    entity_id = _entity_id()
    key = str(uuid4())

    first = _upload(client, clerk, entity_id=entity_id, key=key)
    second = _upload(client, clerk, entity_id=entity_id, key=key)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, "lần gửi lại không tạo gì nên không phải 201"
    assert first.json()["id"] == second.json()["id"]

    listed = client.get(
        "/api/v1/attachments",
        headers=clerk,
        params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
    )
    assert len(listed.json()["items"]) == 1


def test_reusing_a_key_for_a_different_file_is_refused(
    client: TestClient, clerk: dict[str, str]
) -> None:
    """Dùng lại khóa cho nội dung khác là lỗi client, không phải một lần gửi lại."""
    entity_id = _entity_id()
    key = str(uuid4())

    _upload(client, clerk, entity_id=entity_id, key=key)
    response = _upload(client, clerk, entity_id=entity_id, content=b"mot tep khac", key=key)

    assert response.status_code == 409
    assert response.json()["error_code"] == "idempotency.key_reused"


def test_attaching_the_same_file_twice_with_a_new_key_is_a_conflict(
    client: TestClient, clerk: dict[str, str]
) -> None:
    entity_id = _entity_id()
    _upload(client, clerk, entity_id=entity_id)

    response = _upload(client, clerk, entity_id=entity_id)

    assert response.status_code == 409
    assert response.json()["error_code"] == "attachment.already_attached"


def test_the_same_file_can_be_attached_to_two_different_records(
    client: TestClient, clerk: dict[str, str], attachments_dir: Path, dataset_alpha: DatasetRef
) -> None:
    """Một hợp đồng scan đính vào nhiều chứng từ = nhiều bản ghi, **một** tệp."""
    first = _upload(client, clerk, entity_id=_entity_id())
    second = _upload(client, clerk, entity_id=_entity_id())

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["content_hash"] == second.json()["content_hash"]

    blobs = [
        path
        for path in (attachments_dir / dataset_alpha.schema_name).rglob("*")
        if path.is_file() and path.name == CONTENT_HASH
    ]
    assert len(blobs) == 1


# --------------------------------------------------------------------------
# Giới hạn và cấu hình
# --------------------------------------------------------------------------


def test_a_file_over_the_limit_is_refused(client: TestClient, clerk: dict[str, str]) -> None:
    response = _upload(client, clerk, entity_id=_entity_id(), content=b"x" * (MAX_BYTES + 1))

    assert response.status_code == 413
    assert response.json()["error_code"] == "attachment.too_large"


def test_an_empty_file_is_refused(client: TestClient, clerk: dict[str, str]) -> None:
    response = _upload(client, clerk, entity_id=_entity_id(), content=b"")

    assert response.status_code == 422
    assert response.json()["error_code"] == "attachment.empty"


def test_the_endpoint_says_so_when_the_installation_has_no_attachment_directory(
    client_without_storage: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    clerk_role: str,
    attachment_branches: list[str],
) -> None:
    """Mặc định của bản cài là **chưa bật** — và câu trả lời phải nêu cách bật."""
    headers = _actor(
        client_without_storage,
        session_factory,
        dataset_alpha,
        user_factory,
        clerk_role,
        "khongkho",
        branch_codes=[attachment_branches[0]],
    )

    response = _upload(client_without_storage, headers, entity_id=_entity_id())

    assert response.status_code == 503
    assert response.json()["error_code"] == "attachment.storage_not_configured"


@pytest.mark.parametrize(
    ("entity_type", "entity_id"),
    [("Vouchers", "12"), ("../etc", "12"), ("vouchers", "12 34"), ("vouchers", "")],
)
def test_a_malformed_owner_reference_is_refused(
    client: TestClient, clerk: dict[str, str], entity_type: str, entity_id: str
) -> None:
    """`entity_type`/`entity_id` đi thẳng vào cột sẽ được in ra báo cáo."""
    response = _upload(client, clerk, entity_type=entity_type, entity_id=entity_id)

    assert response.status_code == 422


def test_uploading_without_choosing_a_branch_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    clerk_role: str,
    attachment_branches: list[str],
) -> None:
    """Người dùng nhiều chi nhánh phải nói rõ tệp thuộc chi nhánh nào.

    Đoán hộ nghĩa là tệp rơi vào ngăn của chi nhánh khác và chính người vừa đính
    nó không còn thấy nữa.
    """
    headers = _actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        clerk_role,
        "hai_chi_nhanh",
        branch_codes=attachment_branches,
    )

    response = _upload(client, headers, entity_id=_entity_id())

    assert response.status_code == 422
    assert response.json()["error_code"] == "attachment.branch_required"

    # ...và chọn rồi thì đính được, vào đúng chi nhánh đã chọn.
    branches = client.get("/api/v1/system/access", headers=headers).json()["branch_ids"]
    chosen = branches[1]
    created = _upload(client, {**headers, BRANCH_HEADER: str(chosen)}, entity_id=_entity_id())
    assert created.status_code == 201, created.text
    assert created.json()["branch_id"] == chosen


# --------------------------------------------------------------------------
# Phân quyền và cô lập
# --------------------------------------------------------------------------


def test_viewing_does_not_imply_attaching(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    reader_role: str,
    attachment_branches: list[str],
) -> None:
    headers = _actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        reader_role,
        "chixem",
        branch_codes=[attachment_branches[0]],
    )

    response = _upload(client, headers, entity_id=_entity_id())

    assert response.status_code == 403
    assert ATTACHMENT_CREATE in response.text


def test_another_branch_can_neither_list_nor_download_the_file(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    clerk_role: str,
    attachment_branches: list[str],
    clerk: dict[str, str],
) -> None:
    """Cô lập do **RLS trên bảng gốc** làm, không do bộ lọc trong câu truy vấn.

    Hợp đồng, bảng lương scan và ủy nhiệm chi là đúng loại tệp không được rò
    sang chi nhánh khác — và một `WHERE branch_id` quên ở một endpoint của phase
    sau sẽ không làm test này đỏ nếu cô lập nằm ở tầng ứng dụng.
    """
    entity_id = _entity_id()
    attachment_id = _upload(client, clerk, entity_id=entity_id).json()["id"]

    other = _actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        clerk_role,
        "chinhanh_b",
        branch_codes=[attachment_branches[1]],
    )

    listed = client.get(
        "/api/v1/attachments",
        headers=other,
        params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
    )
    assert listed.json()["items"] == []

    fetched = client.get(f"/api/v1/attachments/{attachment_id}", headers=other)
    assert fetched.status_code == 404

    downloaded = client.get(f"/api/v1/attachments/{attachment_id}/content", headers=other)
    assert downloaded.status_code == 404


def test_the_other_dataset_sees_nothing_and_keeps_its_files_apart(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
    user_factory: UserFactory,
    beta_clerk_role: str,
    beta_branch: str,
    clerk: dict[str, str],
    attachments_dir: Path,
) -> None:
    """Hai doanh nghiệp trong cùng bản cài: tách ở DB **và** tách trên đĩa."""
    entity_id = _entity_id()
    alpha_id = _upload(client, clerk, entity_id=entity_id).json()["id"]

    beta = _actor(
        client,
        session_factory,
        dataset_beta,
        user_factory,
        beta_clerk_role,
        "beta_dinhkem",
        branch_codes=[beta_branch],
    )

    listed = client.get(
        "/api/v1/attachments",
        headers=beta,
        params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
    )
    assert listed.json()["items"] == []
    assert client.get(f"/api/v1/attachments/{alpha_id}", headers=beta).status_code == 404

    # Cùng nội dung, đính ở dataset beta → tệp nằm ở nhánh thư mục khác hẳn.
    _upload(client, beta, entity_id=entity_id)
    assert storage.blob_path(attachments_dir, dataset_beta.schema_name, CONTENT_HASH).is_file()
    assert storage.blob_path(attachments_dir, dataset_alpha.schema_name, CONTENT_HASH).is_file()


def test_attaching_and_detaching_leave_an_audit_trail(
    client: TestClient, clerk: dict[str, str]
) -> None:
    """Một tệp lặng lẽ biến mất khỏi chứng từ là đúng thứ nhật ký sinh ra để chặn."""
    entity_id = _entity_id()
    attachment_id = _upload(client, clerk, entity_id=entity_id).json()["id"]
    client.delete(f"/api/v1/attachments/{attachment_id}", headers=clerk)

    entries = client.get("/api/v1/system/audit-log?page_size=200", headers=clerk).json()["items"]
    mine = [
        entry
        for entry in entries
        if entry["entity_type"] == "attachments" and entry["entity_id"] == str(attachment_id)
    ]

    assert {entry["action"] for entry in mine} == {"created", "updated"}


def test_the_same_file_can_be_reattached_after_being_detached(
    client: TestClient, clerk: dict[str, str]
) -> None:
    """Đính nhầm chỗ rồi sửa: gỡ ra, đính lại đúng tệp đó vào đúng bản ghi đó.

    Đây là test canh tính **bộ phận** của `uq_attachments_live`. Bỏ mệnh đề
    `WHERE detached_at IS NULL` đi thì mọi test khác vẫn xanh — chỉ thao tác sửa
    sai này hỏng, và nó hỏng ở nơi người dùng thật gặp nó chứ không ở CI.
    """
    entity_id = _entity_id()
    first = _upload(client, clerk, entity_id=entity_id)
    client.delete(f"/api/v1/attachments/{first.json()['id']}", headers=clerk)

    again = _upload(client, clerk, entity_id=entity_id)

    assert again.status_code == 201, again.text
    assert again.json()["id"] != first.json()["id"]
    listed = client.get(
        "/api/v1/attachments",
        headers=clerk,
        params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
    )
    assert [item["id"] for item in listed.json()["items"]] == [again.json()["id"]]


def test_two_branches_can_attach_the_same_file_to_the_same_record(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    clerk_role: str,
    attachment_branches: list[str],
    clerk: dict[str, str],
) -> None:
    """Ràng buộc duy nhất chạy **ngoài** RLS, nên nó phải mang theo chi nhánh.

    Không mang thì nó thành máy soi: chi nhánh B đính tệp và nhận `409` là biết
    chi nhánh A đã đính đúng tệp đó vào đúng bản ghi đó — dù B không được phép
    thấy dòng nào của A. Và B cũng mất luôn khả năng đính tệp của chính mình.
    """
    entity_id = _entity_id()
    _upload(client, clerk, entity_id=entity_id)

    other = _actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        clerk_role,
        "chinhanh_b_dinh",
        branch_codes=[attachment_branches[1]],
    )
    response = _upload(client, other, entity_id=entity_id)

    assert response.status_code == 201, response.text
    # ...và mỗi bên chỉ thấy đúng dòng của mình.
    for headers in (clerk, other):
        listed = client.get(
            "/api/v1/attachments",
            headers=headers,
            params={"entity_type": ENTITY_TYPE, "entity_id": entity_id},
        )
        assert len(listed.json()["items"]) == 1


def test_the_declared_length_matches_the_bytes_actually_sent(
    client: TestClient, clerk: dict[str, str]
) -> None:
    """`Content-Length` phải nói về tệp thật, không về cột metadata.

    Hai giá trị chỉ lệch khi kho tệp đã hỏng — và đúng lúc đó, khai theo metadata
    làm client treo chờ những byte không bao giờ tới.
    """
    created = _upload(client, clerk, entity_id=_entity_id())

    downloaded = client.get(f"/api/v1/attachments/{created.json()['id']}/content", headers=clerk)

    assert int(downloaded.headers["content-length"]) == len(downloaded.content) == len(CONTENT)


def test_an_unwritable_store_reports_an_operations_problem_not_a_crash(
    client: TestClient, clerk: dict[str, str], attachments_dir: Path, dataset_alpha: DatasetRef
) -> None:
    """Ổ chưa gắn / sai quyền là việc người quản trị sửa được trong một phút.

    `500 "lỗi không mong muốn, báo bộ phận hỗ trợ"` che mất đúng câu cần nói.
    """
    dataset_dir = attachments_dir / dataset_alpha.schema_name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    original_mode = dataset_dir.stat().st_mode
    dataset_dir.chmod(0o500)
    try:
        response = _upload(client, clerk, entity_id=_entity_id(), content=b"khong ghi duoc")
    finally:
        dataset_dir.chmod(original_mode)

    assert response.status_code == 503
    assert response.json()["error_code"] == "attachment.storage_unavailable"
