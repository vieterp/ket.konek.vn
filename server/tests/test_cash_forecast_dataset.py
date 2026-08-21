"""Dataset `cash_forecast` — Dự báo dòng tiền (FR-QUY-032, bước 11 phase-06).

Dự báo là MỘT báo cáo metadata (`du-bao-dong-tien`), không màn hình riêng:
test đi qua chính đường preview của report engine để chứng minh không dòng
code renderer nào là của riêng nó. Nguồn v1 = chi tiết chứng từ số dư ban đầu;
số còn nợ phải co lại theo `paid_amount*` khi phiếu thu đối trừ đã ghi sổ.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_open_invoice
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.main import create_app
from ket.posting.opening_balances.models import OpeningDetailKind
from ket.settings import Settings
from posting_support import PostingContext, seed_posting_context

pytestmark = pytest.mark.db

VIEWER_ROLE = "xem_du_bao"


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
def viewer_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    return ensure_role(session_factory, dataset_alpha, VIEWER_ROLE, ["reporting.report.view"])


def _cell_values(row: dict[str, object]) -> list[object]:
    cells = row.get("cells") or []
    return [cell["text"] for cell in cells if isinstance(cell, dict)]


def test_forecast_lists_open_receivables_and_payables_with_buckets(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    viewer_role: str,
    test_password: str,
    context: PostingContext,
) -> None:
    seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=9601,
        amount_fc=Decimal("700000"),
        invoice_no="HD-DUBAO-THU",
    )
    seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=9602,
        amount_fc=Decimal("450000"),
        invoice_no="HD-DUBAO-CHI",
        due_date=None,
    )
    headers = {
        **actor(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            viewer_role,
            "du_bao",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    listed = client.get("/api/v1/reports", headers=headers)
    assert listed.status_code == 200
    assert any(item["code"] == "du-bao-dong-tien" for item in listed.json()["reports"])

    previewed = client.post(
        "/api/v1/reports/du-bao-dong-tien/preview",
        json={"params": {"from_date": "2026-01-01", "to_date": "2026-01-31"}},
        headers=headers,
    )
    assert previewed.status_code == 200, previewed.text
    body = previewed.json()
    data_rows = [
        _cell_values(row) for row in body["rows"] if row["kind"] == "data" and row.get("cells")
    ]
    flattened = ["|".join(str(value) for value in row) for row in data_rows]
    thu_rows = [row for row in flattened if "HD-DUBAO-THU" in row]
    chi_rows = [row for row in flattened if "HD-DUBAO-CHI" in row]
    assert thu_rows and "thu" in thu_rows[0]
    # Hạn 2026-02-20 so với mốc 31/01 → rơi vào ô 0–30 ngày.
    assert "0-30" in thu_rows[0]
    assert chi_rows and "chi" in chi_rows[0]
    # Không ghi hạn thanh toán → ô riêng, không lẫn vào "quá hạn".
    assert "khong-han" in chi_rows[0]
