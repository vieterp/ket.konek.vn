"""Tra hệ thống tài khoản qua HTTP (`/api/v1/accounts`) — lát 4E.

Đọc-only: hệ thống TK là dữ liệu của gói cấu hình (TT200/TT133), sửa đổi thuộc
màn hình quản trị gói (phase 5). Mọi truy vấn đi qua `resolve_package` theo
`(chế độ kế toán của năm, ngày)` — đúng quy tắc đọc của phase-05: không code
nào đoán gói hay tra `chart_of_accounts` bằng `code` cứng.

Endpoint này phục vụ ô tra TK trên form chứng từ: gõ tới đâu lọc tới đó, và
`detail_tracking` trong phản hồi cho form biết phải hiện cột chiều nào.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.accounts_schemas import AccountListResponse, AccountResponse
from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.accounts_provider import resolve_package
from ket.kernel.errors import DomainError
from ket.kernel.periods.models import FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import MASTER_MODULE, Action, permission_code

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])

ACCOUNT_VIEW: Final[str] = permission_code(MASTER_MODULE, "account", Action.VIEW)

AccountReader = Annotated[AuthorizedRequest, Depends(require_permission(ACCOUNT_VIEW))]

MAX_LIMIT: Final[int] = 200
MAX_IDS: Final[int] = 200


@router.get("", response_model=AccountListResponse)
def search_accounts(
    authorized: AccountReader,
    factory: SessionFactory,
    on_date: Annotated[date, Query()],
    search: Annotated[str | None, Query(max_length=100)] = None,
    ids: Annotated[list[int] | None, Query(max_length=MAX_IDS)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> AccountListResponse:
    """Tài khoản của gói hiệu lực tại `on_date` — client gửi ngày hạch toán.

    `on_date` bắt buộc thay vì mặc-định-hôm-nay: "hôm nay" của server và của
    client có thể khác múi giờ, mà gói cấu hình hiệu lực theo ngày — form đã
    biết ngày hạch toán nên cứ gửi nó lên.

    `search` khớp **tiền tố số hiệu** hoặc **chứa trong tên** (không phân biệt
    hoa thường; `%`/`_` do người dùng gõ được escape — họ tra mã, không tra
    mẫu) — hai cách kế toán viên tra TK: gõ "156" hoặc gõ "hàng hóa".
    TK ngừng dùng không trả về: form không được chọn thêm vào chứng từ mới.

    `ids` là đường **dựng lại** (hydrate) cho form sửa chứng từ: dòng cũ chỉ
    mang `account_id`, và hệ TK thật vượt trần `limit` nên không thể trông vào
    trang đầu. Khi có `ids`: bỏ `search`, và **gồm cả TK ngừng dùng** — chứng
    từ cũ trỏ vào TK nay đã ngừng vẫn phải hiện được mã/tên của nó.
    """
    lookup_date = on_date
    with unit_of_work(factory, authorized.scope) as session:
        # ORDER BY để hai năm chồng lấn (bất biến app canh — nếu vỡ) vẫn cho
        # kết quả xác định: lấy năm bắt đầu muộn nhất phủ ngày này.
        year = session.execute(
            select(FiscalYear)
            .where(FiscalYear.start_date <= lookup_date)
            .where(FiscalYear.end_date >= lookup_date)
            .order_by(FiscalYear.start_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if year is None:
            raise DomainError(
                "Chưa có năm tài chính phủ ngày này — tạo năm tài chính trước khi tra TK",
                on_date=lookup_date.isoformat(),
            )
        package = resolve_package(session, scheme=year.accounting_scheme, on_date=lookup_date)

        statement = (
            select(ChartOfAccount)
            .where(ChartOfAccount.package_id == package.id)
            .order_by(ChartOfAccount.code)
            .limit(limit)
        )
        if ids:
            statement = statement.where(ChartOfAccount.id.in_(ids))
        else:
            statement = statement.where(ChartOfAccount.is_inactive.is_(False))
            if search:
                term = search.strip()
                if term:
                    statement = statement.where(
                        or_(
                            ChartOfAccount.code.startswith(term, autoescape=True),
                            ChartOfAccount.name.icontains(term, autoescape=True),
                        )
                    )
        rows = session.execute(statement).scalars().all()
        return AccountListResponse(
            package_id=package.id,
            items=[AccountResponse.model_validate(row) for row in rows],
        )
