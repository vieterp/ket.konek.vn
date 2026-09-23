"""Endpoint phiếu kho và sổ kho (`/api/v1/inventory/*`) — SRS 09, lát 8A.

Router của module (RT-21): tạo/sửa/đọc thân phiếu NK/XK/CK, sắp xếp lại thứ tự
trong ngày (FR-STK-017) và tồn theo khóa (số lượng; giá trị khi 8B có engine).
Ghi sổ / bỏ ghi sổ / xóa dùng endpoint chứng từ dùng chung (`routers/vouchers.py`)
— ba loại đã đăng ký vào registry của posting nên bên đó tự biết kiểm quyền nào
và chạy hook dựng/gỡ sổ kho khi nào. Không có `/actions/post|unpost` riêng: hai
đường ghi sổ cho một loại chứng từ là hai chỗ để chúng lệch nhau (7B).

Ba loại phiếu, ba đường dẫn (`receipts`/`issues`/`transfers`) mang ba mã quyền
riêng — cùng lý do PT/PC tách quyền ở phiếu quỹ: người lập phiếu nhập không
nhất thiết được lập phiếu xuất. Cùng một `InventoryVoucherIn`, `kind` trên thân
phải khớp đường dẫn (422 nếu lệch — gửi phiếu xuất vào đường nhập là lỗi gõ
client, không phải một quyết định nghiệp vụ).
"""

# KHÔNG `from __future__ import annotations`: ba bộ endpoint dựng trong hàm
# với `Annotated[..., Depends(require_permission(<biến cục bộ>))]` — chú thích
# dạng chuỗi thì FastAPI/pydantic không giải được tên cục bộ lúc sinh OpenAPI.
from datetime import date, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.kernel.config.catalog import SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import BranchNotInScopeError, PostingValidationError, PostingViolation
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.inventory import (
    ASSEMBLY_PERMISSION_CODE,
    INVENTORY_PERMISSION_MODULE,
    ISSUE_PERMISSION_CODE,
    RECEIPT_PERMISSION_CODE,
    TRANSFER_PERMISSION_CODE,
)
from ket.modules.inventory.availability import availability
from ket.modules.inventory.costing import COSTING_VIEW
from ket.modules.inventory.costing.affected import preview as costing_preview
from ket.modules.inventory.costing.uncosted import uncosted_vouchers
from ket.modules.inventory.models import (
    InventoryVoucher,
    InventoryVoucherKind,
    InventoryVoucherLine,
)
from ket.modules.inventory.movements import reorder_day
from ket.modules.inventory.schemas import (
    AvailabilityResponse,
    CostingAffectedPreview,
    InventoryVoucherIn,
    InventoryVoucherLineOut,
    InventoryVoucherOut,
    InventoryVoucherUpdate,
    ReorderDayIn,
    ReorderDayOut,
    StockResponse,
    UncostedVouchersResponse,
)
from ket.modules.inventory.service import InventoryVoucherService
from ket.modules.inventory.stock import stock_rows
from ket.posting.documents.models import Voucher

router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])

KIND_MISMATCH_CODE: Final[str] = "inventory.kind_path_mismatch"

PERMISSION_BY_KIND: Final[dict[int, str]] = {
    InventoryVoucherKind.RECEIPT: RECEIPT_PERMISSION_CODE,
    InventoryVoucherKind.ISSUE: ISSUE_PERMISSION_CODE,
    InventoryVoucherKind.TRANSFER: TRANSFER_PERMISSION_CODE,
    InventoryVoucherKind.ASSEMBLY: ASSEMBLY_PERMISSION_CODE,
    InventoryVoucherKind.DISASSEMBLY: ASSEMBLY_PERMISSION_CODE,
}


def _permission(code: str, action: Action) -> str:
    return permission_code(INVENTORY_PERMISSION_MODULE, code, action)


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _to_response(
    voucher: Voucher, body: InventoryVoucher, lines: list[InventoryVoucherLine]
) -> InventoryVoucherOut:
    base = InventoryVoucherOut.model_validate(voucher)
    return base.model_copy(
        update={
            "kind": body.kind,
            "operation_code": body.operation_code,
            "warehouse_id": body.warehouse_id,
            "to_warehouse_id": body.to_warehouse_id,
            "partner_id": body.partner_id,
            "partner_kind": body.partner_kind,
            "delivered_by": body.delivered_by,
            "keeper_status": body.keeper_status,
            "lines": tuple(InventoryVoucherLineOut.model_validate(line) for line in lines),
        }
    )


