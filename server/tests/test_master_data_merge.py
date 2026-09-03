"""Gộp hai bản ghi danh mục trùng nhau (FR-SYS-016, lát 3B-2).

Điểm khó của thao tác này không phải câu `UPDATE` mà là **biết phải sửa bảng
nào**. Danh sách viết tay sẽ thiếu đúng bảng mà phase sau vừa thêm, nên
`merge_service` đọc khóa ngoại từ `pg_catalog` lúc chạy — và test chính của tệp
này dựng một bảng **mới toanh** mà mã nguồn chưa từng biết tới, rồi khẳng định
gộp vẫn sửa nó. Đó là cách duy nhất đo được tính chất "không hard-code", vì mọi
bảng có sẵn trong repo đều có thể đã được liệt kê ở đâu đó.

Tiêu chí thành công của phase ("gộp 2 đối tác có chứng từ ở ≥3 module") chưa
kiểm được trọn vẹn ở đây: chứng từ đầu tiên ra đời ở phase 6. Bảng dựng tạm bên
dưới đóng vai một bảng chứng từ tương lai, và tiêu chí thật vẫn để ngỏ trong
phase file cho tới khi có chứng từ thật.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, UniqueConstraint, select, text
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import (
    UserFactory,
    actor,
    all_branch_codes,
    branch_ids,
    catalog_codes,
    create_record,
    ensure_branches,
    ensure_role,
    unique_code,
)
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.attachments.models import ATTACHMENT_TABLE_NAME, Attachment
from ket.kernel.auditing.models import AUDIT_TABLE_NAME, AuditLog
from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.merge_service import (
    SOFT_REFERENCE_POLICY,
    SoftReferencePolicy,
    foreign_keys_to,
)
from ket.kernel.master_data.registry import REGISTRY
from ket.kernel.master_data.usage import MASTER_DATA_USAGE_TABLE_NAME, record_use, usage_count_of
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import Action
from ket.main import create_app
from ket.model_registry import DatasetBase
from ket.settings import Settings

PARTNERS = "partners"
BANKS = "banks"

EDITOR_ROLE = "ke_toan_gop"
READER_ROLE = "chi_sua_khong_xoa"
BRANCH_CODES = ["CN_GOP_A", "CN_GOP_B"]

UNIQUE_CHILD_TABLE = "merge_probe_unique_children"
"""Bảng con có ràng buộc duy nhất theo `partner_id`, dựng trong test."""

FUTURE_DOCUMENT_TABLE = "merge_probe_documents"
"""Bảng đóng vai "bảng chứng từ của phase sau" — dựng trong test, không có trong
mã nguồn. Nếu `merge_service` hard-code danh sách bảng thì test dùng nó sẽ đỏ."""


# ------------------------------------------------------------------ cổng không cần DB


def test_every_soft_reference_table_declares_a_policy() -> None:
    """Bảng trỏ vào danh mục bằng `(entity_type, entity_id)` phải có chính sách.

    Ba bảng như vậy hôm nay (`master_data_usage`, `attachments`, `audit_log`), và
    chúng **không** nằm trong danh sách khóa ngoại nên bộ dò `pg_catalog` không
    thấy. Bảng mềm thứ tư ra đời ở phase sau sẽ làm test này đỏ — đúng lúc cần
    một người quyết định nó phải chuyển, phải chặn, hay phải giữ nguyên.
    """
    soft_tables = {
        name
        for name, table in DatasetBase.metadata.tables.items()
        if {"entity_type", "entity_id"} <= set(table.columns.keys())
    }

    missing = soft_tables - set(SOFT_REFERENCE_POLICY)

    assert not missing, (
        f"Bảng tham chiếu mềm chưa khai chính sách gộp: {sorted(missing)}. "
        "Thêm một dòng vào `SOFT_REFERENCE_POLICY` kèm lý do."
    )
    assert SOFT_REFERENCE_POLICY[AUDIT_TABLE_NAME] is SoftReferencePolicy.KEEP_HISTORY
    assert SOFT_REFERENCE_POLICY[MASTER_DATA_USAGE_TABLE_NAME] is SoftReferencePolicy.MOVE_COUNTER
    assert SOFT_REFERENCE_POLICY[ATTACHMENT_TABLE_NAME] is SoftReferencePolicy.REFUSE_WHEN_PRESENT


def test_a_child_table_with_a_unique_constraint_forces_its_catalog_to_declare_a_hook() -> None:
    """Bảng con mang ràng buộc duy nhất theo cột danh mục ⇒ danh mục phải khai `MergeHook`.

    Câu `UPDATE` chuyển khóa ngoại không biết gì về ràng buộc duy nhất, nên bảng
    con kiểu này làm gộp đổ ở đúng ca thường gặp nhất — hai bản trùng thì thường
    **cả hai** đều đã khai dòng con (review lát này, H1). Cổng này bắt bảng con
    thứ hai ra đời ở phase sau, khi người thêm nó chưa đọc lại chỗ gộp.

    Đối chiếu **hai chiều** để nó không thành hằng đúng: hôm nay ba danh mục cần
    hook (`partners`, `items`, và `units_of_measure` — vì `uq_item_units_item_unit`
    chứa **cả hai** cột danh mục của bảng con ấy), và test khẳng định cả tập cần
    lẫn tập đã khai.
    """
    catalog_tables = {str(spec.model.__tablename__): spec.slug for spec in REGISTRY.specs()}
    needs_hook: set[str] = set()
    for table in DatasetBase.metadata.tables.values():
        unique_columns = {
            column.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
            for column in constraint.columns
        } | {column.name for index in table.indexes if index.unique for column in index.columns}
        for foreign_key in (key for column in table.columns for key in column.foreign_keys):
            owner = catalog_tables.get(foreign_key.column.table.name)
            if owner is not None and foreign_key.parent.name in unique_columns:
                needs_hook.add(owner)

    declared = {spec.slug for spec in REGISTRY.specs() if spec.merge_hooks}

    assert needs_hook == declared, (
        f"Danh mục cần khai `merge_hooks`: {sorted(needs_hook)}; đang khai: {sorted(declared)}. "
        "Bảng con có ràng buộc duy nhất theo cột danh mục thì gộp phải có bước hợp nhất, "
        "nếu không nó đổ ngay ở ca hai bản ghi cùng có dòng con."
    )


# --------------------------------------------------------------------------- khung


db = pytest.mark.db


@pytest.fixture(scope="module")
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    """`TestClient` dùng chung cho CẢ TỆP — kèm hai ràng buộc.

    Phạm vi module để `editor` chỉ phải gán chi nhánh một lần (xem docstring
    của nó). Đổi lại tệp này mang hai bất biến mà 32 tệp API khác không có, nên
    **đừng chép dòng `scope="module"` sang tệp mới mà không chép cả hai**:

    1. **Không bài nào trong tệp được tạo chi nhánh mới** — người dùng dùng
       chung được gán chi nhánh MỘT lần và không tự cập nhật.
    2. Cửa sổ hạn mức của `RateLimitMiddleware` là trạng thái trong tiến trình
       của một app, nên nay nó dùng chung cho cả tệp thay vì mới lại mỗi bài.
       Vì thế tắt hẳn hạn mức ở đây: đo được đỉnh **68/600** ở bucket mặc
       định (theo người gọi) trong một cửa sổ 60s, và mỗi fixture actor phạm vi
       hàm còn lại tốn thêm một lượt đăng nhập ở bucket `auth` — hai bucket
       khác nhau, câu cũ gộp nhầm chúng làm một. Tệp vẫn xanh với hạn mức
       production, tức đây là chống-vỡ-về-sau chứ không phải một `429` đã quan
       sát được. Hạn mức có bộ test riêng ở `test_rate_limit.py`; tệp này
       không nói gì về nó.
    """
    assert app_engine is not None and session_factory is not None
    unlimited = test_settings.model_copy(
        update={"rate_limit_per_minute": 0, "rate_limit_auth_per_minute": 0}
    )
    with api_test_client(create_app(unlimited)) as instance:
        yield instance


@pytest.fixture(autouse=True)
def _shared_actor_still_spans_every_branch(request: pytest.FixtureRequest) -> None:
    """Người dùng dùng chung được gán chi nhánh MỘT lần — canh cho nó còn trọn phạm vi.

    `editor` là fixture phạm vi module: nó đọc "mọi chi nhánh" và ghi
    `user_branches` đúng một lần, ở bài đầu tiên. Nếu một bài trong tệp này tạo
    thêm chi nhánh, người dùng ấy **không** được cập nhật, và các bài sau đỏ
    bằng `scope_incomplete` / `scope_insufficient` — một thông điệp trỏ thẳng
    vào mã production, khiến bản sửa hấp dẫn nhất là nới lỏng chính phép kiểm
    phạm vi đang đúng.

    Bài kiểm này biến ca đó thành một câu nói rõ nguyên nhân. Nó tốn một truy
    vấn đếm mỗi bài; so với 19 lượt gán chi nhánh mà phạm vi module vừa cắt đi
    thì không đáng kể.
    """
    # Bài KHÔNG mang dấu `db` không được kéo theo fixture cần PostgreSQL.
    # Autouse áp cho MỌI bài trong tệp, kể cả hai bài kiểm bất biến schema chạy
    # thuần trong bộ nhớ — và khai `session_factory`/`dataset_alpha` thành THAM
    # SỐ là đủ để pytest dựng chúng TRƯỚC khi thân hàm chạy, nên một lệnh
    # `return` sớm không cứu được. Phải lấy lười bằng `getfixturevalue` sau khi
    # đã kiểm dấu. (Bản đầu mắc đúng lỗi này: `make server-test` đỏ 2 ERROR.)
    if request.node.get_closest_marker("db") is None:
        return
    session_factory: sessionmaker[Session] = request.getfixturevalue("session_factory")
    dataset_alpha: DatasetRef = request.getfixturevalue("dataset_alpha")
    with unit_of_work(
        session_factory,
        RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=()),
    ) as session:
        live = len(list(session.scalars(select(Branch.code)).all()))
    seen = _BRANCH_SPAN.setdefault("n", live)
    assert live == seen, (
        f"Tệp này tạo thêm chi nhánh giữa chừng ({seen} → {live}). "
        "`editor` phạm vi module đã gán chi nhánh một lần và không tự cập "
        "nhật, nên các bài sau sẽ đỏ vì thiếu phạm vi. Tạo chi nhánh trong "
        "fixture phạm vi module trước khi dựng người dùng, đừng tạo trong bài."
    )


_BRANCH_SPAN: dict[str, int] = {}


@pytest.fixture(scope="module")
def merge_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    every = (Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE)
    return ensure_role(
        session_factory,
        dataset_alpha,
        EDITOR_ROLE,
        [*catalog_codes(PARTNERS, *every), *catalog_codes(BANKS, *every)],
    )


@pytest.fixture(scope="module")
def editor_without_delete(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        READER_ROLE,
        [*catalog_codes(PARTNERS, Action.VIEW, Action.CREATE, Action.EDIT)],
    )


@pytest.fixture(scope="module")
def merge_branches(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> list[str]:
    return ensure_branches(session_factory, dataset_alpha, BRANCH_CODES)


@pytest.fixture(scope="module")
def editor(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    merge_role: str,
    merge_branches: list[str],
    test_password: str,
) -> dict[str, str]:
    """Người gộp có **phạm vi toàn công ty** — điều kiện của thao tác gộp.

    Không phải chi tiết dựng test cho tiện: `merge_records` đòi đúng điều này, vì
    mọi câu lệnh của lần gộp chạy dưới RLS và phạm vi người gọi **chính là** phần
    dữ liệu lần gộp nhìn thấy (review C1). Người có phạm vi thiếu có test riêng.
    """
    assert merge_branches, "cần ít nhất một chi nhánh để dựng người gộp"
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        merge_role,
        "gop",
        test_password,
        branch_codes=all_branch_codes(session_factory, dataset_alpha),
    )


@pytest.fixture
def every_branch_editor(editor: dict[str, str]) -> dict[str, str]:
    """Tên nói rõ ý định ở những test mà phạm vi đầy đủ **là** thứ đang được đo."""
    return editor


@pytest.fixture
def probe_table(owner_engine: Engine, dataset_alpha: DatasetRef) -> Iterator[str]:
    """Một bảng chứng từ **giả định của phase sau**, có khóa ngoại tới `partners`.

    Dựng bằng `ket_owner` như migration thật, kèm `GRANT` cho vai trò runtime
    của dataset — thiếu `GRANT` thì lệnh gộp đổ vì quyền chứ vì lý do khác, và
    test sẽ đo nhầm thứ.
    """
    role = role_name_for_schema(dataset_alpha.schema_name)
    schema = dataset_alpha.schema_name
    with owner_engine.begin() as connection:
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.execute(
            text(
                f"CREATE TABLE {FUTURE_DOCUMENT_TABLE} ("
                "  id serial PRIMARY KEY,"
                "  partner_id integer NOT NULL REFERENCES partners(id) ON DELETE RESTRICT)"
            )
        )
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {FUTURE_DOCUMENT_TABLE} TO {role}")
        )
        connection.execute(
            text(f"GRANT USAGE, SELECT ON SEQUENCE {FUTURE_DOCUMENT_TABLE}_id_seq TO {role}")
        )
    try:
        yield FUTURE_DOCUMENT_TABLE
    finally:
        with owner_engine.begin() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.execute(text(f"DROP TABLE IF EXISTS {FUTURE_DOCUMENT_TABLE}"))


@pytest.fixture
def probe_unique_table(owner_engine: Engine, dataset_alpha: DatasetRef) -> Iterator[str]:
    """Bảng con của phase sau **chưa** khai `MergeHook`, có ràng buộc duy nhất.

    Đóng vai "định mức vật tư theo đối tác" mà review nêu: người thêm bảng chưa
    đọc lại chỗ gộp. Ở đây đo phần lưới an toàn — câu từ chối phải nêu việc phải
    làm, không phải tên một chỉ mục.
    """
    role = role_name_for_schema(dataset_alpha.schema_name)
    schema = dataset_alpha.schema_name
    with owner_engine.begin() as connection:
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.execute(
            text(
                f"CREATE TABLE {UNIQUE_CHILD_TABLE} ("
                "  id serial PRIMARY KEY,"
                "  partner_id integer NOT NULL REFERENCES partners(id) ON DELETE RESTRICT,"
                "  code text NOT NULL,"
                f"  CONSTRAINT uq_{UNIQUE_CHILD_TABLE} UNIQUE (partner_id, code))"
            )
        )
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {UNIQUE_CHILD_TABLE} TO {role}")
        )
        connection.execute(
            text(f"GRANT USAGE, SELECT ON SEQUENCE {UNIQUE_CHILD_TABLE}_id_seq TO {role}")
        )
    try:
        yield UNIQUE_CHILD_TABLE
    finally:
        with owner_engine.begin() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.execute(text(f"DROP TABLE IF EXISTS {UNIQUE_CHILD_TABLE}"))


def _add_account(
    client: TestClient,
    headers: dict[str, str],
    partner_id: int,
    bank_id: int,
    *,
    number: str,
) -> httpx.Response:
    response = client.post(
        f"/api/v1/master/{PARTNERS}/{partner_id}/bank-accounts",
        json={
            "bank_id": bank_id,
            "account_number": number,
            "account_holder": "Công ty trùng",
            "is_default": True,
        },
        headers={**headers, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )
    assert response.status_code == 201, response.text
    return response


def _partner(client: TestClient, headers: dict[str, str], **extra: object) -> dict[str, object]:
    response = create_record(
        client,
        headers,
        PARTNERS,
        {
            "code": unique_code("GOP"),
            "name": "Công ty trùng",
            "is_customer": True,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _merge(
    client: TestClient,
    headers: dict[str, str],
    *,
    source_id: int,
    target_id: int,
    slug: str = PARTNERS,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/v1/master/{slug}/actions/merge",
        json={"source_id": source_id, "target_id": target_id},
        headers={**headers, IDEMPOTENCY_HEADER: key or unique_code("KEY")},
    )


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=1, branch_ids=())


# ------------------------------------------------------------------------- hành vi


@db
def test_merging_moves_references_a_table_the_code_never_heard_of(
    client: TestClient,
    editor: dict[str, str],
    owner_engine: Engine,
    dataset_alpha: DatasetRef,
    probe_table: str,
) -> None:
    """Danh sách bảng phải đến từ **cơ sở dữ liệu**, không từ mã nguồn.

    Bảng `merge_probe_documents` chỉ tồn tại trong lần chạy test này. Mọi bản
    cài đặt "gộp" viết danh sách bảng bằng tay đều trượt test này, và trượt đúng
    theo cách phase sau sẽ trượt trong sản xuất: bảng mới không được sửa, chứng
    từ vẫn trỏ vào một bản ghi đã xóa.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    schema = dataset_alpha.schema_name
    with owner_engine.begin() as connection:
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.execute(
            text(f"INSERT INTO {probe_table} (partner_id) VALUES (:id), (:id)"),
            {"id": source["id"]},
        )

    response = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 200, response.text
    body = response.json()
    moved = {f"{item['table']}.{item['column']}": item["rows"] for item in body["moved"]}
    assert moved.get(f"{probe_table}.partner_id") == 2
    assert body["total_rows_moved"] >= 2
    with owner_engine.begin() as connection:
        connection.execute(text(f'SET search_path TO "{schema}"'))
        owners = connection.execute(text(f"SELECT partner_id FROM {probe_table}")).scalars().all()
    assert set(owners) == {target["id"]}
    gone = client.get(f"/api/v1/master/{PARTNERS}/{source['id']}", headers=editor)
    assert gone.status_code == 404, gone.text


