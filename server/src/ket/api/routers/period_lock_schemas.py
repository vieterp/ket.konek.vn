"""Schema của endpoint khóa / mở kỳ (`/api/v1/periods/{id}/actions/*`)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

UNLOCK_REASON_MAX_LENGTH = 500


class PeriodResponse(BaseModel):
    """Dòng kỳ sau thao tác — đủ để màn hình khóa sổ vẽ lại trạng thái."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    fiscal_year_id: int
    period_no: int
    start_date: date
    end_date: date
    locked_at: datetime | None
    locked_by: int | None


class LockWarningResponse(BaseModel):
    """Một việc còn dở mà lượt khóa cho qua nhưng phải nói ra (U11)."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    message: str
    count: int
    sample: tuple[str, ...]


class PeriodLockResponse(BaseModel):
    period: PeriodResponse
    warnings: tuple[LockWarningResponse, ...]


class PeriodUnlockRequest(BaseModel):
    """Mở kỳ bắt buộc nêu lý do — nó đi thẳng vào `audit_log` (FR-GLE-031).

    `strip_whitespace` trước `min_length`: một chuỗi toàn khoảng trắng là "không
    có lý do" mặc áo hợp lệ, và chỗ nó lộ ra là bản ghi kiểm toán — quá muộn.
    """

    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=UNLOCK_REASON_MAX_LENGTH),
    ]