def _response_of(service: InventoryVoucherService, voucher_id: UUID) -> InventoryVoucherOut:
    return _to_response(*service.get(voucher_id))


def _require_kind(payload: InventoryVoucherIn, kind: int, path: str) -> None:
    if payload.kind != kind:
        raise PostingValidationError(
            "Loại phiếu trên thân không khớp đường dẫn",
            violations=[
                PostingViolation(
                    KIND_MISMATCH_CODE,
                    f"Đường dẫn `{path}` chỉ nhận phiếu loại {kind}",
                    requested_kind=payload.kind,
                    expected_kind=kind,
                )
            ],
        )


def _register_voucher_routes(*, path: str, kind: int, permission_name: str, title: str) -> None:
    """Một bộ POST/GET/PUT cho mỗi loại phiếu — ba bộ giống nhau chỉ khác
    quyền và `kind`, nên dựng bằng vòng lặp thay vì chép ba lần."""
    view = _permission(permission_name, Action.VIEW)
    create = _permission(permission_name, Action.CREATE)
    edit = _permission(permission_name, Action.EDIT)
    post = _permission(permission_name, Action.POST)
    create_route = f"POST /api/v1/inventory/{path}"
    create_key_dependency = idempotency_key_dependency(create_route)

    def create_voucher(
        payload: InventoryVoucherIn,
        authorized: Annotated[AuthorizedRequest, Depends(require_permission(create))],
        factory: SessionFactory,
        settings: AppSettings,
        idempotency_key: Annotated[str, Depends(create_key_dependency)],
        response: Response,
        acknowledge_warnings: Annotated[bool, Query()] = False,
    ) -> InventoryVoucherOut:
        _require_kind(payload, kind, path)
        _require_branch_in_scope(authorized, payload.branch_id)

        def work(session: Session) -> tuple[InventoryVoucherOut, IdempotentRef]:
            service = InventoryVoucherService(session)
            if value_of(session, key=SAVE_ALSO_POSTS_KEY, user_id=authorized.scope.user_id) is True:
                authorized.access.require(post)
            voucher = service.create(
                payload,
                user_id=authorized.scope.user_id,
                acknowledged_warnings=acknowledge_warnings,
            )
            return _response_of(service, voucher.id), IdempotentRef(
                result_type=Voucher.__tablename__, result_id=str(voucher.id)
            )

        def replay(session: Session, ref: IdempotentRef) -> InventoryVoucherOut:
            return _response_of(InventoryVoucherService(session), UUID(ref.result_id))

        created_body, created = execute_once(
            factory,
            authorized.scope,
            route_key=create_route,
            key=idempotency_key,
            fingerprint=fingerprint_of(payload.model_dump_json()),
            work=work,
            replay=replay,
            ttl=timedelta(hours=settings.idempotency_ttl_hours),
        )
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return created_body

    def get_voucher(
        voucher_id: UUID,
        authorized: Annotated[AuthorizedRequest, Depends(require_permission(view))],
        factory: SessionFactory,
    ) -> InventoryVoucherOut:
        with unit_of_work(factory, authorized.scope) as session:
            header, body, lines = InventoryVoucherService(session).get(voucher_id)
            # Quyền xem theo LOẠI phiếu đã lưu, không theo đường dẫn: người chỉ
            # có `receipt.view` gọi `/receipts/{id}` với id một phiếu xuất phải
            # nhận 403 (cùng luật `cash_book.get_cash_voucher`, review 8A M-2).
            authorized.access.require(_permission(PERMISSION_BY_KIND[body.kind], Action.VIEW))
            return _to_response(header, body, lines)

    def update_voucher(
        voucher_id: UUID,
        payload: InventoryVoucherUpdate,
        authorized: Annotated[AuthorizedRequest, Depends(require_permission(edit))],
        factory: SessionFactory,
    ) -> InventoryVoucherOut:
        """Sửa phiếu Đã cất — khóa lạc quan bằng `row_version` (FR-NFR-005)."""
        _require_kind(payload, kind, path)
        _require_branch_in_scope(authorized, payload.branch_id)
        with unit_of_work(factory, authorized.scope) as session:
            service = InventoryVoucherService(session)
            voucher = service.update(
                voucher_id,
                payload,
                expected_row_version=payload.row_version,
                user_id=authorized.scope.user_id,
            )
            return _response_of(service, voucher.id)

    router.add_api_route(
        f"/{path}",
        create_voucher,
        methods=["POST"],
        response_model=InventoryVoucherOut,
        status_code=status.HTTP_201_CREATED,
        name=f"create_inventory_{path}",
        summary=f"Cất {title.lower()}",
    )
    router.add_api_route(
        f"/{path}/{{voucher_id}}",
        get_voucher,
        methods=["GET"],
        response_model=InventoryVoucherOut,
        name=f"get_inventory_{path}",
        summary=f"Đọc {title.lower()}",
    )
    router.add_api_route(
        f"/{path}/{{voucher_id}}",
        update_voucher,
        methods=["PUT"],
        response_model=InventoryVoucherOut,
        name=f"update_inventory_{path}",
        summary=f"Sửa {title.lower()}",
    )


