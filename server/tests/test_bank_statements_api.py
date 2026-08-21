"""Sao kê + đối chiếu qua HTTP (lát 6D) — những gì chỉ lộ ở tầng router.

Luật nghiệp vụ đã kiểm ở `test_bank_statement_import_and_reconciliation.py`;
ở đây kiểm: multipart nhập + kho tệp, bộ quyền `bank.statement.*` riêng khỏi
quyền chứng từ (FR-BNK-022), và chuỗi hành động khớp/gỡ/xóa đi trọn qua HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from bank_support import ensure_company_bank_account, seed_bank_package_data
from cash_book_support import seed_cash_book_package_data
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.kernel.bank_import.profile_models import BankStatementProfile, StatementFileKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

STATEMENT_ROLE = "doi_chieu_ngan_hang"
ACTOR_ID = 1


def _statement_codes() -> list[str]:
    return [
        permission_code("bank", "statement", action)
        for action in (Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE)
    ]


@pytest.fixture(scope="module")
def statements_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("bank-statements")


@pytest.fixture
def client(
    test_settings: Settings,
    app_engine: Engine,
    session_factory: sessionmaker[Session],
    statements_dir: Path,
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    settings = test_settings.model_copy(update={"attachments_dir": statements_dir})
    with api_test_client(create_app(settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    seeded = seed_cash_book_package_data(session_factory, dataset_alpha, context)
    seed_bank_package_data(session_factory, dataset_alpha, context)
    return seeded


@pytest.fixture(scope="module")
def bank_account(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    return ensure_company_bank_account(
        session_factory, dataset_alpha, context, code="0041-BANK-STMT"
    )


@pytest.fixture(scope="module")
def profile_id(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    bank_account: int,
) -> int:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        account = session.get(CompanyBankAccount, bank_account)
        assert account is not None
        existing = session.scalar(
            select(BankStatementProfile).where(
                BankStatementProfile.bank_id == account.bank_id,
                BankStatementProfile.name == "CSV http 6D",
            )
        )
        if existing is not None:
            return existing.id
        profile = BankStatementProfile(
            bank_id=account.bank_id,
            name="CSV http 6D",
            file_kind=StatementFileKind.CSV,
            header_row=1,
            date_col="Ngay GD",
            date_format="%d/%m/%Y",
            debit_col="Ghi no",
            credit_col="Ghi co",
            ref_col="So CT",
            description_col="Dien giai",
            decimal_sep=".",
            thousand_sep=None,
            csv_delimiter=";",
        )
        session.add(profile)
        session.flush()
        return profile.id


def _all_branch_codes(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> list[str]:
    """Khớp tự động đòi phạm vi MỌI chi nhánh (guard M-1) — người đối chiếu
    của test cầm trọn danh sách chi nhánh hiện có của dataset dùng chung."""
    from ket.kernel.security.models import Branch

    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        return list(session.scalars(select(Branch.code)).all())


@pytest.fixture
def clerk_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    role = ensure_role(session_factory, dataset_alpha, STATEMENT_ROLE, _statement_codes())
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "stmt_clerk",
        test_password,
        branch_codes=_all_branch_codes(session_factory, dataset_alpha, context),
    )


@pytest.fixture
def viewer_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    """Chỉ `bank.statement.view` — mọi hành động khớp phải 403 (M17)."""
    role = ensure_role(
        session_factory,
        dataset_alpha,
        "xem_sao_ke",
        [permission_code("bank", "statement", Action.VIEW)],
    )
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "stmt_viewer",
        test_password,
        branch_codes=[context.branch_code],
    )


@pytest.fixture
def narrow_clerk_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    """Đủ quyền statement nhưng chỉ MỘT chi nhánh — auto-match phải 403 (M-1)."""
    role = ensure_role(session_factory, dataset_alpha, STATEMENT_ROLE, _statement_codes())
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "stmt_narrow",
        test_password,
        branch_codes=[context.branch_code],
    )


@pytest.fixture
def banker_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    """Vai trò chứng từ tiền gửi ĐẦY ĐỦ nhưng không có `bank.statement.*`."""
    codes = [
        permission_code("bank", name, action)
        for name in ("credit_advice", "payment_order")
        for action in (Action.VIEW, Action.CREATE, Action.POST)
    ]
    role = ensure_role(session_factory, dataset_alpha, "chi_chung_tu_bank", codes)
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "voucher_only",
        test_password,
        branch_codes=[context.branch_code],
    )


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    bank_account_id: int,
    profile_id: int,
    body: str,
    name: str = "sao-ke.csv",
) -> object:
    return client.post(
        "/api/v1/bank/statements/import",
        headers=headers,
        data={"bank_account_id": str(bank_account_id), "profile_id": str(profile_id)},
        files={"file": (name, body.encode(), "text/csv")},
    )


CSV_BODY = (
    "Ngay GD;So CT;Dien giai;Ghi no;Ghi co\n"
    "15/01/2026;FT-H1;khach chuyen;;450000\n"
    "16/01/2026;FT-H2;tra ncc;120000;\n"
)


def test_statement_flow_over_http(
    client: TestClient,
    clerk_headers: dict[str, str],
    bank_account: int,
    profile_id: int,
) -> None:
    imported = _upload(
        client, clerk_headers, bank_account_id=bank_account, profile_id=profile_id, body=CSV_BODY
    )
    assert imported.status_code == 201, imported.text  # type: ignore[attr-defined]
    payload = imported.json()  # type: ignore[attr-defined]
    statement_id = payload["statement"]["id"]
    assert payload["line_count"] == 2

    # Nhập lại đúng tệp → 409 duplicate.
    duplicate = _upload(
        client, clerk_headers, bank_account_id=bank_account, profile_id=profile_id, body=CSV_BODY
    )
    assert duplicate.status_code == 409  # type: ignore[attr-defined]

    listing = client.get(
        "/api/v1/bank/statements",
        headers=clerk_headers,
        params={"bank_account_id": bank_account},
    )
    assert listing.status_code == 200
    assert any(row["id"] == statement_id for row in listing.json()["items"])

    detail = client.get(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers)
    assert detail.status_code == 200
    lines = detail.json()["lines"]
    assert len(lines) == 2

    matched = client.post(
        f"/api/v1/bank/statements/{statement_id}/actions/auto-match", headers=clerk_headers
    )
    assert matched.status_code == 200
    # Không có chứng từ nào trong dataset khớp số tiền này — 0 khớp là kết quả
    # đúng; điều đang kiểm là chuỗi hành động đi trọn qua router.
    assert matched.json()["matched"] == 0

    candidates = client.get(
        f"/api/v1/bank/statements/lines/{lines[0]['id']}/candidates", headers=clerk_headers
    )
    assert candidates.status_code == 200

    reconciliation = client.get(
        "/api/v1/bank/reconciliation",
        headers=clerk_headers,
        params={"bank_account_id": bank_account, "as_of": "2026-01-31"},
    )
    assert reconciliation.status_code == 200
    assert len(reconciliation.json()["unmatched_statement_lines"]) == 2

    removed = client.delete(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers)
    assert removed.status_code == 204

    gone = client.get(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers)
    assert gone.status_code == 404


def test_statement_permission_is_separate_from_voucher_permission(
    client: TestClient,
    banker_headers: dict[str, str],
    bank_account: int,
    profile_id: int,
) -> None:
    """FR-BNK-022 tinh thần: người lập chứng từ không đương nhiên nhập sao kê."""
    refused = _upload(
        client, banker_headers, bank_account_id=bank_account, profile_id=profile_id, body=CSV_BODY
    )
    assert refused.status_code == 403  # type: ignore[attr-defined]

    listing = client.get(
        "/api/v1/bank/statements",
        headers=banker_headers,
        params={"bank_account_id": bank_account},
    )
    assert listing.status_code == 403


def test_match_actions_require_edit_permission(
    client: TestClient,
    clerk_headers: dict[str, str],
    viewer_headers: dict[str, str],
    bank_account: int,
    profile_id: int,
) -> None:
    """Review 6D, M17: ba endpoint khớp phải đòi `bank.statement.edit` —
    quyền VIEW chỉ xem, đột biến hạ cổng xuống VIEW phải bị bắt ở đây."""
    body = "Ngay GD;So CT;Dien giai;Ghi no;Ghi co\n15/01/2026;PM-1;xem thoi;;50000\n"
    imported = _upload(
        client, clerk_headers, bank_account_id=bank_account, profile_id=profile_id, body=body
    )
    assert imported.status_code == 201, imported.text  # type: ignore[attr-defined]
    statement_id = imported.json()["statement"]["id"]  # type: ignore[attr-defined]
    detail = client.get(f"/api/v1/bank/statements/{statement_id}", headers=viewer_headers)
    assert detail.status_code == 200  # VIEW xem được
    line_id = detail.json()["lines"][0]["id"]

    from uuid import uuid4

    assert (
        client.post(
            f"/api/v1/bank/statements/{statement_id}/actions/auto-match", headers=viewer_headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/bank/statements/lines/{line_id}/actions/match",
            headers=viewer_headers,
            json={"voucher_id": str(uuid4())},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/bank/statements/lines/{line_id}/actions/unmatch", headers=viewer_headers
        ).status_code
        == 403
    )
    # VIEW cũng không xóa được.
    assert (
        client.delete(f"/api/v1/bank/statements/{statement_id}", headers=viewer_headers).status_code
        == 403
    )
    # Dọn bằng người đủ quyền.
    assert (
        client.delete(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers).status_code
        == 204
    )


def test_auto_match_requires_full_branch_scope(
    client: TestClient,
    clerk_headers: dict[str, str],
    narrow_clerk_headers: dict[str, str],
    bank_account: int,
    profile_id: int,
) -> None:
    """Review 6D, M-1: sao kê là dữ liệu mức tài khoản còn chứng từ dưới RLS
    chi nhánh — người phạm vi hẹp chạy khớp tự động sẽ khớp nhầm tất định vào
    ứng viên duy nhất còn nhìn thấy, nên bị chặn bằng 403 có thông điệp."""
    body = "Ngay GD;So CT;Dien giai;Ghi no;Ghi co\n15/01/2026;SC-1;scope;;60000\n"
    imported = _upload(
        client, clerk_headers, bank_account_id=bank_account, profile_id=profile_id, body=body
    )
    assert imported.status_code == 201, imported.text  # type: ignore[attr-defined]
    statement_id = imported.json()["statement"]["id"]  # type: ignore[attr-defined]

    refused = client.post(
        f"/api/v1/bank/statements/{statement_id}/actions/auto-match",
        headers=narrow_clerk_headers,
    )
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "bank_statement.scope_insufficient"

    allowed = client.post(
        f"/api/v1/bank/statements/{statement_id}/actions/auto-match", headers=clerk_headers
    )
    assert allowed.status_code == 200

    assert (
        client.delete(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers).status_code
        == 204
    )
