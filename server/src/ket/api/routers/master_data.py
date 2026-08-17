"""Endpoint danh mục — **một bộ route cho mỗi danh mục, sinh từ registry** (H47).

Tiêu chí thành công của phase 3: *"thêm một loại danh mục mới chỉ cần thêm model
+ descriptor, không thêm router"*. Tệp này là chỗ tiêu chí đó được giữ — nó
không nhắc tên một danh mục nào, và số dòng của nó không đổi khi lát 3B-2 thêm
đối tác hay phase 5 thêm hệ thống tài khoản.

Sáu thao tác cho mỗi danh mục:

| Đường dẫn | Việc |
| --- | --- |
| `GET    /api/v1/master/{slug}` | Danh sách: con của một nút, hoặc cả nhánh |
| `GET    /api/v1/master/{slug}/{id}` | Một bản ghi |
| `POST   /api/v1/master/{slug}` | Tạo mới — thực hiện đúng một lần (FR-NFR-004) |
| `PUT    /api/v1/master/{slug}/{id}` | Sửa mô tả + cột riêng + "Ngừng theo dõi" |
| `PUT    /api/v1/master/{slug}/{id}/parent` | Chuyển nhánh (FR-SYS-011) |
| `DELETE /api/v1/master/{slug}/{id}` | Xóa thật — chỉ khi chưa ai dùng (BR-SYS-02) |

**Phạm vi chi nhánh không phải tham số của client**: mọi đường đọc lọc theo
`authorized.scope.acting_branch_id`, và bảng danh mục cố ý không bật RLS (H39)
nên đây là lớp lọc duy nhất. Nhận `branch_id` từ query string sẽ cho bất kỳ ai
đọc danh mục riêng của chi nhánh khác chỉ bằng cách sửa URL.

**Tệp này cố ý không có `from __future__ import annotations`** — khác mọi tệp
khác của repo. Lý do là cơ chế của FastAPI: nó suy kiểu thân request từ
annotation của tham số, mà lớp Pydantic ở đây được **dựng lúc chạy** cho từng
danh mục. Với annotation hoãn (chuỗi), FastAPI sẽ đi tìm tên `schemas` trong
`globals()` của module và không thấy — nó là biến cục bộ của `_mount`. Không có
dòng import đó, annotation được tính ngay lúc định nghĩa hàm và FastAPI nhận
đúng lớp.

Hai endpoint có **thân request** vì thế không gắn bằng decorator: chữ ký của
chúng khai kiểu **tĩnh** (bộ trường chung) để mypy kiểm được trọn thân hàm, rồi
`_register_with_body` thay annotation bằng lớp của đúng danh mục ngay trước khi
gắn route. Nhờ vậy không tệp nào trong `src/` cần một dòng miễn trừ kiểm kiểu —
ADR-015 đặt ngưỡng cho chúng ở **0** và có cổng CI đếm.

Phần cột riêng thì có cổng khác canh — `tests/test_master_data_registry.py` đối
chiếu `extra_fields` với cột đã ánh xạ theo cả hai chiều.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.master_data_schemas import (
    CatalogSchemas,
    MasterDataBaseCreateRequest,
    MasterDataBaseUpdateRequest,
    MasterDataMergeRequest,
    MasterDataMergeResponse,
    MasterDataMoveRequest,
    MovedReferenceResponse,
    build_schemas,
    extra_values,
)
from ket.kernel.errors import (
    BranchNotInScopeError,
    MasterDataFilterUnknownError,
    MasterDataGroupNotPostableError,
    MasterDataNotFoundError,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.merge_service import merge_records
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action

router = APIRouter(prefix="/api/v1/master", tags=["master-data"])

PREFIX: Final[str] = "/api/v1/master"

MAX_PAGE_SIZE: Final[int] = 200
"""Bằng trần của `routers/system.py` — một con số cho cả API, không mỗi nhóm một trần."""

DEFAULT_PAGE_SIZE: Final[int] = 100
"""Đủ cho một màn hình cây mở hết một cấp; client xin thêm khi cần."""


def ensure_visible(record: MasterDataRow, branch_id: int | None, spec: CatalogSpec) -> None:
    """Bản ghi riêng của chi nhánh khác thì coi như không tồn tại.

    `404` chứ không `403`, có chủ đích: `403` là lời xác nhận "có một bản ghi id
    47 ở chi nhánh khác", và một vòng lặp qua id sẽ vẽ lại được danh mục của chi
    nhánh bên cạnh. Cùng lập luận đã áp cho `AttachmentNotFoundError`.

    Kiểm ở tầng HTTP chứ không trong `MasterDataService.get`: đường đọc theo
    **khóa chính** có những người gọi hợp lệ ở tầng trong (gộp bản ghi, nhập
    liệu, posting engine) vốn phải nhìn thấy mọi chi nhánh. Ranh giới chi nhánh
    là ranh giới của một **request**.
    """
    if record.branch_id is None or record.branch_id == branch_id:
        return
    raise MasterDataNotFoundError(
        "Không tìm thấy bản ghi danh mục",
        entity_type=spec.entity_type,
        entity_id=record.id,
    )


def _ensure_branch_in_scope(branch_id: int | None, authorized: AuthorizedRequest) -> None:
    """Chỉ tạo được danh mục riêng cho chi nhánh **mình được gán**.

    `branch_id` đến từ thân request, nên không kiểm thì bất kỳ ai có quyền tạo
    danh mục cũng cắm được bản ghi vào ngăn của chi nhánh khác — và vì chính họ
    không nhìn thấy nó sau đó (`ensure_visible`), bản ghi ấy trở thành thứ chỉ
    chi nhánh kia thấy mà không ai bên đó tạo ra.

    `None` luôn hợp lệ: đó là danh mục **dùng chung toàn công ty** (FR-SYS-018),
    và quyền tạo nó là chính mã quyền `create` của danh mục đó.

    `403` chứ không `404` như đường đọc: ở đây người gọi tự nêu ra một chi nhánh
    thay vì dò một khóa chính, nên câu trả lời "ngoài phạm vi của bạn" không tiết
    lộ gì mà họ chưa gõ vào.

    So với **chi nhánh đang thao tác**, không so với cả phạm vi được gán (sửa sau
    review L-1). Người được gán cả A lẫn B, đang thao tác ở A, mà tạo được bản
    ghi mang `branch_id = B` thì sẽ mất dấu nó ngay lập tức — mọi đường đọc lọc
    theo `acting_branch_id`, nên bản ghi vừa tạo biến mất khỏi chính màn hình vừa
    tạo ra nó. Phạm vi ghi và phạm vi đọc phải là **một**.
    """
    acting = authorized.scope.acting_branch_id
    if branch_id is None or branch_id == acting:
        return
    raise BranchNotInScopeError(
        "Chỉ tạo được danh mục riêng cho chi nhánh đang thao tác",
        branch=branch_id,
        acting_branch=acting,
    )


def _ensure_parent_visible(
    service: MasterDataService[MasterDataRow],
    parent_id: int | None,
    authorized: AuthorizedRequest,
    spec: CatalogSpec,
) -> None:
    """Nhóm cha phải **nhìn thấy được** từ chi nhánh đang thao tác (sửa sau review H-4).

    Không có bước này thì lỗi phạm vi của tầng dịch vụ trả lời hộ hai câu hỏi mà
    người gọi chưa được phép hỏi: `422` nghĩa là "id này có thật, ở chi nhánh
    khác" còn `404` nghĩa là "không có id này" — đủ để dò ra danh mục của chi
    nhánh bên cạnh gồm những id nào. Nay cả hai đều là `404`.

    `MasterDataParentScopeError` của tầng dịch vụ **vẫn giữ** và vẫn có ích: nó
    bắt trường hợp cha nhìn thấy được nhưng phạm vi vẫn sai (cha riêng chi nhánh
    A, con khai dùng chung). Ở đó `parent_branch_id` trong `details` không lộ gì
    — người gọi nhìn thấy chính nhóm cha ấy.
    """
    if parent_id is None:
        return
    ensure_visible(service.get(parent_id), authorized.scope.acting_branch_id, spec)


def _flag_column(spec: CatalogSpec, value: str | None) -> str | None:
    """`?flag=customer` → tên cột boolean phải `TRUE`, hoặc `None` khi không lọc.

    Một tham số **chung** cho mọi danh mục thay vì mỗi danh mục một tham số
    riêng: chữ ký của endpoint sinh ra phải là kiểu tĩnh để mypy kiểm được thân
    hàm (xem `_register_with_body`), và một tham số truy vấn khai theo từng danh
    mục lúc chạy sẽ tái lập đúng vấn đề annotation mà tệp này đã giải một lần.
    Danh sách giá trị hợp lệ của từng danh mục nằm trong mô tả OpenAPI.
    """
    if value is None:
        return None
    flag = spec.flag(value)
    if flag is None:
        allowed = ", ".join(item.value for item in spec.flags) or "(danh mục này không có bộ lọc)"
        raise MasterDataFilterUnknownError(
            "Bộ lọc không hợp lệ cho danh mục này",
            entity_type=spec.entity_type,
            flag=value,
            allowed=allowed,
        )
    return flag.column


def _ensure_references_visible(
    session: Session,
    spec: CatalogSpec,
    payload: BaseModel,
    authorized: AuthorizedRequest,
) -> None:
    """Mọi khóa ngoại trỏ sang danh mục khác phải **nhìn thấy được** (`CatalogReference`).

    Khóa ngoại của DB chỉ trả lời "id có tồn tại", và đó là câu trả lời sai ở hai
    mặt: bản ghi riêng của chi nhánh khác vẫn tồn tại (nên gắn được, rồi biến mất
    khỏi màn hình của chính người vừa gắn), và một nút **nhóm** cũng tồn tại (nên
    gắn được, dù nhóm chỉ để gom cây chứ không mang giá trị nào).

    `404` cho bản ghi ngoài phạm vi, cùng mã với "không có id này" — cùng lập
    luận H55 đã áp cho nhóm cha: hai mã khác nhau là một oracle liệt kê id.
    """
    for reference in spec.references:
        value = getattr(payload, reference.field, None)
        if value is not None:
            ensure_catalog_choice(session, reference.slug, int(value), authorized)


def ensure_catalog_choice(
    session: Session, slug: str, record_id: int, authorized: AuthorizedRequest
) -> None:
    """Một giá trị người dùng chọn từ danh mục `slug` phải dùng được thật.

    Dùng chung cho hai đường: cột khóa ngoại khai trong `CatalogSpec.references`
    (đối tác → điều khoản thanh toán) và các bảng con viết tay
    (`routers/partners.py` → ngân hàng của tài khoản). Một hàm cho cả hai vì câu
    hỏi giống hệt nhau, và hai bản sao là hai chỗ để phép kiểm chi nhánh trôi
    lệch.
    """
    target_spec = CATALOG_REGISTRY.get(slug)
    if target_spec is None:  # pragma: no cover - test registry canh trước khi chạy tới đây
        raise MasterDataNotFoundError(
            "Danh mục đích của tham chiếu không tồn tại", entity_type=slug
        )
    service: MasterDataService[MasterDataRow] = MasterDataService(session, target_spec.model)
    record = service.get(record_id)
    ensure_visible(record, authorized.scope.acting_branch_id, target_spec)
    if record.is_group:
        # `MasterDataGroupNotPostableError` chứ không `404` như nhánh trên:
        # tới đây bản ghi đã nhìn thấy được, nên nói thẳng "đây là nhóm"
        # không lộ gì mà người gọi chưa biết — và đó là câu duy nhất giúp họ
        # sửa. Bản ghi **đã ngừng theo dõi** thì vẫn cho gắn: chặn nó sẽ làm
        # mọi lần sửa tên một đối tác cũ bị từ chối vì một điều khoản thanh
        # toán mà chính lần sửa đó không đụng tới.
        raise MasterDataGroupNotPostableError(
            "Giá trị được chọn là một nhóm — hãy chọn một mục con",
            entity_type=target_spec.entity_type,
            entity_id=record.id,
        )


def _register_with_body(
    decorator: Callable[[Callable[..., object]], object],
    endpoint: Callable[..., object],
    body_model: type[BaseModel],
) -> None:
    """Gắn một endpoint sau khi thay annotation thân request bằng lớp của danh mục.

    Vì sao không khai thẳng `payload: schemas.create` ở chữ ký: đó là cú pháp
    **kiểu** đặt trên một biến, mà lớp Pydantic ở đây dựng lúc chạy — mypy chỉ
    chấp nhận nếu có một dòng miễn trừ kiểm kiểu, và ADR-015 đặt ngưỡng cho loại
    miễn trừ ấy ở **0**, có cổng CI đếm.

    Cách này tốt hơn cả về mặt kiểm kiểu, không chỉ để lách cổng: chữ ký khai bộ
    trường **chung** nên mypy kiểm được toàn bộ thân hàm (trước đó `payload` là
    `Any`, tức phần mã dùng chung cho mười bảy danh mục không được kiểm gì). Chỉ
    đúng một thứ đổi lúc chạy — kiểu mà FastAPI đọc để phân giải thân request.

    An toàn vì `inspect.signature` (thứ FastAPI dùng) đọc `__annotations__`, và
    module này cố ý không bật `from __future__ import annotations` nên chúng là
    đối tượng thật chứ không phải chuỗi.
    """
    endpoint.__annotations__["payload"] = body_model
    decorator(endpoint)


def _mount(spec: CatalogSpec) -> None:
    """Gắn sáu route của một danh mục vào router.

    Hàm lồng chứ không phương thức của một lớp: mỗi route phải **đóng gói** đúng
    một `spec` và đúng một bộ lớp Pydantic, và closure là cách diễn đạt điều đó
    mà FastAPI đọc được ở chữ ký hàm.
    """
    schemas: CatalogSchemas = build_schemas(spec)
    response_model = schemas.response
    create_route: Final[str] = f"POST {PREFIX}/{spec.slug}"
    merge_route: Final[str] = f"POST {PREFIX}/{spec.slug}/actions/merge"
    flag_help: Final[str] = (
        "Lọc theo: " + ", ".join(f"`{item.value}` ({item.title})" for item in spec.flags)
        if spec.flags
        else "Danh mục này không có bộ lọc nào"
    )

    def service_for(session: Session) -> MasterDataService[MasterDataRow]:
        return MasterDataService(session, spec.model)

    def to_response(record: MasterDataRow) -> BaseModel:
        """Bản ghi ORM → model phản hồi của danh mục này.

        `uid` chuyển sang chuỗi ở đúng chỗ này: model phản hồi khai nó là `str`
        (xem `master_data_schemas.py`) trong khi cột là `UUID`.
        """
        values: dict[str, object] = {
            field: getattr(record, field) for field in response_model.model_fields
        }
        values["uid"] = str(record.uid)
        return response_model.model_validate(values)

    @router.get(
        f"/{spec.slug}",
        response_model=schemas.list_response,
        summary=f"{spec.title} — danh sách",
    )
    def list_records(
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.VIEW)))
        ],
        factory: SessionFactory,
        parent_id: Annotated[int | None, Query()] = None,
        subtree_of: Annotated[int | None, Query()] = None,
        flag: Annotated[str | None, Query(description=flag_help)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> BaseModel:
        """Con trực tiếp của một nút, hoặc cả nhánh dưới một nút.

        Hai chế độ trong một endpoint vì màn hình cây dùng cả hai: mở dần từng
        cấp lúc duyệt, lấy trọn nhánh lúc tìm kiếm. `subtree_of` thắng khi cả hai
        cùng có — nó là câu hỏi hẹp hơn.

        **Có phân trang từ lát này**, dù mười bảy danh mục hiện tại đều nhỏ: hợp
        đồng này đã sinh ra type TypeScript ở máy khách, nên thêm phân trang sau
        là một breaking change cho cả mười bảy danh mục cùng lúc — đúng lúc 3D
        đang dựng UI trên nó. Danh mục vật tư ở lát 3B-3 là cái đầu tiên **cần**
        nó (FR-NFR-043 nói tới 10.000 dòng).

        `total` là tổng **trước** khi cắt trang: màn hình cần nó để vẽ thanh cuộn
        và để nói "1–100 trong 3.412", thứ không suy ra được từ độ dài trang.
        """
        branch_id = authorized.scope.acting_branch_id
        flag_column = _flag_column(spec, flag)
        with unit_of_work(factory, authorized.scope) as session:
            service = service_for(session)
            if subtree_of is not None:
                # Kiểm nút neo **trước** khi liệt kê (sửa sau review H-3). Không
                # có bước này thì một nút của chi nhánh khác trả về `200` với
                # danh sách rỗng còn một id không tồn tại trả `404` — đủ để dò
                # ra danh mục của chi nhánh bên cạnh có những id nào.
                ensure_visible(service.get(subtree_of), branch_id, spec)
                page = service.subtree_of(
                    subtree_of,
                    branch_id=branch_id,
                    flag_column=flag_column,
                    limit=limit,
                    offset=offset,
                )
            else:
                page = service.children_of(
                    parent_id,
                    branch_id=branch_id,
                    flag_column=flag_column,
                    limit=limit,
                    offset=offset,
                )
            return schemas.list_response(
                items=[to_response(record) for record in page.items], total=page.total
            )

    @router.get(
        f"/{spec.slug}/{{record_id}}",
        response_model=response_model,
        summary=f"{spec.title} — một bản ghi",
    )
    def get_record(
        record_id: int,
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.VIEW)))
        ],
        factory: SessionFactory,
    ) -> BaseModel:
        with unit_of_work(factory, authorized.scope) as session:
            record = service_for(session).get(record_id)
            ensure_visible(record, authorized.scope.acting_branch_id, spec)
            return to_response(record)

    def create_record(
        payload: MasterDataBaseCreateRequest,
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.CREATE)))
        ],
        factory: SessionFactory,
        settings: AppSettings,
        idempotency_key: Annotated[str, Depends(idempotency_key_dependency(create_route))],
        response: Response,
    ) -> BaseModel:
        """Tạo một bản ghi — **thực hiện đúng một lần** (FR-NFR-004).

        Lần gửi lại trả `200` kèm chính bản ghi đã tạo, không phải `201`: mã
        trạng thái là chỗ duy nhất client biết được lần này có tạo thêm gì không.
        """
        body = payload
        _ensure_branch_in_scope(body.branch_id, authorized)

        def work(session: Session) -> tuple[BaseModel, IdempotentRef]:
            service = service_for(session)
            _ensure_parent_visible(service, body.parent_id, authorized, spec)
            _ensure_references_visible(session, spec, body, authorized)
            # Cột riêng đi thẳng vào `create`, **không** qua một `update` nối
            # tiếp (sửa sau review M-2): tạo rồi sửa ngay sinh ra bản ghi mới
            # toanh mang `row_version = 2` cùng hai dòng nhật ký, đúng thứ mà
            # `create` được dựng ra để tránh — và chỉ cho những danh mục có cột
            # riêng, tức nửa số danh mục có nhật ký sạch còn nửa kia thì không.
            record = service.create(
                code=body.code,
                name=body.name,
                name_en=body.name_en,
                parent_id=body.parent_id,
                is_group=body.is_group,
                branch_id=body.branch_id,
                extra=extra_values(spec, body),
            )
            return to_response(record), IdempotentRef(
                result_type=spec.entity_type, result_id=str(record.id)
            )

        def replay(session: Session, ref: IdempotentRef) -> BaseModel:
            record = session.get(spec.model, int(ref.result_id))
            if record is not None:
                # Cùng người gọi nhưng có thể đã đổi chi nhánh đang thao tác giữa
                # hai lần gửi (sửa sau review L-6): lượt phát lại không được là
                # đường vòng để đọc một bản ghi mà đường `GET` đã từ chối.
                ensure_visible(record, authorized.scope.acting_branch_id, spec)
            if record is None:
                # Khóa còn nhưng bản ghi đã biến mất (khôi phục một phần, hoặc
                # ai đó vừa xóa). Nói thật là không tìm thấy, thay vì dựng lại
                # một phản hồi từ dữ liệu lưu trong khóa — dữ liệu đó là tham
                # chiếu, không phải bản sao.
                raise MasterDataNotFoundError(
                    "Bản ghi của lần thực hiện trước không còn tồn tại",
                    entity_type=spec.entity_type,
                    entity_id=ref.result_id,
                )
            return to_response(record)

        created_record, created = execute_once(
            factory,
            authorized.scope,
            route_key=create_route,
            key=idempotency_key,
            fingerprint=fingerprint_of(body.model_dump_json()),
            work=work,
            replay=replay,
            ttl=timedelta(hours=settings.idempotency_ttl_hours),
        )
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return created_record

    def update_record(
        record_id: int,
        payload: MasterDataBaseUpdateRequest,
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.EDIT)))
        ],
        factory: SessionFactory,
    ) -> BaseModel:
        """Sửa mô tả, cột riêng và cờ "Ngừng theo dõi" — một lượt ghi, một `row_version`."""
        body = payload
        with unit_of_work(factory, authorized.scope) as session:
            service = service_for(session)
            ensure_visible(service.get(record_id), authorized.scope.acting_branch_id, spec)
            _ensure_references_visible(session, spec, body, authorized)
            record = service.update(
                record_id,
                expected_row_version=body.row_version,
                code=body.code,
                name=body.name,
                name_en=body.name_en,
                is_active=body.is_active,
                extra=extra_values(spec, body),
            )
            return to_response(record)

    @router.put(
        f"/{spec.slug}/{{record_id}}/parent",
        response_model=response_model,
        summary=f"{spec.title} — chuyển nhánh",
    )
    def move_record(
        record_id: int,
        payload: MasterDataMoveRequest,
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.EDIT)))
        ],
        factory: SessionFactory,
    ) -> BaseModel:
        """Chuyển một nút và cả nhánh dưới nó sang nhóm cha khác (FR-SYS-011)."""
        with unit_of_work(factory, authorized.scope) as session:
            service = service_for(session)
            ensure_visible(service.get(record_id), authorized.scope.acting_branch_id, spec)
            # Cha **mới** cũng phải nhìn thấy được: chuyển một nút vào nhánh của
            # chi nhánh khác là cách đẩy nó ra khỏi tầm nhìn của chính mình.
            _ensure_parent_visible(service, payload.new_parent_id, authorized, spec)
            service.move(
                record_id,
                new_parent_id=payload.new_parent_id,
                expected_row_version=payload.row_version,
            )
            # Đọc lại sau `move`: nó chạy một câu `UPDATE` thẳng xuống DB rồi làm
            # hết hạn mọi đối tượng trong session, nên bản sao đang cầm mang
            # `path`/`level`/`row_version` cũ.
            return to_response(service.get(record_id))

    # Hai endpoint có **thân request** không gắn bằng decorator như bốn cái kia:
    # chữ ký của chúng khai kiểu tĩnh (bộ trường chung) để mypy kiểm được thân
    # hàm, rồi annotation được thay bằng lớp dựng lúc chạy của đúng danh mục
    # ngay trước khi gắn route. Xem `_register_with_body` bên dưới.
    _register_with_body(
        router.post(
            f"/{spec.slug}",
            response_model=response_model,
            status_code=status.HTTP_201_CREATED,
            summary=f"{spec.title} — tạo mới",
        ),
        create_record,
        schemas.create,
    )
    _register_with_body(
        router.put(
            f"/{spec.slug}/{{record_id}}",
            response_model=response_model,
            summary=f"{spec.title} — sửa",
        ),
        update_record,
        schemas.update,
    )

    @router.post(
        f"/{spec.slug}/actions/merge",
        response_model=MasterDataMergeResponse,
        summary=f"{spec.title} — gộp hai bản ghi trùng",
    )
    def merge_two_records(
        payload: MasterDataMergeRequest,
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.DELETE)))
        ],
        factory: SessionFactory,
        settings: AppSettings,
        idempotency_key: Annotated[str, Depends(idempotency_key_dependency(merge_route))],
    ) -> MasterDataMergeResponse:
        """Chuyển mọi tham chiếu của bản ghi nguồn sang đích rồi xóa nguồn (FR-SYS-016).

        Quyền `delete` chứ không `edit`: kết quả của thao tác này là một bản ghi
        **biến mất**, và người được sửa tên một mã hàng không đương nhiên được
        làm biến mất một mã hàng khác cùng toàn bộ chứng từ của nó.

        Có khóa idempotency vì nó **không lũy đẳng theo cách nguy hiểm nhất**:
        lần gửi lại sau khi mạng rớt sẽ thấy bản ghi nguồn đã biến mất và báo
        `404`, để người dùng ngồi đoán xem lần đầu có chạy hay không. Với khóa,
        lần gửi lại trả lại đúng báo cáo của lần đã chạy.
        """
        body = payload

        def work(session: Session) -> tuple[MasterDataMergeResponse, IdempotentRef]:
            service = service_for(session)
            branch_id = authorized.scope.acting_branch_id
            # Cả hai bản ghi phải nhìn thấy được **trước** khi gộp: không có
            # bước này thì `?source_id=` của chi nhánh khác trả về lỗi nghiệp vụ
            # khác với id không tồn tại — lại là một oracle liệt kê id (H-3).
            ensure_visible(service.get(body.source_id), branch_id, spec)
            ensure_visible(service.get(body.target_id), branch_id, spec)
            report = merge_records(
                session,
                spec.model,
                source_id=body.source_id,
                target_id=body.target_id,
                # Phạm vi **được gán**, không phải chi nhánh đang thao tác: gộp
                # là thao tác toàn công ty (xem `merge_records`), và câu hỏi ở
                # đây là "người này nhìn thấy hết dữ liệu chưa", không phải "họ
                # đang đứng ở đâu".
                actor_branch_ids=frozenset(authorized.scope.branch_ids),
                hook=spec.merge_hook,
            )
            result = MasterDataMergeResponse(
                entity_type=report.entity_type,
                source_id=report.source_id,
                source_code=report.source_code,
                target_id=report.target_id,
                total_rows_moved=report.total_rows,
                moved=[
                    MovedReferenceResponse(table=item.table, column=item.column, rows=item.rows)
                    for item in report.moved
                    if item.rows
                ],
            )
            return result, IdempotentRef(
                result_type=spec.entity_type, result_id=str(report.target_id)
            )

        def replay(session: Session, ref: IdempotentRef) -> MasterDataMergeResponse:
            """Lần gửi lại: khẳng định đích còn đó, **không** gộp lần nữa.

            Không dựng lại danh sách bảng đã chuyển: nó là kết quả của một lượt
            chạy đã xong, và đọc lại DB bây giờ sẽ cho ra số của hiện tại chứ
            không phải số của lúc đó. `total_rows_moved = 0` nói đúng điều đang
            xảy ra — lần gọi **này** không chuyển gì.
            """
            record = session.get(spec.model, int(ref.result_id))
            if record is None:
                raise MasterDataNotFoundError(
                    "Bản ghi đích của lần gộp trước không còn tồn tại",
                    entity_type=spec.entity_type,
                    entity_id=ref.result_id,
                )
            ensure_visible(record, authorized.scope.acting_branch_id, spec)
            return MasterDataMergeResponse(
                entity_type=spec.entity_type,
                source_id=body.source_id,
                source_code=None,
                target_id=record.id,
                total_rows_moved=0,
                moved=[],
            )

        merged, _created = execute_once(
            factory,
            authorized.scope,
            route_key=merge_route,
            key=idempotency_key,
            fingerprint=fingerprint_of(body.model_dump_json()),
            work=work,
            replay=replay,
            ttl=timedelta(hours=settings.idempotency_ttl_hours),
        )
        return merged

    @router.delete(
        f"/{spec.slug}/{{record_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary=f"{spec.title} — xóa",
    )
    def delete_record(
        record_id: int,
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.DELETE)))
        ],
        factory: SessionFactory,
    ) -> None:
        """Xóa thật — chỉ khi chưa ai dùng và không còn nhánh con (BR-SYS-02).

        Bản ghi đã lên chứng từ thì dùng "Ngừng theo dõi" (`PUT` với
        `is_active = false`), đúng lối FR-SYS-012 chỉ ra.
        """
        with unit_of_work(factory, authorized.scope) as session:
            service = service_for(session)
            ensure_visible(service.get(record_id), authorized.scope.acting_branch_id, spec)
            service.delete(record_id)


for _spec in CATALOG_REGISTRY.specs():
    _mount(_spec)