for _path, _kind, _permission_name, _title in (
    ("receipts", InventoryVoucherKind.RECEIPT, RECEIPT_PERMISSION_CODE, "Phiếu nhập kho"),
    ("issues", InventoryVoucherKind.ISSUE, ISSUE_PERMISSION_CODE, "Phiếu xuất kho"),
    ("transfers", InventoryVoucherKind.TRANSFER, TRANSFER_PERMISSION_CODE, "Phiếu chuyển kho"),
    ("assemblies", InventoryVoucherKind.ASSEMBLY, ASSEMBLY_PERMISSION_CODE, "Phiếu lắp ráp"),
    ("disassemblies", InventoryVoucherKind.DISASSEMBLY, ASSEMBLY_PERMISSION_CODE, "Phiếu tháo dỡ"),
):
    _register_voucher_routes(path=_path, kind=_kind, permission_name=_permission_name, title=_title)


REORDER_ROUTE: Final[str] = "POST /api/v1/inventory/movements/actions/reorder-day"
ReorderKey = Annotated[str, Depends(idempotency_key_dependency(REORDER_ROUTE))]
ReorderActor = Annotated[
    AuthorizedRequest,
    Depends(require_permission(_permission(ISSUE_PERMISSION_CODE, Action.POST))),
]
"""Đổi thứ tự trong ngày đổi giá xuất (BR-STK-04) — quyền ghi sổ phiếu xuất."""

StockReader = Annotated[
    AuthorizedRequest,
    Depends(require_permission(_permission(RECEIPT_PERMISSION_CODE, Action.VIEW))),
]
IssueReader = Annotated[
    AuthorizedRequest,
    Depends(require_permission(_permission(ISSUE_PERMISSION_CODE, Action.VIEW))),
]


