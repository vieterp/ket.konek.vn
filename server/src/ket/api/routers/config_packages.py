"""Quản trị gói cấu hình pháp lý qua HTTP (`/api/v1/config-packages`) — lát 5A.

Bốn việc: liệt kê gói, xem cây TK của một gói, kích hoạt, nhập gói `.zip` đã ký
(RT-07). Tầng duy nhất trong hệ thống được phép biết cả `ket.kernel.config` lẫn
`ket.posting` (luật phân tầng C1/C2 — `kernel` không được biết `posting`), nên
phép tính "năm tài chính nào đã có chứng từ" cho cổng chặn FR-SYS-004
(`activator.activate`) nằm ở đây, không nằm trong `activator.py` — xem docstring
đầu `kernel/config/packages/activator.py`.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.config_packages_schemas import (
    ConfigPackageAccountResponse,
    ConfigPackageAccountsResponse,
    ConfigPackageListResponse,
    ConfigPackageResponse,
)
from ket.kernel.config.accounts_models import ChartOfAccount, ConfigPackage
from ket.kernel.config.packages.activator import activate as activate_package
from ket.kernel.config.packages.importer import import_package
from ket.kernel.errors import ConfigPackageIdUnknownError
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code
from ket.posting.documents.models import Voucher

router = APIRouter(prefix="/api/v1/config-packages", tags=["config-packages"])

CONFIG_PACKAGES_PREFIX: Final[str] = "/api/v1/config-packages"
"""Xuất ra để `main.py` đặt trần thân request riêng cho `POST .../import` —
cùng lý do `ATTACHMENTS_PREFIX` tồn tại ở `attachments.py`."""

CONFIG_PACKAGE_VIEW: Final[str] = permission_code(SYSTEM_MODULE, "config_package", Action.VIEW)
CONFIG_PACKAGE_ACTIVATE: Final[str] = permission_code(SYSTEM_MODULE, "config_package", Action.EDIT)
CONFIG_PACKAGE_IMPORT: Final[str] = permission_code(SYSTEM_MODULE, "config_package", Action.CREATE)

ConfigPackageReader = Annotated[AuthorizedRequest, Depends(require_permission(CONFIG_PACKAGE_VIEW))]
ConfigPackageActivator = Annotated[
    AuthorizedRequest, Depends(require_permission(CONFIG_PACKAGE_ACTIVATE))
]
ConfigPackageImporter = Annotated[
    AuthorizedRequest, Depends(require_permission(CONFIG_PACKAGE_IMPORT))
]

ACTIVATE_ROUTE: Final[str] = "POST /api/v1/config-packages/{package_id}/actions/activate"
IMPORT_ROUTE: Final[str] = "POST /api/v1/config-packages/import"

ActivateKey = Annotated[str, Depends(idempotency_key_dependency(ACTIVATE_ROUTE))]
ImportKey = Annotated[str, Depends(idempotency_key_dependency(IMPORT_ROUTE))]


def _require_package(session: Session, package_id: int) -> ConfigPackage:
    package = session.get(ConfigPackage, package_id)
    if package is None:
        raise ConfigPackageIdUnknownError("Không có gói cấu hình với id này", package_id=package_id)
    return package


def _fiscal_year_ids_with_vouchers(session: Session) -> tuple[int, ...]:
    """Mọi năm tài chính đã có ít nhất một chứng từ — đầu vào của cổng FR-SYS-004.

    Truy vấn theo `period_id` (mọi chứng từ đều có, bất kể trạng thái): một
    chứng từ ở trạng thái Nhập cũng đã "phát sinh" theo nghĩa FR-SYS-004 —
    chế độ kế toán của năm đó không còn là tờ giấy trắng nữa, bất kể chứng từ
    đã ghi sổ hay chưa.
    """
    rows = session.execute(
        select(AccountingPeriod.fiscal_year_id)
        .join(Voucher, Voucher.period_id == AccountingPeriod.id)
        .distinct()
    ).scalars()
    return tuple(rows)


@router.get("", response_model=ConfigPackageListResponse)
def list_config_packages(
    authorized: ConfigPackageReader, factory: SessionFactory
) -> ConfigPackageListResponse:
    """Mọi gói cấu hình đã có trong dataset — dựng sẵn lẫn nhập thêm, mới nhất trước."""
    with unit_of_work(factory, authorized.scope) as session:
        rows = (
            session.execute(select(ConfigPackage).order_by(ConfigPackage.effective_from.desc()))
            .scalars()
            .all()
        )
        return ConfigPackageListResponse(
            items=[ConfigPackageResponse.model_validate(row) for row in rows]
        )


@router.get("/{package_id}/accounts", response_model=ConfigPackageAccountsResponse)
def get_package_accounts(
    package_id: int, authorized: ConfigPackageReader, factory: SessionFactory
) -> ConfigPackageAccountsResponse:
    """Toàn bộ cây TK của một gói — kể cả TK ngừng dùng (màn hình quản trị gói,
    khác `GET /api/v1/accounts` là tra theo ngày hạch toán cho form chứng từ).
    """
    with unit_of_work(factory, authorized.scope) as session:
        _require_package(session, package_id)
        rows = (
            session.execute(
                select(ChartOfAccount)
                .where(ChartOfAccount.package_id == package_id)
                .order_by(ChartOfAccount.path)
            )
            .scalars()
            .all()
        )
        return ConfigPackageAccountsResponse(
            package_id=package_id,
            items=[ConfigPackageAccountResponse.model_validate(row) for row in rows],
        )


@router.post(
    "/{package_id}/actions/activate",
    response_model=ConfigPackageResponse,
    responses={200: {"model": ConfigPackageResponse, "description": "Lần gửi lại: gói hiện tại"}},
)
def activate_config_package(
    package_id: int,
    authorized: ConfigPackageActivator,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: ActivateKey,
    response: Response,
) -> ConfigPackageResponse:
    """Kích hoạt một gói cấu hình (FR-SYS-004). Chặn khi đổi `scheme` trên dữ
    liệu đã có chứng từ theo chế độ khác — xem `activator.activate`.
    """

    def work(session: Session) -> tuple[ConfigPackageResponse, IdempotentRef]:
        fiscal_year_ids = _fiscal_year_ids_with_vouchers(session)
        package = activate_package(
            session,
            package_id=package_id,
            actor=authorized.scope.user_id,
            fiscal_year_ids_with_vouchers=fiscal_year_ids,
        )
        return ConfigPackageResponse.model_validate(package), IdempotentRef(
            result_type=ConfigPackage.__tablename__, result_id=str(package.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ConfigPackageResponse:
        package = _require_package(session, int(ref.result_id))
        return ConfigPackageResponse.model_validate(package)

    result, _performed = execute_once(
        factory,
        authorized.scope,
        route_key=ACTIVATE_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(f"{ACTIVATE_ROUTE}:{package_id}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    # `200` cho cả hai lần: kích hoạt đổi trạng thái một bản ghi đã tồn tại,
    # không tạo bản ghi mới — cùng quy ước với `post_voucher`.
    response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/import",
    response_model=ConfigPackageResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": ConfigPackageResponse, "description": "Lần gửi lại: gói đã nhập"}},
)
def import_config_package(
    authorized: ConfigPackageImporter,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: ImportKey,
    response: Response,
    file: Annotated[UploadFile, File()],
) -> ConfigPackageResponse:
    """Nhập một gói `.zip` đã ký (RT-07). Sai chữ ký/checksum/cấu trúc → từ
    chối cả gói, không ghi gì (`importer.import_package`).

    Đọc trọn nội dung tệp **trước** khi mở transaction ghi: verify chữ ký +
    checksum là việc CPU-bound, không cần giữ transaction DB mở trong lúc đó.
    """
    archive_bytes = file.file.read()

    def work(session: Session) -> tuple[ConfigPackageResponse, IdempotentRef]:
        package = import_package(session, archive_bytes=archive_bytes)
        return ConfigPackageResponse.model_validate(package), IdempotentRef(
            result_type=ConfigPackage.__tablename__, result_id=str(package.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> ConfigPackageResponse:
        package = _require_package(session, int(ref.result_id))
        return ConfigPackageResponse.model_validate(package)

    result, created = execute_once(
        factory,
        authorized.scope,
        route_key=IMPORT_ROUTE,
        key=idempotency_key,
        # Vân tay theo **nội dung** tệp: dùng lại một khóa cho một `.zip` khác
        # là lỗi client và phải nổ ra, giống hệt lý do `upload_attachment` băm
        # nội dung vào vân tay. Băm trước bằng sha256 rồi mới đưa vào
        # `fingerprint_of` — tránh nhân đôi bộ nhớ của tệp vài MB qua `.hex()`.
        fingerprint=fingerprint_of(hashlib.sha256(archive_bytes).hexdigest()),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return result