@db
def test_merging_moves_the_bank_accounts_of_the_source(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Khóa ngoại thật trong repo cũng phải đi theo, không chỉ bảng dựng tạm."""
    source = _partner(client, editor)
    target = _partner(client, editor)
    bank = create_record(client, editor, BANKS, {"code": unique_code("NH"), "name": "NH gộp"})
    added = client.post(
        f"/api/v1/master/{PARTNERS}/{source['id']}/bank-accounts",
        json={
            "bank_id": bank.json()["id"],
            "account_number": "7000001",
            "account_holder": "Công ty trùng",
            "is_default": True,
        },
        headers={**editor, IDEMPOTENCY_HEADER: unique_code("KEY")},
    )
    assert added.status_code == 201, added.text

    merged = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert merged.status_code == 200, merged.text
    listing = client.get(f"/api/v1/master/{PARTNERS}/{target['id']}/bank-accounts", headers=editor)
    assert [item["account_number"] for item in listing.json()["items"]] == ["7000001"]


@db
def test_the_usage_counter_moves_to_the_target(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Bộ đếm phải đi theo số dòng chứng từ vừa được trỏ lại (BR-SYS-02).

    Không cộng thì bản ghi đích trông như chưa ai dùng — và xóa được ngay sau
    khi vừa nhận toàn bộ chứng từ của bản ghi nguồn.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        record_use(session, entity_type="partners", entity_id=int(source["id"]), delta=5)
        record_use(session, entity_type="partners", entity_id=int(target["id"]), delta=2)

    merged = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert merged.status_code == 200, merged.text
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        assert usage_count_of(session, entity_type="partners", entity_id=int(target["id"])) == 7
        assert usage_count_of(session, entity_type="partners", entity_id=int(source["id"])) == 0


@db
def test_merging_leaves_a_trail_naming_the_source(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Gộp không hoàn tác được, nên nó phải có vết (FR-NFR-012).

    Vết ghi trên bản ghi **đích** và nêu mã của bản ghi nguồn: dòng nhật ký của
    bản ghi nguồn chỉ nói "đã xóa", không nói nó đi đâu.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)

    merged = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))
    assert merged.status_code == 200, merged.text

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        entries = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "partners")
                .where(AuditLog.entity_id == str(target["id"]))
                .order_by(AuditLog.id.desc())
            )
            .scalars()
            .all()
        )
    merge_entries = [
        entry
        for entry in entries
        if entry.new_values is not None and "merged_from_id" in entry.new_values
    ]
    assert merge_entries, "không có dòng nhật ký nào ghi lại lần gộp"
    assert merge_entries[0].new_values is not None
    assert merge_entries[0].new_values["merged_from_code"] == source["code"]


@db
def test_the_foreign_key_lookup_stays_inside_one_dataset(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Danh sách khóa ngoại chỉ được của **schema hiện tại** (LD-15).

    Mỗi dữ liệu kế toán là một schema, và cùng một bảng tồn tại trong tất cả.
    Không lọc thì mỗi cột bị liệt kê một lần cho mỗi dataset — một bản cài 50
    doanh nghiệp chạy 50 lần cùng một `UPDATE` cho mỗi cột, và tệ hơn: khi các
    dataset lệch phiên bản migration, tên bảng không định danh schema sẽ trỏ vào
    một bảng không tồn tại trong schema hiện tại.

    Test **đòi** dataset thứ hai tồn tại (`dataset_beta`) — chính chỗ mà lần đo
    của reviewer bị rỗng: đột biến gỡ dòng lọc sống sót vì schema thứ hai chưa
    được dựng trong lần chạy đó.
    """
    assert dataset_beta.schema_name != dataset_alpha.schema_name
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        found = foreign_keys_to(session, "partners")

    assert found, "phải tìm thấy ít nhất một cột trỏ vào partners"
    assert len(found) == len(set(found)), (
        f"danh sách khóa ngoại có bản lặp: {found} — bộ lọc `current_schema()` đã rơi mất, "
        "nên bảng cùng tên ở dataset khác cũng được liệt kê"
    )


@db
def test_the_merge_trail_is_recorded_on_the_branch_of_the_target(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """Vết gộp phải gắn chi nhánh của bản ghi **đích**.

    `audit_log` bật RLS theo chi nhánh: gắn sai chi nhánh thì dòng vết bị giấu
    khỏi đúng người cần đọc nó, và một thao tác không hoàn tác được trở thành
    thao tác không tra được.
    """
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    branch = ids[merge_branches[0]]
    at_branch = {**editor, BRANCH_HEADER: str(branch)}
    source = _partner(client, at_branch, branch_id=branch)
    target = _partner(client, at_branch, branch_id=branch)

    merged = _merge(client, at_branch, source_id=int(source["id"]), target_id=int(target["id"]))
    assert merged.status_code == 200, merged.text

    # Đọc nhật ký **trong phạm vi chi nhánh đó**: `audit_log` bật RLS, nên một
    # phạm vi rỗng không thấy dòng nào — và một test đọc bằng phạm vi rỗng sẽ
    # xanh với mọi `branch_id` sai.
    reader_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(branch,),
        acting_branch_id=branch,
    )
    with unit_of_work(session_factory, reader_scope) as session:
        entries = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "partners")
                .where(AuditLog.entity_id == str(target["id"]))
            )
            .scalars()
            .all()
        )
    trail = [
        entry
        for entry in entries
        if entry.new_values is not None and "merged_from_id" in entry.new_values
    ]
    assert trail, "không có dòng nhật ký nào ghi lại lần gộp"
    assert trail[0].branch_id == branch


