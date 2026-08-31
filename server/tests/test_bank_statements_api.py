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
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.settings import Settings
from posting_support import (
    PostingContext,
    ensure_second_branch,
    posting_scope,
    seed_posting_context,
)

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
    của test cầm trọn danh sách chi nhánh hiện có của dataset dùng chung.

    Tự dựng chi nhánh thứ hai nếu dataset chưa có (lát 6G-1): `dataset_alpha`
    dùng chung nên chạy CẢ BỘ thì tệp khác đã tạo sẵn vài chi nhánh, còn chạy
    RIÊNG tệp này thì chỉ có một — lúc ấy "trọn phạm vi" và "phạm vi hẹp" là
    cùng một tập và `test_auto_match_requires_full_branch_scope` đỏ vì lý do
    không liên quan gì tới thứ nó kiểm. Bộ test không được phụ thuộc thứ tự tệp.
    """
    from ket.kernel.security.models import Branch

    ensure_second_branch(session_factory, dataset_alpha)
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


def test_every_reconciliation_door_requires_company_scope(
    client: TestClient,
    clerk_headers: dict[str, str],
    narrow_clerk_headers: dict[str, str],
    bank_account: int,
    profile_id: int,
) -> None:
    """Lát 6G-2 (M-4): đối chiếu là nghiệp vụ phạm vi CÔNG TY, không riêng
    đường khớp tự động.

    Cổng phải nằm ở **mọi** cửa so sổ↔sao kê. Bỏ nó ở một cửa duy nhất là đủ
    để người hẹp chi nhánh dựng nên (hoặc gỡ đi) một cặp khớp dựa trên nửa dữ
    liệu họ thấy — và số chênh lệch của cả tài khoản đi theo. Người hẹp ở đây
    có TRỌN bộ quyền `bank.statement.*`, nên phép kiểm này không thể xanh vì
    lý do thiếu quyền.
    """
    body = "Ngay GD;So CT;Dien giai;Ghi no;Ghi co\n17/01/2026;CS-1;cong ty;;70000\n"
    imported = _upload(
        client, clerk_headers, bank_account_id=bank_account, profile_id=profile_id, body=body
    )
    assert imported.status_code == 201, imported.text  # type: ignore[attr-defined]
    statement_id = imported.json()["statement"]["id"]  # type: ignore[attr-defined]
    detail = client.get(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers)
    assert detail.status_code == 200, detail.text
    line_id = detail.json()["lines"][0]["id"]

    from uuid import uuid4

    doors = (
        client.get(
            f"/api/v1/bank/statements/lines/{line_id}/candidates", headers=narrow_clerk_headers
        ),
        client.post(
            f"/api/v1/bank/statements/lines/{line_id}/actions/match",
            headers=narrow_clerk_headers,
            json={"voucher_id": str(uuid4())},
        ),
        client.post(
            f"/api/v1/bank/statements/lines/{line_id}/actions/unmatch",
            headers=narrow_clerk_headers,
        ),
        client.get(
            "/api/v1/bank/reconciliation",
            headers=narrow_clerk_headers,
            params={"bank_account_id": bank_account, "as_of": "2026-01-31"},
        ),
    )
    for response in doors:
        assert response.status_code == 403, response.text
        assert response.json()["error_code"] == "bank_statement.scope_insufficient"

    # Cùng những cửa ấy, người phạm vi công ty đi qua được — chứng minh 403 ở
    # trên đến từ PHẠM VI chứ không từ một lỗi chung nào khác.
    assert (
        client.get(
            f"/api/v1/bank/statements/lines/{line_id}/candidates", headers=clerk_headers
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/bank/reconciliation",
            headers=clerk_headers,
            params={"bank_account_id": bank_account, "as_of": "2026-01-31"},
        ).status_code
        == 200
    )
    assert (
        client.delete(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers).status_code
        == 204
    )


def test_profiles_endpoint_lists_only_the_accounts_bank(
    client: TestClient,
    clerk_headers: dict[str, str],
    viewer_headers: dict[str, str],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    bank_account: int,
    profile_id: int,
) -> None:
    """`GET /statements/profiles?bank_account_id=` (lát 6F-2): ô chọn hồ sơ của
    màn nhập sao kê. Lọc theo ngân hàng CỦA TÀI KHOẢN — hồ sơ ngân hàng khác
    hiện ra là mời một lượt nhập 422 đoán được trước; và đường tĩnh phải thắng
    đường `{statement_id}` (khai sau nó là 422 "không phải UUID")."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        other_bank = session.scalar(select(Bank).where(Bank.code == "ACB-TEST-6F2"))
        if other_bank is None:
            other_bank = Bank(code="ACB-TEST-6F2", name="Ngân hàng khác", path="0.")
            session.add(other_bank)
            session.flush()
            other_bank.path = f"{other_bank.id}."
        existing = session.scalar(
            select(BankStatementProfile).where(
                BankStatementProfile.bank_id == other_bank.id,
                BankStatementProfile.name == "CSV ngan hang khac",
            )
        )
        if existing is None:
            session.add(
                BankStatementProfile(
                    bank_id=other_bank.id,
                    name="CSV ngan hang khac",
                    file_kind=StatementFileKind.CSV,
                    header_row=1,
                    date_col="Ngay GD",
                    date_format="%d/%m/%Y",
                    debit_col="Ghi no",
                    credit_col="Ghi co",
                    ref_col=None,
                    description_col=None,
                    decimal_sep=".",
                    thousand_sep=None,
                    csv_delimiter=";",
                )
            )

    listed = client.get(
        f"/api/v1/bank/statements/profiles?bank_account_id={bank_account}",
        headers=viewer_headers,
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert profile_id in [row["id"] for row in items]
    assert "CSV ngan hang khac" not in [row["name"] for row in items]

    # Tài khoản không tồn tại → lỗi nghiệp vụ đọc được, không phải 500.
    missing = client.get(
        "/api/v1/bank/statements/profiles?bank_account_id=999999", headers=clerk_headers
    )
    assert missing.status_code == 422
    assert missing.json()["error_code"] == "bank_statement.import_invalid"


def test_every_statement_door_refuses_another_branchs_account(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    bank_account: int,
    narrow_clerk_headers: dict[str, str],
) -> None:
    """Review pre-landing M-2 — bốn cửa gọi `require_bank_account`, ba cửa
    không có test nào.

    Phép đo của review: gỡ HẲN lời gọi khỏi `list_bank_statements` và
    `get_reconciliation` mà cả tệp này vẫn xanh. Cổng phạm vi mới (H-4) vì thế
    có thể biến mất khỏi ba trong bốn cửa mà không ai biết.

    `company_bank_accounts` cố ý không bật RLS nên `session.get` thấy mọi tài
    khoản — cổng ở tầng ứng dụng là lớp duy nhất, và nó phải có mặt ở TỪNG cửa.
    """
    ensure_second_branch(session_factory, dataset_alpha)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        other_branch_id = session.scalar(
            select(Branch.id).where(Branch.id != context.branch_id).order_by(Branch.id).limit(1)
        )
    assert other_branch_id is not None

    seeding = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(context.branch_id, other_branch_id),
        acting_branch_id=other_branch_id,
    )
    with unit_of_work(session_factory, seeding) as session:
        foreign = CompanyBankAccount(
            code="6G-API-CN-KHAC",
            name="TK của chi nhánh khác",
            path="0.",
            bank_id=session.scalar(
                select(CompanyBankAccount.bank_id).where(CompanyBankAccount.id == bank_account)
            ),
            branch_id=other_branch_id,
        )
        session.add(foreign)
        session.flush()
        foreign.path = f"{foreign.id}."
        session.flush()
        foreign_id = foreign.id

    for path in (
        f"/api/v1/bank/statements?bank_account_id={foreign_id}",
        f"/api/v1/bank/reconciliation?bank_account_id={foreign_id}&as_of=2026-01-31",
        f"/api/v1/bank/statements/profiles?bank_account_id={foreign_id}",
    ):
        refused = client.get(path, headers=narrow_clerk_headers)
        assert refused.status_code == 422, f"{path}: {refused.text}"
        assert refused.json()["error_code"] == "bank_statement.import_invalid", path


