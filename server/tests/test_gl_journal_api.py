"""Chứng từ nghiệp vụ khác qua HTTP — hợp đồng API của phase 4 (slice 4A).

Kiểm phần **đi qua HTTP**: phân quyền theo `{module}.{loại}.{hành vi}`,
idempotency của đường tạo và đường hành động, khóa lạc quan trả `409`, và
endpoint hành động dùng chung tra đúng quyền theo `document_type`. Luật ghi sổ
đã có tệp riêng (`test_posting_engine_flow.py`) — không kiểm lại ở đây.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.general_ledger.journal import (
    JOURNAL_PERMISSION_CODE,
    JOURNAL_PERMISSION_MODULE,
)
from ket.settings import Settings
from posting_support import PostingContext, seed_posting_context

pytestmark = pytest.mark.db

AUTHOR_ROLE = "ke_toan_ghi_so"
READER_ROLE = "chi_xem_chung_tu"


def _codes(*actions: Action) -> list[str]:
    return [
        permission_code(JOURNAL_PERMISSION_MODULE, JOURNAL_PERMISSION_CODE, action)
        for action in actions
    ]


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def author_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(
        session_factory,
        dataset_alpha,
        AUTHOR_ROLE,
        _codes(
            Action.VIEW,
            Action.CREATE,
            Action.EDIT,
            Action.DELETE,
            Action.POST,
            Action.UNPOST,
        ),
    )


@pytest.fixture(scope="module")
def reader_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, READER_ROLE, _codes(Action.VIEW))


@pytest.fixture
def author_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    author_role: str,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        author_role,
        "gl_author",
        test_password,
        branch_codes=[context.branch_code],
    )


@pytest.fixture
def reader_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    reader_role: str,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        reader_role,
        "gl_reader",
        test_password,
        branch_codes=[context.branch_code],
    )


def _body(context: PostingContext) -> dict[str, Any]:
    return {
        "branch_id": context.branch_id,
        "document_date": "2026-03-05",
        "posting_date": "2026-03-05",
        "currency_code": "VND",
        "exchange_rate": "1",
        "description": "chứng từ qua HTTP",
        "lines": [
            {"account_id": context.accounts["642"], "debit_fc": "250000"},
            {"account_id": context.accounts["111"], "credit_fc": "250000"},
        ],
    }


def _create(
    client: TestClient,
    headers: dict[str, str],
    context: PostingContext,
    *,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/api/v1/gl/journal-vouchers",
        json=_body(context),
        headers={**headers, IDEMPOTENCY_HEADER: key or uuid4().hex},
    )


def test_create_read_post_unpost_delete_through_http(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    created = _create(client, author_headers, context)
    assert created.status_code == 201, created.text
    voucher = created.json()
    assert voucher["voucher_no"].startswith("GLE")
    assert voucher["status"] == 1
    assert len(voucher["lines"]) == 2

    fetched = client.get(f"/api/v1/gl/journal-vouchers/{voucher['id']}", headers=author_headers)
    assert fetched.status_code == 200

    posted = client.post(
        f"/api/v1/vouchers/{voucher['id']}/actions/post",
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["status"] == 2
    assert posted.json()["posted_by"] is not None

    unposted = client.post(
        f"/api/v1/vouchers/{voucher['id']}/actions/unpost",
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert unposted.status_code == 200, unposted.text
    assert unposted.json()["status"] == 1

    deleted = client.delete(f"/api/v1/vouchers/{voucher['id']}", headers=author_headers)
    assert deleted.status_code == 204, deleted.text
    assert (
        client.get(
            f"/api/v1/gl/journal-vouchers/{voucher['id']}", headers=author_headers
        ).status_code
        == 404
    )


def test_create_is_idempotent_per_key(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    key = uuid4().hex
    first = _create(client, author_headers, context, key=key)
    assert first.status_code == 201, first.text
    replay = _create(client, author_headers, context, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["voucher_no"] == first.json()["voucher_no"]


def test_missing_idempotency_key_is_refused(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    response = client.post(
        "/api/v1/gl/journal-vouchers", json=_body(context), headers=author_headers
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "idempotency.key_missing"


def test_reader_cannot_write_but_can_list(
    client: TestClient,
    author_headers: dict[str, str],
    reader_headers: dict[str, str],
    context: PostingContext,
) -> None:
    created = _create(client, author_headers, context)
    assert created.status_code == 201

    refused = _create(client, reader_headers, context)
    assert refused.status_code == 403

    refused_post = client.post(
        f"/api/v1/vouchers/{created.json()['id']}/actions/post",
        headers={**reader_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert refused_post.status_code == 403

    listing = client.get("/api/v1/vouchers?type=GLE", headers=reader_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] >= 1

    # Không nêu `type` → chỉ thấy loại được xem; danh sách vẫn trả về.
    mixed = client.get("/api/v1/vouchers", headers=reader_headers)
    assert mixed.status_code == 200
    assert all(item["document_type"] == "GLE" for item in mixed.json()["items"])


def test_stale_row_version_returns_conflict(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    created = _create(client, author_headers, context).json()
    body = {**_body(context), "row_version": created["row_version"] + 41}
    response = client.put(
        f"/api/v1/gl/journal-vouchers/{created['id']}", json=body, headers=author_headers
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "concurrency.row_version_conflict"


def test_updating_a_posted_voucher_is_refused_with_the_next_step(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    created = _create(client, author_headers, context).json()
    client.post(
        f"/api/v1/vouchers/{created['id']}/actions/post",
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    body = {**_body(context), "row_version": created["row_version"]}
    refused = client.put(
        f"/api/v1/gl/journal-vouchers/{created['id']}", json=body, headers=author_headers
    )
    assert refused.status_code == 422
    assert refused.json()["error_code"] == "voucher.invalid_transition"

    refused_delete = client.delete(f"/api/v1/vouchers/{created['id']}", headers=author_headers)
    assert refused_delete.status_code == 422


def test_unbalanced_voucher_returns_all_violations(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    body = _body(context)
    body["lines"][0]["debit_fc"] = "260000"
    created = client.post(
        "/api/v1/gl/journal-vouchers",
        json=body,
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert created.status_code == 201  # Cất được — lệch chỉ chặn lúc ghi sổ

    refused = client.post(
        f"/api/v1/vouchers/{created.json()['id']}/actions/post",
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert refused.status_code == 422
    problem = refused.json()
    assert problem["error_code"] == "posting.invalid"
    codes = {violation["code"] for violation in problem["violations"]}
    assert "posting.unbalanced" in codes


def test_action_key_reused_for_another_voucher_is_refused(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    """Vân tay kèm id: một khóa không dùng lại được cho chứng từ thứ hai."""
    first = _create(client, author_headers, context).json()
    second = _create(client, author_headers, context).json()
    key = uuid4().hex
    ok = client.post(
        f"/api/v1/vouchers/{first['id']}/actions/post",
        headers={**author_headers, IDEMPOTENCY_HEADER: key},
    )
    assert ok.status_code == 200
    reused = client.post(
        f"/api/v1/vouchers/{second['id']}/actions/post",
        headers={**author_headers, IDEMPOTENCY_HEADER: key},
    )
    assert reused.status_code == 409
    assert reused.json()["error_code"] == "idempotency.key_reused"


def test_branch_outside_scope_is_refused_with_a_clear_message(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    body = _body(context)
    body["branch_id"] = context.branch_id + 987_654
    response = client.post(
        "/api/v1/gl/journal-vouchers",
        json=body,
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.branch_not_in_scope"


def test_multi_currency_line_amounts_survive_the_json_round_trip(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    """Số tiền đi qua JSON dạng **chuỗi** — không bao giờ thành float."""
    body = _body(context)
    body["currency_code"] = "USD"
    body["exchange_rate"] = "25000.5"
    body["lines"] = [
        {
            "account_id": context.accounts["112"],
            "debit_fc": "100.33",
            "currency_code": "USD",
            "exchange_rate": "25000.5",
        },
        {
            "account_id": context.accounts["111"],
            "credit_fc": "100.33",
            "currency_code": "USD",
            "exchange_rate": "25000.5",
        },
    ]
    created = client.post(
        "/api/v1/gl/journal-vouchers",
        json=body,
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert created.status_code == 201, created.text
    line = created.json()["lines"][0]
    assert Decimal(line["debit_fc"]) == Decimal("100.33")
    assert Decimal(line["exchange_rate"]) == Decimal("25000.5")


def test_amounts_finer_than_the_money_scale_are_refused_at_the_gate(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    """M1 review 4A: `0.004` sẽ bị `NUMERIC(18,2)` làm tròn âm thầm — chặn ở biên."""
    body = _body(context)
    body["lines"][0]["debit_fc"] = "0.004"
    response = client.post(
        "/api/v1/gl/journal-vouchers",
        json=body,
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert response.status_code == 422


def test_save_also_posts_demands_the_post_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> None:
    """M4 review 4A: bật FR-SYS-061 thì người chỉ có `create` không đi vòng
    qua quyền `post` được — Cất-đồng-thời-ghi-sổ là một lần ghi sổ thật."""
    from ket.kernel.config.catalog import SAVE_ALSO_POSTS_KEY
    from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
    from ket.kernel.security.models import Setting

    drafter_role = ensure_role(
        session_factory,
        dataset_alpha,
        "ke_toan_chi_cat",
        _codes(Action.VIEW, Action.CREATE, Action.EDIT),
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        drafter_role,
        "gl_drafter",
        test_password,
        branch_codes=[context.branch_code],
    )

    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())

    def _set(raw_value: str) -> None:
        from sqlalchemy import select as sa_select

        with unit_of_work(session_factory, scope) as session:
            row = session.scalar(
                sa_select(Setting).where(
                    Setting.key == SAVE_ALSO_POSTS_KEY, Setting.scope == "system"
                )
            )
            if row is None:
                session.add(
                    Setting(
                        scope="system",
                        key=SAVE_ALSO_POSTS_KEY,
                        value=raw_value,
                        value_type="boolean",
                    )
                )
            else:
                row.value = raw_value

    _set("true")
    try:
        refused = client.post(
            "/api/v1/gl/journal-vouchers",
            json=_body(context),
            headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert refused.status_code == 403, refused.text
    finally:
        _set("false")

    # Tắt tùy chọn thì người chỉ-Cất lại làm việc bình thường.
    allowed = client.post(
        "/api/v1/gl/journal-vouchers",
        json=_body(context),
        headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert allowed.status_code == 201, allowed.text


def test_replaying_an_action_still_requires_the_view_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> None:
    """M5 review 4A: `replay` đọc lại bản ghi thật nên mọi lớp kiểm quyền phải
    chạy — người vừa mất quyền xem không rút được phản hồi cũ qua khóa idempotency."""
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select as sa_select

    from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
    from ket.kernel.security.models import Permission, Role, RolePermission

    role_code = f"gl_tam_{uuid4().hex[:6]}"
    ensure_role(
        session_factory,
        dataset_alpha,
        role_code,
        _codes(Action.VIEW, Action.CREATE, Action.POST),
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role_code,
        "gl_revoked",
        test_password,
        branch_codes=[context.branch_code],
    )

    created = _create(client, headers, context).json()
    key = uuid4().hex
    first = client.post(
        f"/api/v1/vouchers/{created['id']}/actions/post",
        headers={**headers, IDEMPOTENCY_HEADER: key},
    )
    assert first.status_code == 200, first.text

    view_code = _codes(Action.VIEW)[0]
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(session_factory, scope) as session:
        role_id = session.scalar(sa_select(Role.id).where(Role.code == role_code))
        permission_id = session.scalar(sa_select(Permission.id).where(Permission.code == view_code))
        session.execute(
            sa_delete(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission_id,
            )
        )

    replay = client.post(
        f"/api/v1/vouchers/{created['id']}/actions/post",
        headers={**headers, IDEMPOTENCY_HEADER: key},
    )
    assert replay.status_code == 403, replay.text
