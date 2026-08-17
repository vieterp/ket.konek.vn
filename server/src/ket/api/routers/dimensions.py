"""Endpoint chiều phân tích mở rộng (`/api/v1/dimensions`) — LD-08, FR-SYS-051.

Tách khỏi `routers/master_data.py` dù cùng nhóm màn hình, vì hai thứ khác hẳn
nhau về **hình dạng**: danh mục có mười bảy bảng cùng một bộ cột nên route sinh
ra từ registry, còn chiều phân tích có đúng hai bảng và một quan hệ cha–con giữa
chúng. Ép nó vào bộ sinh route kia sẽ phải nới bộ sinh cho một trường hợp duy
nhất — đúng lối "bẻ cong khung dùng chung" mà §Risk của phase 3 cảnh báo.

**Ai gọi ở v1:** công cụ quản trị và gói cấu hình phase 5. Màn hình cho người
dùng cuối tự khai chiều hoãn tới v1.1 (RT-20) — nhưng đường ghi thì có ngay từ
đây, vì đó là thứ làm cho "khai bằng cấu hình, không sửa code" thành sự thật
kiểm chứng được thay vì một lời hứa.

**Chưa có ở lát này:** ép chiều bắt buộc theo `applies_to_accounts`. Nó cần một
dòng hạch toán thật để ép, nên nó sống cùng posting engine ở phase 4.
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
from ket.api.routers.dimensions_schemas import (
    DimensionDeclareRequest,
    DimensionListResponse,
    DimensionResponse,
    DimensionValueAddRequest,
    DimensionValueListResponse,
    DimensionValueResponse,
)
from ket.kernel.dimensions.models import AnalysisDimension, AnalysisDimensionValue
from ket.kernel.dimensions.service import DimensionService
from ket.kernel.errors import DimensionNotFoundError, DimensionValueNotFoundError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import MASTER_MODULE, Action, permission_code

router = APIRouter(prefix="/api/v1/dimensions", tags=["master-data"])

DIMENSION: Final[str] = "analysis_dimension"

DIMENSION_VIEW = permission_code(MASTER_MODULE, DIMENSION, Action.VIEW)
DIMENSION_CREATE = permission_code(MASTER_MODULE, DIMENSION, Action.CREATE)

DimensionReader = Annotated[AuthorizedRequest, Depends(require_permission(DIMENSION_VIEW))]
DimensionAuthor = Annotated[AuthorizedRequest, Depends(require_permission(DIMENSION_CREATE))]

DECLARE_ROUTE: Final[str] = "POST /api/v1/dimensions"
ADD_VALUE_ROUTE: Final[str] = "POST /api/v1/dimensions/{code}/values"

DeclareKey = Annotated[str, Depends(idempotency_key_dependency(DECLARE_ROUTE))]
AddValueKey = Annotated[str, Depends(idempotency_key_dependency(ADD_VALUE_ROUTE))]


@router.get("", response_model=DimensionListResponse)
def list_dimensions(
    authorized: DimensionReader,
    factory: SessionFactory,
    include_inactive: Annotated[bool, Query()] = False,
) -> DimensionListResponse:
    """Chiều phân tích đã khai trong dữ liệu kế toán đang mở.

    Mặc định **ẩn** chiều đã ngừng theo dõi: đây là nguồn dựng ô chọn trên form
    nhập liệu, và một chiều đã tắt hiện ra ở đó thì việc tắt nó chẳng có tác
    dụng gì. Màn hình cấu hình gọi kèm `include_inactive=true`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        dimensions = DimensionService(session).list_dimensions(include_inactive=include_inactive)
        return DimensionListResponse(
            items=[DimensionResponse.model_validate(item) for item in dimensions]
        )


@router.get("/{code}", response_model=DimensionResponse)
def get_dimension(
    code: str, authorized: DimensionReader, factory: SessionFactory
) -> DimensionResponse:
    with unit_of_work(factory, authorized.scope) as session:
        return DimensionResponse.model_validate(DimensionService(session).get_by_code(code))


