"""Hình dạng phản hồi của `/api/v1/auto-posting` (FR-SYS-025, lát 6A)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AutoPostingOperationResponse(BaseModel):
    """Một nghiệp vụ đã phân giải — form đọc và điền sẵn cặp Nợ/Có."""

    model_config = ConfigDict(from_attributes=True)

    operation_code: str
    operation_name: str
    debit_account_code: str | None
    """`None` = gói không điền sẵn bên Nợ — người dùng tự chọn TK."""
    credit_account_code: str | None
    requires_partner: bool
    partner_kind: int | None
    """`kernel.contracts.PartnerKind` (0 khách, 1 NCC, 2 nhân viên); `None` =
    không gợi ý loại."""
    display_order: int


class AutoPostingOperationsResponse(BaseModel):
    package_id: int
    """Gói cấu hình đã phân giải theo `(chế độ của năm, on_date)` — client so
    với `package_id` của `/accounts` để chắc hai lượt tra cùng một gói."""

    items: list[AutoPostingOperationResponse]
