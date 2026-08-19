"""Endpoint khóa / mở kỳ kế toán (phase-04 §API surface, bước 14).

Hai hành động, hai mã quyền tách bạch: `posting.period.post` (khóa) và
`posting.period.unpost` (mở — quyền **riêng**, FR-GLE-031). Cả hai đổi trạng
thái nên đều đòi khóa idempotency khai theo route (RT-12); gửi lại cùng khóa
trả đúng kết quả cũ chứ không chạy lại phép khóa.

Toàn bộ luật (FOR UPDATE, tuần tự, hàng đợi rỗng, checklist, cảnh báo) nằm ở
`posting/periods/lock_service.py` — router chỉ dịch HTTP ↔ service.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from ket.api.dependencies import AppSettings, Authorized, SessionFactory
from ket.api.idempotency import idempotency_key_dependency
from ket.api.routers.period_lock_schemas import (
    LockWarningResponse,
    PeriodLockResponse,
    PeriodResponse,
    PeriodUnlockRequest,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.security.permissions import Action, permission_code
from ket.posting.periods.lock_service import PeriodLockService

router = APIRouter(prefix="/api/v1/periods", tags=["periods"])

LOCK_PERMISSION: Final[str] = permission_code("posting", "period", Action.POST)
UNLOCK_PERMISSION: Final[str] = permission_code("posting", "period", Action.UNPOST)

LOCK_ROUTE: Final[str] = "POST /api/v1/periods/{period_id}/actions/lock"
UNLOCK_ROUTE: Final[str] = "POST /api/v1/periods/{period_id}/actions/unlock"

LockKey = Annotated[str, Depends(idempotency_key_dependency(LOCK_ROUTE))]
UnlockKey = Annotated[str, Depends(idempotency_key_dependency(UNLOCK_ROUTE))]


def _period_ref(period: AccountingPeriod) -> IdempotentRef:
    return IdempotentRef(result_type=AccountingPeriod.__tablename__, result_id=str(period.id))


def _replay_period(session: Session, ref: IdempotentRef) -> PeriodResponse:
    period = session.get(AccountingPeriod, int(ref.result_id))
    if period is None:  # pragma: no cover - kỳ không xóa được (FK RESTRICT)
        raise RuntimeError(f"Khóa idempotency trỏ vào kỳ không tồn tại: {ref.result_id}")
    return PeriodResponse.model_validate(period)


@router.post("/{period_id}/actions/lock", response_model=PeriodLockResponse)
def lock_period(
    period_id: int,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: LockKey,
    response: Response,
) -> PeriodLockResponse:
    """Khóa sổ một kỳ (RT-09, U11). Cảnh báo trong response, không nằm trong khóa replay.

    Lần gửi lại trả về dòng kỳ với danh sách cảnh báo **rỗng**: khóa idempotency
    lưu *tham chiếu* kết quả chứ không phải thân response (RT-12), và cảnh báo
    là thông tin của đúng khoảnh khắc khóa — phát lại một con số đếm cũ còn dễ
    gây hiểu nhầm hơn là không nói gì.
    """
    authorized.access.require(LOCK_PERMISSION)

    def work(session: Session) -> tuple[PeriodLockResponse, IdempotentRef]:
        outcome = PeriodLockService(session).lock(period_id, scope=authorized.scope)
        body = PeriodLockResponse(
            period=PeriodResponse.model_validate(outcome.period),
            warnings=tuple(
                LockWarningResponse.model_validate(warning) for warning in outcome.warnings
            ),
        )
        return body, _period_ref(outcome.period)

    def replay(session: Session, ref: IdempotentRef) -> PeriodLockResponse:
        return PeriodLockResponse(period=_replay_period(session, ref), warnings=())

    result, _performed = execute_once(
        factory,
        authorized.scope,
        route_key=LOCK_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(f"{LOCK_ROUTE}:{period_id}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_200_OK
    return result


@router.post("/{period_id}/actions/unlock", response_model=PeriodLockResponse)
def unlock_period(
    period_id: int,
    payload: PeriodUnlockRequest,
    authorized: Authorized,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: UnlockKey,
    response: Response,
) -> PeriodLockResponse:
    """Mở lại kỳ đã khóa — quyền riêng + lý do bắt buộc, ghi audit (FR-GLE-031)."""
    authorized.access.require(UNLOCK_PERMISSION)

    def work(session: Session) -> tuple[PeriodLockResponse, IdempotentRef]:
        period = PeriodLockService(session).unlock(
            period_id, scope=authorized.scope, reason=payload.reason
        )
        return (
            PeriodLockResponse(period=PeriodResponse.model_validate(period), warnings=()),
            _period_ref(period),
        )

    def replay(session: Session, ref: IdempotentRef) -> PeriodLockResponse:
        return PeriodLockResponse(period=_replay_period(session, ref), warnings=())

    result, _performed = execute_once(
        factory,
        authorized.scope,
        route_key=UNLOCK_ROUTE,
        key=idempotency_key,
        # Vân tay kèm cả lý do: cùng khóa cho một lý do khác không phải "gửi
        # lại" — nó là một lượt mở khác đang mượn khóa cũ.
        fingerprint=fingerprint_of(f"{UNLOCK_ROUTE}:{period_id}:{payload.reason}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_200_OK
    return result