@router.get("/{code}/values", response_model=DimensionValueListResponse)
def list_dimension_values(
    code: str,
    authorized: DimensionReader,
    factory: SessionFactory,
    include_inactive: Annotated[bool, Query()] = False,
) -> DimensionValueListResponse:
    """Giá trị của một chiều `list`, theo thứ tự cây.

    Chiều lấy giá trị từ danh mục (`value_source = master`) trả lỗi ở đây chứ
    không trả danh sách rỗng: rỗng nghĩa là "chưa ai khai giá trị nào", còn đây
    là "hỏi nhầm chỗ" — và client phải đi đọc `/api/v1/master/{master_slug}`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        service = DimensionService(session)
        dimension = service.get_by_code(code)
        values = service.values_of(dimension, include_inactive=include_inactive)
        return DimensionValueListResponse(
            items=[DimensionValueResponse.model_validate(value) for value in values]
        )


@router.post("", response_model=DimensionResponse, status_code=status.HTTP_201_CREATED)
def declare_dimension(
    payload: DimensionDeclareRequest,
    authorized: DimensionAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: DeclareKey,
    response: Response,
) -> DimensionResponse:
    """Khai một chiều phân tích mới — thêm một dòng, không migration.

    Đi đường idempotency đầy đủ như mọi `POST` tạo bản ghi khác, dù
    `analysis_dimensions.code` đã có ràng buộc `UNIQUE`. Ràng buộc chỉ chặn được
    **bản ghi thứ hai**; nó không cho người gửi lại biết lần trước đã thành công
    — họ nhận `409` và phải tự đoán xem chiều đó là của mình hay của đồng
    nghiệp. Với khóa, lần bấm lại trả về chính bản ghi đã tạo và `200`.
    """

    def work(session: Session) -> tuple[DimensionResponse, IdempotentRef]:
        dimension = DimensionService(session).declare(
            code=payload.code,
            name=payload.name,
            name_en=payload.name_en,
            value_source=payload.value_source,
            master_slug=payload.master_slug,
            is_required=payload.is_required,
            applies_to_accounts=payload.applies_to_accounts,
        )
        return DimensionResponse.model_validate(dimension), IdempotentRef(
            result_type=AnalysisDimension.__tablename__, result_id=str(dimension.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> DimensionResponse:
        dimension = session.get(AnalysisDimension, int(ref.result_id))
        if dimension is None:
            raise DimensionNotFoundError(
                "Chiều phân tích của lần thực hiện trước không còn tồn tại", code=ref.result_id
            )
        return DimensionResponse.model_validate(dimension)

    declared, created = execute_once(
        factory,
        authorized.scope,
        route_key=DECLARE_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(payload.model_dump_json()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return declared


@router.post(
    "/{code}/values", response_model=DimensionValueResponse, status_code=status.HTTP_201_CREATED
)
def add_dimension_value(
    code: str,
    payload: DimensionValueAddRequest,
    authorized: DimensionAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: AddValueKey,
    response: Response,
) -> DimensionValueResponse:
    """Thêm một giá trị vào chiều `list` — cùng lối idempotency như `declare`.

    Dấu vân tay **kèm mã chiều**: khóa idempotency có phạm vi theo route khai
    báo (`/api/v1/dimensions/{code}/values`, chưa điền `code`), nên hai lời gọi
    thêm giá trị `BAC` cho hai chiều khác nhau sẽ dùng cùng một phạm vi. Không
    đưa `code` vào vân tay thì lời gọi thứ hai bị coi là "gửi lại" và nhận về
    giá trị của chiều thứ nhất.
    """

    def work(session: Session) -> tuple[DimensionValueResponse, IdempotentRef]:
        service = DimensionService(session)
        dimension = service.get_by_code(code)
        parent = (
            service.resolve_value_by_id(dimension, payload.parent_id)
            if payload.parent_id is not None
            else None
        )
        value = service.add_value(
            dimension,
            code=payload.code,
            name=payload.name,
            name_en=payload.name_en,
            parent=parent,
        )
        return DimensionValueResponse.model_validate(value), IdempotentRef(
            result_type=AnalysisDimensionValue.__tablename__, result_id=str(value.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> DimensionValueResponse:
        value = session.get(AnalysisDimensionValue, int(ref.result_id))
        if value is None:
            raise DimensionValueNotFoundError(
                "Giá trị của lần thực hiện trước không còn tồn tại",
                dimension=code,
                value_id=ref.result_id,
            )
        return DimensionValueResponse.model_validate(value)

    added, created = execute_once(
        factory,
        authorized.scope,
        route_key=ADD_VALUE_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(f"{code}:{payload.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return added