@router.post("/movements/actions/reorder-day", response_model=ReorderDayOut)
def reorder_movements_in_day(
    payload: ReorderDayIn,
    authorized: ReorderActor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: ReorderKey,
) -> ReorderDayOut:
    """FR-STK-017: sắp xếp lại thứ tự chứng từ của một khóa tồn kho trong ngày."""
    _require_branch_in_scope(authorized, payload.branch_id)

    def work(session: Session) -> tuple[ReorderDayOut, IdempotentRef]:
        changed, marked_from = reorder_day(session, payload)
        return ReorderDayOut(reordered=changed, marked_from=marked_from), IdempotentRef(
            result_type="inventory_reorder", result_id=payload.posting_date.isoformat()
        )

    def replay(session: Session, ref: IdempotentRef) -> ReorderDayOut:
        return ReorderDayOut(reordered=0, marked_from=date.fromisoformat(ref.result_id))

    result, _created = execute_once(
        factory,
        authorized.scope,
        route_key=REORDER_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    return result


@router.get("/stock", response_model=StockResponse)
def read_stock(
    authorized: StockReader,
    factory: SessionFactory,
    as_of: Annotated[date, Query()],
    branch_id: Annotated[int | None, Query()] = None,
    warehouse_id: Annotated[int | None, Query()] = None,
    item_id: Annotated[int | None, Query()] = None,
) -> StockResponse:
    """Tồn theo `(chi nhánh, kho, vật tư, lô)` tại cuối ngày — số lượng đơn vị
    chính; giá trị `null` cho tới khi khóa đã tính giá đủ (8B). Chi nhánh ngoài
    phạm vi → 422; không truyền thì RLS trả mọi chi nhánh trong phạm vi."""
    if branch_id is not None:
        _require_branch_in_scope(authorized, branch_id)
    with unit_of_work(factory, authorized.scope) as session:
        rows = stock_rows(
            session, as_of=as_of, branch_id=branch_id, warehouse_id=warehouse_id, item_id=item_id
        )
    return StockResponse(as_of=as_of, items=tuple(rows))


@router.get("/availability", response_model=AvailabilityResponse)
def read_availability(
    authorized: IssueReader,
    factory: SessionFactory,
    as_of: Annotated[date, Query()],
    warehouse_id: Annotated[int | None, Query()] = None,
    item_ids: Annotated[tuple[int, ...], Query()] = (),
) -> AvailabilityResponse:
    """Cột **"Có thể bán"** (U7) = tồn − đã hứa giao, cho chi nhánh **đang thao
    tác**: cam kết là số của một chi nhánh, và cộng cam kết của nhiều chi nhánh
    vào tồn của một kho là một con số không ai dùng được.

    Quyền `inventory.issue.view` — người sắp xuất hàng là người hỏi câu này.
    """
    branch_id = authorized.scope.acting_branch_id
    if branch_id is None:
        raise BranchNotInScopeError("Cột Có thể bán cần một chi nhánh đang thao tác", branch=None)
    with unit_of_work(factory, authorized.scope) as session:
        return availability(
            session,
            as_of=as_of,
            branch_id=branch_id,
            warehouse_id=warehouse_id,
            item_ids=tuple(item_ids),
        )


CostingViewer = Annotated[AuthorizedRequest, Depends(require_permission(COSTING_VIEW))]


@router.get("/costing/affected", response_model=CostingAffectedPreview)
def read_costing_affected(
    authorized: CostingViewer,
    factory: SessionFactory,
    from_date: Annotated[date | None, Query()] = None,
) -> CostingAffectedPreview:
    """FR-STK-003: xem trước chứng từ bị tính lại giá xuất kho — và kỳ đã khóa
    bị chạm (RT-11) — cho chi nhánh **đang thao tác**, đúng phạm vi mà job
    `inventory.costing.recalc` sẽ chạy (xem `routers/jobs.py`). `from_date` =
    xem trước lượt ép tính lại từ ngày đó."""
    branch_id = authorized.scope.acting_branch_id
    if branch_id is None:
        raise BranchNotInScopeError(
            "Xem trước tính giá xuất kho cần một chi nhánh đang thao tác", branch=None
        )
    with unit_of_work(factory, authorized.scope) as session:
        return costing_preview(session, branch_id=branch_id, force_from=from_date)


@router.get("/costing/uncosted", response_model=UncostedVouchersResponse)
def read_costing_uncosted(
    authorized: CostingViewer,
    factory: SessionFactory,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> UncostedVouchersResponse:
    """FR-STK-008: chứng từ kho còn dòng chưa tính giá của chi nhánh **đang
    thao tác** — sau một lượt job xanh danh sách phải rỗng (trừ khóa chưa có
    lần nhập có giá)."""
    branch_id = authorized.scope.acting_branch_id
    if branch_id is None:
        raise BranchNotInScopeError(
            "Danh sách chứng từ chưa tính giá cần một chi nhánh đang thao tác", branch=None
        )
    with unit_of_work(factory, authorized.scope) as session:
        return uncosted_vouchers(session, branch_id=branch_id, date_from=date_from, date_to=date_to)