@db
def test_merging_a_record_into_itself_is_refused(
    client: TestClient, editor: dict[str, str]
) -> None:
    partner = _partner(client, editor)

    response = _merge(client, editor, source_id=int(partner["id"]), target_id=int(partner["id"]))

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "same_record"


@db
def test_a_source_with_children_is_refused(client: TestClient, editor: dict[str, str]) -> None:
    """Gộp hai bản trùng ≠ dời cả một nhánh — đoán nhầm ý định là mất dữ liệu."""
    group = _partner(client, editor, is_group=True)
    _partner(client, editor, parent_id=group["id"])
    target = _partner(client, editor)

    response = _merge(client, editor, source_id=int(group["id"]), target_id=int(target["id"]))

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "source_has_children"


@db
def test_a_source_with_a_live_attachment_is_refused(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """Tệp đính kèm bật RLS theo chi nhánh — chuyển ở đây chỉ chuyển được một nửa."""
    source = _partner(client, editor)
    target = _partner(client, editor)
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    branch = ids[merge_branches[0]]
    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(branch,),
        acting_branch_id=branch,
    )
    with unit_of_work(session_factory, scope) as session:
        session.add(
            Attachment(
                entity_type="partners",
                entity_id=str(source["id"]),
                content_hash="a" * 64,
                byte_size=10,
                media_type="application/pdf",
                file_name="hop-dong.pdf",
                branch_id=branch,
                uploaded_by=1,
            )
        )

    response = _merge(
        client,
        {**editor, BRANCH_HEADER: str(branch)},
        source_id=int(source["id"]),
        target_id=int(target["id"]),
    )

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "source_has_attachments"


