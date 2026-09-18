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


def test_acknowledge_warnings_reaches_the_posting_service_through_http(
    client: TestClient, author_headers: dict[str, str], context: PostingContext
) -> None:
    """Dây `?acknowledge_warnings=` từ query tới `PostingService.post` (review
    6A, M5 — mutation cắt dây này trước đó không làm test nào đỏ): lượt đầu 422
    mang dấu `warning=1`, lượt gửi lại kèm xác nhận thì 200 và lên sổ."""
    from collections.abc import Sequence

    from ket.kernel.errors import PostingViolation
    from ket.posting.contracts import GUARD_REGISTRY, GuardFinding, PostingGuard, Voucher
    from ket.posting.engine.guards import GUARD_WARNING_DETAIL
    from ket.posting.engine.prepared import PreparedLine

    class _HttpProbeGuard:
        def check(
            self, session: Session, *, voucher: Voucher, lines: Sequence[PreparedLine]
        ) -> Sequence[GuardFinding]:
            return (
                GuardFinding(
                    violation=PostingViolation("guard.http_probe", "cảnh báo qua HTTP"),
                    blocking=False,
                ),
            )

    guard: PostingGuard = _HttpProbeGuard()
    GUARD_REGISTRY.register(guard)
    try:
        created = _create(client, author_headers, context)
        assert created.status_code == 201, created.text
        voucher = created.json()

        refused = client.post(
            f"/api/v1/vouchers/{voucher['id']}/actions/post",
            headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert refused.status_code == 422, refused.text
        violations = refused.json()["violations"]
        assert [v["code"] for v in violations] == ["guard.http_probe"]
        assert violations[0]["details"][GUARD_WARNING_DETAIL] == 1

        acknowledged = client.post(
            f"/api/v1/vouchers/{voucher['id']}/actions/post",
            params={"acknowledge_warnings": "true"},
            headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["status"] == 2

        unposted = client.post(
            f"/api/v1/vouchers/{voucher['id']}/actions/unpost",
            headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
        )
        assert unposted.status_code == 200, unposted.text
        client.delete(f"/api/v1/vouchers/{voucher['id']}", headers=author_headers)
    finally:
        GUARD_REGISTRY._guards.remove(guard)


# ------------------------------------------------ khối đối trừ trên form GLE (7H-2b)

OFFSET_PARTNER_ID = 772_001
OFFSET_AS_OF = "2026-03-31"


def _ensure_offset_partner(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> None:
    """Một đối tác vừa khách vừa NCC — để bốn ca của luật chạy trên cùng một mã."""
    from ket.kernel.master_data.models.partner import Partner
    from ket.kernel.persistence.unit_of_work import unit_of_work
    from posting_support import posting_scope

    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        if session.get(Partner, OFFSET_PARTNER_ID) is None:
            session.add(
                Partner(
                    id=OFFSET_PARTNER_ID,
                    code="DT7720",
                    name="Đối tác DT7720",
                    path=f"{OFFSET_PARTNER_ID}.",
                    is_customer=True,
                    is_vendor=True,
                )
            )
            session.flush()


def _post_debt_voucher(
    client: TestClient,
    headers: dict[str, str],
    context: PostingContext,
    *,
    debit: tuple[str, int | None],
    credit: tuple[str, int | None],
    amount: str,
) -> str:
    """Cất + ghi sổ một chứng từ hai dòng; phần tử thứ hai của mỗi cặp là
    `partner_kind` gắn vào dòng đó (None = dòng không mang đối tác)."""

    def line(account: str, partner_kind: int | None, side: str) -> dict[str, Any]:
        body: dict[str, Any] = {"account_id": context.accounts[account], side: amount}
        if partner_kind is not None:
            body["partner_id"] = OFFSET_PARTNER_ID
            body["partner_kind"] = partner_kind
        return body

    body = _body(context)
    body["lines"] = [line(*debit, "debit_fc"), line(*credit, "credit_fc")]
    created = client.post(
        "/api/v1/gl/journal-vouchers",
        json=body,
        headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert created.status_code == 201, created.text
    voucher_id: str = created.json()["id"]
    posted = client.post(
        f"/api/v1/vouchers/{voucher_id}/actions/post",
        headers={**headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert posted.status_code == 200, posted.text
    return voucher_id


def _open_for_line(
    client: TestClient,
    headers: dict[str, str],
    context: PostingContext,
    *,
    account: str,
    partner_kind: int,
    on_debit: bool,
    branch_id: int | None = None,
    currency_code: str = "VND",
    account_id: int | None = None,
) -> httpx.Response:
    return client.get(
        "/api/v1/gl/journal-vouchers/open-invoices",
        params={
            "partner_kind": partner_kind,
            "partner_id": OFFSET_PARTNER_ID,
            "account_id": context.accounts[account] if account_id is None else account_id,
            "on_debit": on_debit,
            "currency_code": currency_code,
            "branch_id": context.branch_id if branch_id is None else branch_id,
            "as_of": OFFSET_AS_OF,
        },
        headers=headers,
    )


def _entry_ids(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    voucher_id: str,
) -> set[str]:
    from uuid import UUID

    from sqlalchemy import select

    from ket.kernel.persistence.unit_of_work import unit_of_work
    from ket.modules.receivables.models import ArApLedgerEntry
    from posting_support import posting_scope

    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        return {
            str(row)
            for row in session.scalars(
                select(ArApLedgerEntry.id).where(ArApLedgerEntry.document_id == UUID(voucher_id))
            ).all()
        }


def test_open_invoices_for_a_journal_line_follow_the_side_of_the_line(
    client: TestClient,
    author_headers: dict[str, str],
    reader_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Bốn ca của luật hai trục (7C-4) nhìn qua endpoint của khối đối trừ:
    bên NGƯỢC thấy khoản nợ, bên THUẬN thấy khoản ứng trước, và không ca nào
    thấy đích của ca kia — danh sách trả về đúng tập `price_settlements` nhận."""
    _ensure_offset_partner(session_factory, dataset_alpha, context)
    customer, vendor = 0, 1
    # Nợ 131 khách / Có 642: khoản phải thu ghi tay.
    receivable = _post_debt_voucher(
        client,
        author_headers,
        context,
        debit=("131", customer),
        credit=("642", None),
        amount="45000",
    )
    # Nợ 642 / Có 331 NCC: khoản phải trả ghi tay.
    payable = _post_debt_voucher(
        client, author_headers, context, debit=("642", None), credit=("331", vendor), amount="65000"
    )
    # Nợ 111 / Có 131 khách, không đích: khách ứng trước.
    customer_advance = _post_debt_voucher(
        client,
        author_headers,
        context,
        debit=("111", None),
        credit=("131", customer),
        amount="30000",
    )
    # Nợ 331 NCC / Có 111, không đích: ta ứng trước cho NCC.
    vendor_advance = _post_debt_voucher(
        client, author_headers, context, debit=("331", vendor), credit=("111", None), amount="20000"
    )
    ids = {
        name: _entry_ids(session_factory, dataset_alpha, context, voucher_id)
        for name, voucher_id in (
            ("receivable", receivable),
            ("payable", payable),
            ("customer_advance", customer_advance),
            ("vendor_advance", vendor_advance),
        )
    }
    assert all(len(found) == 1 for found in ids.values()), ids

    def targets(response: httpx.Response) -> set[str]:
        assert response.status_code == 200, response.text
        return {item["target_id"] for item in response.json()["items"]}

    # Có 131 khách (bên ngược) → khoản nợ, không phải khoản ứng trước.
    seen = targets(
        _open_for_line(
            client, reader_headers, context, account="131", partner_kind=customer, on_debit=False
        )
    )
    assert ids["receivable"] <= seen and not (ids["customer_advance"] & seen)
    # Nợ 131 khách (bên thuận) → chỉ khoản ứng trước.
    seen = targets(
        _open_for_line(
            client, reader_headers, context, account="131", partner_kind=customer, on_debit=True
        )
    )
    assert ids["customer_advance"] <= seen and not (ids["receivable"] & seen)
    # Nợ 331 NCC (bên ngược) → khoản phải trả.
    seen = targets(
        _open_for_line(
            client, reader_headers, context, account="331", partner_kind=vendor, on_debit=True
        )
    )
    assert ids["payable"] <= seen and not (ids["vendor_advance"] & seen)
    # Có 331 NCC (bên thuận) → khoản đã ứng cho NCC.
    seen = targets(
        _open_for_line(
            client, reader_headers, context, account="331", partner_kind=vendor, on_debit=False
        )
    )
    assert ids["vendor_advance"] <= seen and not (ids["payable"] & seen)
    # Mọi dòng trả về đứng trên đúng TK của dòng hỏi.
    for item in _open_for_line(
        client, reader_headers, context, account="131", partner_kind=customer, on_debit=False
    ).json()["items"]:
        assert item["account_id"] == context.accounts["131"]


def test_open_invoices_for_a_non_debt_line_is_empty_not_an_error(
    client: TestClient, reader_headers: dict[str, str], context: PostingContext
) -> None:
    """TK 642 không theo dõi khách hàng: dòng không phải dòng công nợ, danh sách
    rỗng — ô người dùng gõ chưa xong không phải một lỗi."""
    response = _open_for_line(
        client, reader_headers, context, account="642", partner_kind=0, on_debit=True
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_open_invoices_outside_the_branch_scope_is_refused(
    client: TestClient, reader_headers: dict[str, str], context: PostingContext
) -> None:
    response = _open_for_line(
        client,
        reader_headers,
        context,
        account="131",
        partner_kind=0,
        on_debit=False,
        branch_id=context.branch_id + 987_654,
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "auth.branch_not_in_scope"


def test_open_invoices_requires_the_journal_view_permission(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> None:
    role = ensure_role(session_factory, dataset_alpha, "gle_khong_quyen_xem", [])
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "gl_nobody",
        test_password,
        branch_codes=[context.branch_code],
    )
    response = _open_for_line(
        client, headers, context, account="131", partner_kind=0, on_debit=False
    )
    assert response.status_code == 403


def test_open_invoices_filter_by_currency_and_by_the_account_of_the_line(
    client: TestClient,
    author_headers: dict[str, str],
    reader_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Hai trục lọc mà `_check_target` sẽ từ chối lúc cất (review 7H-2b M-1/M-2):
    khoản nợ VND không hiện cho dòng USD, và khoản nợ treo trên TK phải thu
    KHÁC (cùng khách, cùng chiều) không hiện cho dòng ghi TK 131 — bỏ một
    trong hai phép lọc thì đúng một bài đỏ."""
    from sqlalchemy import select

    from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount
    from ket.kernel.persistence.unit_of_work import unit_of_work
    from posting_support import posting_scope

    _ensure_offset_partner(session_factory, dataset_alpha, context)
    customer = 0
    # TK phải thu thứ hai theo dõi khách hàng — gói test chỉ có một, nên phép
    # lọc `account_id` không có gì để loại nếu không gieo thêm.
    scope = posting_scope(dataset_alpha, context, user_id=1)
    with unit_of_work(session_factory, scope) as session:
        other = session.scalar(
            select(ChartOfAccount).where(
                ChartOfAccount.package_id == context.package_id, ChartOfAccount.code == "1312"
            )
        )
        if other is None:
            other = ChartOfAccount(
                package_id=context.package_id,
                code="1312",
                name="Phải thu khách hàng — TK thứ hai",
                path="0.",
                balance_nature=BalanceNature.DUAL,
                is_summary=False,
                detail_tracking=["customer"],
            )
            session.add(other)
            session.flush()
            other.path = f"{other.id}."
            session.flush()
        other_account_id = other.id

    on_131 = _post_debt_voucher(
        client,
        author_headers,
        context,
        debit=("131", customer),
        credit=("642", None),
        amount="12000",
    )
    body = _body(context)
    body["lines"] = [
        {
            "account_id": other_account_id,
            "debit_fc": "13000",
            "partner_id": OFFSET_PARTNER_ID,
            "partner_kind": customer,
        },
        {"account_id": context.accounts["642"], "credit_fc": "13000"},
    ]
    created = client.post(
        "/api/v1/gl/journal-vouchers",
        json=body,
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert created.status_code == 201, created.text
    on_1312 = created.json()["id"]
    posted = client.post(
        f"/api/v1/vouchers/{on_1312}/actions/post",
        headers={**author_headers, IDEMPOTENCY_HEADER: uuid4().hex},
    )
    assert posted.status_code == 200, posted.text
    ids_131 = _entry_ids(session_factory, dataset_alpha, context, on_131)
    ids_1312 = _entry_ids(session_factory, dataset_alpha, context, on_1312)
    assert len(ids_131) == 1 and len(ids_1312) == 1

    def targets(response: httpx.Response) -> set[str]:
        assert response.status_code == 200, response.text
        return {item["target_id"] for item in response.json()["items"]}

    # Dòng Có 131: thấy khoản trên 131, không thấy khoản trên 1312.
    seen = targets(
        _open_for_line(
            client, reader_headers, context, account="131", partner_kind=customer, on_debit=False
        )
    )
    assert ids_131 <= seen and not (ids_1312 & seen)
    # Dòng Có 1312: ngược lại.
    seen = targets(
        _open_for_line(
            client,
            reader_headers,
            context,
            account="131",
            partner_kind=customer,
            on_debit=False,
            account_id=other_account_id,
        )
    )
    assert ids_1312 <= seen and not (ids_131 & seen)
    # Dòng USD: khoản nợ VND không hiện.
    seen = targets(
        _open_for_line(
            client,
            reader_headers,
            context,
            account="131",
            partner_kind=customer,
            on_debit=False,
            currency_code="USD",
        )
    )
    assert not (ids_131 & seen)