PROFILE_ADMIN_ROLE = "quan_tri_ho_so_sao_ke"


@pytest.fixture
def profile_admin_headers(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
) -> dict[str, str]:
    role = ensure_role(
        session_factory,
        dataset_alpha,
        PROFILE_ADMIN_ROLE,
        [
            permission_code("bank", "statement_profile", action)
            for action in (Action.VIEW, Action.EDIT)
        ]
        + _statement_codes(),
    )
    return actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        role,
        "profile_admin",
        test_password,
        branch_codes=[context.branch_code],
    )


def _profile_payload(bank_id: int, name: str) -> dict[str, object]:
    return {
        "bank_id": bank_id,
        "name": name,
        "file_kind": "csv",
        "header_row": 1,
        "date_col": "Ngay GD",
        "date_format": "%d/%m/%Y",
        "debit_col": "Ghi no",
        "credit_col": "Ghi co",
        "amount_col": None,
        "sign_rule": None,
        "ref_col": "So CT",
        "description_col": "Dien giai",
        "balance_col": None,
        "decimal_sep": ".",
        "thousand_sep": None,
        "csv_delimiter": ";",
    }


class TestStatementProfileAdministration:
    """Màn khai hồ sơ sao kê (lát 6G-2) — nợ treo từ 6F-2.

    Trước lát này chỉ có đường ĐỌC: khách hàng nhận phần mềm mà không khai được
    "ngân hàng nào, cột nào", nên nhập sao kê chỉ chạy được nếu có dev gieo tay
    một dòng vào DB.
    """

    def _bank_id(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        bank_account: int,
    ) -> int:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            account = session.get(CompanyBankAccount, bank_account)
            assert account is not None
            return account.bank_id

    def test_create_update_and_delete_round_trip(
        self,
        client: TestClient,
        profile_admin_headers: dict[str, str],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        bank_account: int,
    ) -> None:
        bank_id = self._bank_id(session_factory, dataset_alpha, context, bank_account)
        created = client.post(
            "/api/v1/bank/statements/profiles",
            headers=profile_admin_headers,
            json=_profile_payload(bank_id, "Sao ke Internet Banking 6G2"),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["date_format"] == "%d/%m/%Y"

        listed = client.get("/api/v1/bank/statements/profiles/all", headers=profile_admin_headers)
        assert listed.status_code == 200
        assert body["id"] in {item["id"] for item in listed.json()["items"]}

        updated = client.put(
            f"/api/v1/bank/statements/profiles/{body['id']}",
            headers=profile_admin_headers,
            json={
                **_profile_payload(bank_id, "So phu gui qua email 6G2"),
                "row_version": body["row_version"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "So phu gui qua email 6G2"

        # Phiên bản cũ không ghi đè được (khóa lạc quan, cùng khuôn danh mục).
        stale = client.put(
            f"/api/v1/bank/statements/profiles/{body['id']}",
            headers=profile_admin_headers,
            json={
                **_profile_payload(bank_id, "ban cu"),
                "row_version": body["row_version"],
            },
        )
        assert stale.status_code == 409, stale.text

        assert (
            client.delete(
                f"/api/v1/bank/statements/profiles/{body['id']}", headers=profile_admin_headers
            ).status_code
            == 204
        )

    def test_a_duplicate_name_within_one_bank_is_refused_by_the_table(
        self,
        client: TestClient,
        profile_admin_headers: dict[str, str],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        bank_account: int,
    ) -> None:
        """Ràng buộc thật nằm ở BẢNG (unique `(bank_id, name)` từ 3C-2); tầng
        API chỉ dịch `IntegrityError` thành 409 đọc được — kiểm-trước-rồi-ghi
        thua một lượt ghi song song."""
        bank_id = self._bank_id(session_factory, dataset_alpha, context, bank_account)
        payload = _profile_payload(bank_id, "Trung ten 6G2")
        first = client.post(
            "/api/v1/bank/statements/profiles", headers=profile_admin_headers, json=payload
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/api/v1/bank/statements/profiles", headers=profile_admin_headers, json=payload
        )
        assert second.status_code == 409, second.text
        assert second.json()["error_code"] == "bank_statement_profile.conflict"
        assert (
            client.delete(
                f"/api/v1/bank/statements/profiles/{first.json()['id']}",
                headers=profile_admin_headers,
            ).status_code
            == 204
        )

    def test_a_profile_in_use_cannot_be_deleted(
        self,
        client: TestClient,
        clerk_headers: dict[str, str],
        profile_admin_headers: dict[str, str],
        bank_account: int,
        profile_id: int,
    ) -> None:
        """FK `RESTRICT` từ `bank_statements.profile_id`: xóa hồ sơ đang dùng
        sẽ bỏ lại sao kê không lần lại được cách nó đã đọc."""
        body = "Ngay GD;So CT;Dien giai;Ghi no;Ghi co\n18/01/2026;IU-1;dang dung;;80000\n"
        imported = _upload(
            client, clerk_headers, bank_account_id=bank_account, profile_id=profile_id, body=body
        )
        assert imported.status_code == 201, imported.text  # type: ignore[attr-defined]
        statement_id = imported.json()["statement"]["id"]  # type: ignore[attr-defined]

        refused = client.delete(
            f"/api/v1/bank/statements/profiles/{profile_id}", headers=profile_admin_headers
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error_code"] == "bank_statement_profile.conflict"

        assert (
            client.delete(f"/api/v1/bank/statements/{statement_id}", headers=clerk_headers)
        ).status_code == 204

    def test_statement_permission_alone_does_not_open_the_profile_doors(
        self,
        client: TestClient,
        clerk_headers: dict[str, str],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        bank_account: int,
        profile_id: int,
    ) -> None:
        """Quyền riêng: nhập sao kê hằng ngày ≠ đổi luật diễn giải mọi lượt
        nhập sau đó."""
        bank_id = self._bank_id(session_factory, dataset_alpha, context, bank_account)
        assert (
            client.post(
                "/api/v1/bank/statements/profiles",
                headers=clerk_headers,
                json=_profile_payload(bank_id, "khong duoc phep"),
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/api/v1/bank/statements/profiles/{profile_id}", headers=clerk_headers
            ).status_code
            == 403
        )


def test_the_statement_profile_permission_matrix_matches_the_doors(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    bank_account: int,
) -> None:
    """Review 6G-2 M-3: ma trận phân quyền không được hứa mã mà không cửa nào đọc.

    Bản đầu đăng ký cả bốn `CATALOG_ACTIONS` trong khi ba cửa ghi dùng CHUNG
    `edit` và `/all` dùng `statement_profile.view` — nên cấp `create` cho một
    vai trò vẫn ra 403, còn `edit` thì mở luôn cả tạo lẫn xóa. Người quản trị
    đọc màn phân quyền không suy ra được điều đó.
    """
    from ket.kernel.security.permissions import REGISTRY as PERMISSION_REGISTRY

    declared = {
        code for code in PERMISSION_REGISTRY.codes() if code.startswith("bank.statement_profile.")
    }
    assert declared == {"bank.statement_profile.view", "bank.statement_profile.edit"}, declared

    # Và mã `view` thật sự là cổng của màn khai — không phải `bank.statement.view`.
    viewer_role = ensure_role(
        session_factory,
        dataset_alpha,
        "chi_xem_ho_so_sao_ke",
        ["bank.statement_profile.view"],
    )
    headers = actor(
        client,
        session_factory,
        dataset_alpha,
        user_factory,
        viewer_role,
        "profile_viewer",
        test_password,
        branch_codes=[context.branch_code],
    )
    listed = client.get("/api/v1/bank/statements/profiles/all", headers=headers)
    assert listed.status_code == 200, listed.text
    # …nhưng không mở được cửa ghi.
    assert (
        client.post(
            "/api/v1/bank/statements/profiles",
            headers=headers,
            json=_profile_payload(1, "khong duoc phep 2"),
        ).status_code
        == 403
    )


def test_a_bad_bank_id_reports_itself_and_not_a_phantom_statement(
    client: TestClient,
    profile_admin_headers: dict[str, str],
) -> None:
    """Review 6G-2 M-1: `_flush_profile` từng nuốt MỌI `IntegrityError` thành
    "hồ sơ đang được sao kê sử dụng — xóa các sao kê đó trước".

    Khai hồ sơ với `bank_id` không tồn tại vi phạm khóa ngoại của CHÍNH bảng hồ
    sơ, và câu trả lời cũ khuyên người dùng đi xóa dữ liệu thật để chữa một lỗi
    không liên quan. Đây đúng là nợ L-2 của 6D mà `_flush_matches` đã sửa rồi
    lát này chép lại ở bảng bên cạnh.
    """
    response = client.post(
        "/api/v1/bank/statements/profiles",
        headers=profile_admin_headers,
        json=_profile_payload(987_654, "ngan hang khong ton tai"),
    )
    assert response.status_code != 409 or (
        response.json()["error_code"] != "bank_statement_profile.conflict"
    ), (
        "vi phạm FK `bank_id` KHÔNG được đội lốt 'hồ sơ đang được sao kê sử dụng' "
        f"— nhận: {response.text}"
    )