@db
def test_a_target_in_a_narrower_scope_is_refused(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """Đích hẹp hơn nguồn nghĩa là chứng từ của chi nhánh khác trỏ vào chỗ họ không thấy."""
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    at_a = {**editor, BRANCH_HEADER: str(ids[merge_branches[0]])}
    shared = _partner(client, at_a)
    private = _partner(client, at_a, branch_id=ids[merge_branches[0]])

    response = _merge(client, at_a, source_id=int(shared["id"]), target_id=int(private["id"]))

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "target_scope_narrower"


@db
def test_merging_a_private_record_into_the_shared_one_is_allowed(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """Chiều ngược lại chỉ **mở rộng** tầm nhìn — đối trọng của test trên."""
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    at_a = {**editor, BRANCH_HEADER: str(ids[merge_branches[0]])}
    private = _partner(client, at_a, branch_id=ids[merge_branches[0]])
    shared = _partner(client, at_a)

    response = _merge(client, at_a, source_id=int(private["id"]), target_id=int(shared["id"]))

    assert response.status_code == 200, response.text


@db
def test_a_record_of_another_branch_cannot_be_merged(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """`404` như mọi đường khác: mã lỗi khác nhau là một oracle liệt kê id (H-3)."""
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    at_b = {**editor, BRANCH_HEADER: str(ids[merge_branches[1]])}
    private_to_b = _partner(client, at_b, branch_id=ids[merge_branches[1]])

    at_a = {**editor, BRANCH_HEADER: str(ids[merge_branches[0]])}
    target = _partner(client, at_a)
    response = _merge(client, at_a, source_id=int(private_to_b["id"]), target_id=int(target["id"]))

    assert response.status_code == 404, response.text


@db
def test_resending_the_same_merge_does_not_merge_again(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Lần gửi lại sau khi mạng rớt không được biến thành `404` khó hiểu."""
    source = _partner(client, editor)
    target = _partner(client, editor)
    key = unique_code("KEY")

    first = _merge(
        client, editor, source_id=int(source["id"]), target_id=int(target["id"]), key=key
    )
    second = _merge(
        client, editor, source_id=int(source["id"]), target_id=int(target["id"]), key=key
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["target_id"] == int(target["id"])
    assert second.json()["total_rows_moved"] == 0


@db
def test_a_merger_without_every_branch_is_refused(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    merge_role: str,
    merge_branches: list[str],
    test_password: str,
    editor: dict[str, str],
) -> None:
    """Gộp là thao tác **toàn công ty** — phạm vi thiếu thì từ chối, không làm nửa vời.

    Đây là phép chặn thay thế cho lỗ hổng C1 của review: mọi câu lệnh của lần
    gộp chạy dưới RLS, nên người chỉ được gán một phần chi nhánh sẽ không nhìn
    thấy tệp đính kèm và chứng từ của phần còn lại. Trước đây phép kiểm tệp đính
    kèm chạy trên đúng khoảng mù đó và trả lời "không có tệp nào".
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    partial = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        merge_role,
        "motchinhanh",
        test_password,
        branch_codes=[merge_branches[0]],
    )

    response = _merge(client, partial, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "scope_incomplete"


@db
def test_an_attachment_in_a_branch_the_merger_cannot_see_still_blocks(
    client: TestClient,
    editor: dict[str, str],
    every_branch_editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """Kịch bản C1 của review, viết thành cổng.

    Tệp đính kèm nằm ở chi nhánh **không phải** chi nhánh đang thao tác. Nếu
    phép kiểm tệp đính kèm lại chạy trên khoảng mù RLS thì gộp sẽ chạy tiếp và
    để lại một dòng `attachments` trỏ vào id đã xóa — không khóa ngoại nào nổ.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    far_branch = ids[merge_branches[1]]
    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(far_branch,),
        acting_branch_id=far_branch,
    )
    with unit_of_work(session_factory, scope) as session:
        session.add(
            Attachment(
                entity_type="partners",
                entity_id=str(source["id"]),
                content_hash="b" * 64,
                byte_size=10,
                media_type="application/pdf",
                file_name="hop-dong-chi-nhanh-khac.pdf",
                branch_id=far_branch,
                uploaded_by=1,
            )
        )

    # Người gộp có đủ phạm vi công ty nhưng **đang thao tác** ở chi nhánh khác
    # với tệp đính kèm.
    response = _merge(
        client,
        {**every_branch_editor, BRANCH_HEADER: str(ids[merge_branches[0]])},
        source_id=int(source["id"]),
        target_id=int(target["id"]),
    )

    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "source_has_attachments"


@db
def test_a_detached_attachment_does_not_block_the_merge(
    client: TestClient,
    editor: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    merge_branches: list[str],
) -> None:
    """Tệp **đã gỡ** là lịch sử, không phải thứ đang đính — nó không được chặn mãi mãi."""
    source = _partner(client, editor)
    target = _partner(client, editor)
    ids = branch_ids(session_factory, dataset_alpha, merge_branches)
    branch = ids[merge_branches[0]]
    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(branch,),
        acting_branch_id=branch,
    )
    with unit_of_work(session_factory, scope) as session:
        session.add(
            Attachment(
                entity_type="partners",
                entity_id=str(source["id"]),
                content_hash="c" * 64,
                byte_size=10,
                media_type="application/pdf",
                file_name="da-go.pdf",
                branch_id=branch,
                uploaded_by=1,
                detached_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

    response = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 200, response.text


@db
def test_merging_two_partners_that_both_have_bank_accounts(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Ca thường gặp nhất của FR-SYS-016 — và ca mà bản đầu tiên đổ (review H1).

    Cả hai bản ghi đều có tài khoản mặc định. Bản ghi được giữ lại là bản quyết
    định nên mặc định của nó thắng; tài khoản của nguồn chuyển sang dưới dạng
    tài khoản thường.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    bank = create_record(client, editor, BANKS, {"code": unique_code("NH"), "name": "NH gộp"})
    bank_id = int(bank.json()["id"])
    kept = _add_account(client, editor, int(target["id"]), bank_id, number="8100001")
    moved = _add_account(client, editor, int(source["id"]), bank_id, number="8100002")
    assert kept.json()["is_default"] is True
    assert moved.json()["is_default"] is True

    response = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 200, response.text
    listing = client.get(
        f"/api/v1/master/{PARTNERS}/{target['id']}/bank-accounts", headers=editor
    ).json()["items"]
    by_number = {item["account_number"]: item for item in listing}
    assert set(by_number) == {"8100001", "8100002"}
    assert [item["account_number"] for item in listing if item["is_default"]] == ["8100001"]


@db
def test_merging_folds_a_bank_account_declared_on_both_sides(
    client: TestClient, editor: dict[str, str]
) -> None:
    """Cùng số tài khoản ở cùng ngân hàng = **một** tài khoản khai hai lần.

    Đó chính là dấu hiệu "hai bản ghi này là một", nên gộp phải hợp nhất chúng
    chứ không từ chối vì một ràng buộc duy nhất mà người dùng không nhìn thấy.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    bank = create_record(client, editor, BANKS, {"code": unique_code("NH"), "name": "NH gộp"})
    bank_id = int(bank.json()["id"])
    _add_account(client, editor, int(target["id"]), bank_id, number="8200001")
    _add_account(client, editor, int(source["id"]), bank_id, number="8200001")

    response = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 200, response.text
    listing = client.get(
        f"/api/v1/master/{PARTNERS}/{target['id']}/bank-accounts", headers=editor
    ).json()["items"]
    assert [item["account_number"] for item in listing] == ["8200001"]
    assert listing[0]["is_default"] is True


@db
def test_a_conflicting_child_row_is_refused_with_a_usable_message(
    client: TestClient,
    editor: dict[str, str],
    owner_engine: Engine,
    dataset_alpha: DatasetRef,
    probe_unique_table: str,
) -> None:
    """Bảng con **chưa** khai `MergeHook` mà đụng ràng buộc duy nhất → câu nêu việc phải làm.

    Không có lưới này thì người dùng nhận `409 data.duplicate` kèm tên một chỉ
    mục nội bộ — đúng thứ `MasterDataMergeRefusedError` được dựng ra để thay thế
    ở các ca từ chối khác.
    """
    source = _partner(client, editor)
    target = _partner(client, editor)
    schema = dataset_alpha.schema_name
    with owner_engine.begin() as connection:
        connection.execute(text(f'SET search_path TO "{schema}"'))
        connection.execute(
            text(
                f"INSERT INTO {probe_unique_table} (partner_id, code) VALUES (:a, 'X'), (:b, 'X')"
            ),
            {"a": source["id"], "b": target["id"]},
        )

    response = _merge(client, editor, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "master_data.merge_refused"
    assert body["details"]["reason"] == "conflicting_child_rows"
    assert body["details"]["child_table"] == probe_unique_table


@db
def test_merging_needs_the_delete_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    editor_without_delete: str,
    merge_branches: list[str],
    test_password: str,
    editor: dict[str, str],
) -> None:
    """Kết quả của gộp là một bản ghi **biến mất** — quyền sửa không đủ."""
    source = _partner(client, editor)
    target = _partner(client, editor)
    limited = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        editor_without_delete,
        "khongxoa",
        test_password,
        branch_codes=[merge_branches[0]],
    )

    response = _merge(client, limited, source_id=int(source["id"]), target_id=int(target["id"]))

    assert response.status_code == 403, response.text
