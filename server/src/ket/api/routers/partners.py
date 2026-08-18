"""Tài khoản ngân hàng của đối tác — bảng con của danh mục `partners` (FR-SYS-033).

Tách khỏi bộ sinh route ở `routers/master_data.py` vì hình dạng khác hẳn: đây là
danh sách **thuộc về** một bản ghi danh mục, không phải một danh mục. Nới bộ sinh
để nó biết thêm khái niệm "bảng con" sẽ bắt mười bảy danh mục còn lại mang theo
một khả năng chúng không có — đúng lối "bẻ cong khung dùng chung" mà §Risk của
phase 3 cảnh báo.

Quyền dùng lại **chính mã quyền của danh mục đối tác** (`master.partners.*`):
tài khoản ngân hàng là một phần của hồ sơ đối tác, và một mã quyền riêng sẽ tạo
ra tình huống "sửa được đối tác nhưng không sửa được tài khoản của nó" mà không
màn hình nào diễn đạt được.

Phạm vi chi nhánh đi qua **đối tác chủ**: mọi đường dưới đây mở đầu bằng việc
đọc đối tác và kiểm nó nhìn thấy được (`ensure_visible`). Tài khoản không mang
`branch_id` riêng vì nó không có ý nghĩa riêng nào ngoài đối tác của nó.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.master_data_guards import ensure_catalog_choice, ensure_visible
from ket.api.routers.partners_schemas import (
    PartnerBankAccountCreateRequest,
    PartnerBankAccountListResponse,
    PartnerBankAccountResponse,
    PartnerBankAccountUpdateRequest,
)
from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.bank_account_service import PartnerBankAccountService
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action

PARTNER_SLUG: Final[str] = "partners"
BANK_SLUG: Final[str] = "banks"

PREFIX: Final[str] = f"/api/v1/master/{PARTNER_SLUG}/{{partner_id}}/bank-accounts"

ADD_ROUTE: Final[str] = f"POST /api/v1/master/{PARTNER_SLUG}/{{partner_id}}/bank-accounts"


def _partner_spec() -> CatalogSpec:
    """Mô tả danh mục đối tác, lấy từ registry.

    Đọc registry chứ không gõ lại mã quyền: nếu một ngày `slug` của đối tác đổi
    thì tệp này phải đỏ ngay lúc nạp, không phải trả `403` cho người dùng thật.
    """
    spec = CATALOG_REGISTRY.get(PARTNER_SLUG)
    if spec is None:  # pragma: no cover - registry nạp lúc import, thiếu là lỗi khởi động
        raise RuntimeError(f"Danh mục {PARTNER_SLUG!r} chưa được đăng ký")
    return spec


SPEC: Final[CatalogSpec] = _partner_spec()

router = APIRouter(prefix=PREFIX, tags=["master-data"])

PartnerReader = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.VIEW)))
]
PartnerAuthor = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.CREATE)))
]
PartnerEditor = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.EDIT)))
]
PartnerRemover = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.DELETE)))
]

AddKey = Annotated[str, Depends(idempotency_key_dependency(ADD_ROUTE))]


def _load_partner(session: Session, partner_id: int, authorized: AuthorizedRequest) -> None:
    """Đối tác chủ phải tồn tại **và** nhìn thấy được từ chi nhánh đang thao tác."""
    service: MasterDataService[MasterDataRow] = MasterDataService(session, SPEC.model)
    partner = service.get(partner_id)
    ensure_visible(partner, authorized.scope.acting_branch_id, SPEC)
    if partner.is_group:
        raise MasterDataNotFoundError(
            "Nhóm đối tác không có tài khoản ngân hàng — hãy chọn một đối tác cụ thể",
            entity_type=SPEC.entity_type,
            entity_id=partner_id,
        )


@router.get("", response_model=PartnerBankAccountListResponse)
def list_bank_accounts(
    partner_id: int,
    authorized: PartnerReader,
    factory: SessionFactory,
    include_inactive: Annotated[bool, Query()] = False,
) -> PartnerBankAccountListResponse:
    """Tài khoản của một đối tác — mặc định trước.

    **Không** phân trang, khác đường đọc danh mục: một đối tác có vài tài khoản,
    không phải vài nghìn. Thêm phân trang ở đây là thêm hai tham số mà mọi màn
    hình sẽ truyền giá trị mặc định.
    """
    with unit_of_work(factory, authorized.scope) as session:
        _load_partner(session, partner_id, authorized)
        accounts = PartnerBankAccountService(session).list_for(
            partner_id, include_inactive=include_inactive
        )
        return PartnerBankAccountListResponse(
            items=[PartnerBankAccountResponse.model_validate(item) for item in accounts]
        )


@router.post("", response_model=PartnerBankAccountResponse, status_code=status.HTTP_201_CREATED)
def add_bank_account(
    partner_id: int,
    payload: PartnerBankAccountCreateRequest,
    authorized: PartnerAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddKey,
    response: Response,
) -> PartnerBankAccountResponse:
    """Thêm một tài khoản ngân hàng cho đối tác — thực hiện đúng một lần (FR-NFR-004)."""
    body = payload

    def work(session: Session) -> tuple[PartnerBankAccountResponse, IdempotentRef]:
        _load_partner(session, partner_id, authorized)
        ensure_catalog_choice(session, BANK_SLUG, body.bank_id, authorized)
        account = PartnerBankAccountService(session).add(
            partner_id=partner_id,
            bank_id=body.bank_id,
            account_number=body.account_number,
            account_holder=body.account_holder,
            bank_branch=body.bank_branch,
            is_default=body.is_default,
        )
        return PartnerBankAccountResponse.model_validate(account), IdempotentRef(
            result_type=PartnerBankAccountService(session).entity_type,
            result_id=str(account.id),
        )

    def replay(session: Session, ref: IdempotentRef) -> PartnerBankAccountResponse:
        _load_partner(session, partner_id, authorized)
        account = PartnerBankAccountService(session).get(int(ref.result_id), partner_id=partner_id)
        return PartnerBankAccountResponse.model_validate(account)

    created_account, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_ROUTE,
        key=idempotency_key,
        # `partner_id` vào vân tay — xem lập luận M-6 ở `items_units.py`: cùng
        # khóa + cùng thân request gửi lên **đối tác khác** vốn bị coi là lượt
        # phát lại và trả `404` thay vì tạo dòng.
        fingerprint=fingerprint_of(f"{partner_id}:{body.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return created_account


@router.put("/{account_id}", response_model=PartnerBankAccountResponse)
def update_bank_account(
    partner_id: int,
    account_id: int,
    payload: PartnerBankAccountUpdateRequest,
    authorized: PartnerEditor,
    factory: SessionFactory,
) -> PartnerBankAccountResponse:
    """Sửa một tài khoản, gồm cả cờ mặc định và cờ còn dùng."""
    with unit_of_work(factory, authorized.scope) as session:
        _load_partner(session, partner_id, authorized)
        ensure_catalog_choice(session, BANK_SLUG, payload.bank_id, authorized)
        account = PartnerBankAccountService(session).update(
            account_id,
            partner_id=partner_id,
            expected_row_version=payload.row_version,
            bank_id=payload.bank_id,
            account_number=payload.account_number,
            account_holder=payload.account_holder,
            bank_branch=payload.bank_branch,
            is_default=payload.is_default,
            is_active=payload.is_active,
        )
        return PartnerBankAccountResponse.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bank_account(
    partner_id: int,
    account_id: int,
    authorized: PartnerRemover,
    factory: SessionFactory,
) -> None:
    """Xóa một tài khoản khỏi hồ sơ đối tác."""
    with unit_of_work(factory, authorized.scope) as session:
        _load_partner(session, partner_id, authorized)
        PartnerBankAccountService(session).delete(account_id, partner_id=partner_id)
