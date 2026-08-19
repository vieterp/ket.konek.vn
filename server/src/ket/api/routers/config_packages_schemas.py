"""Hình dạng phản hồi của `/api/v1/config-packages`."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class ConfigPackageResponse(BaseModel):
    """Một gói cấu hình — dòng của `config_packages`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    scheme: str
    legal_reference: str | None
    effective_from: date
    effective_to: date | None
    version: int
    is_builtin: bool
    activated_at: datetime | None
    activated_by: int | None


class ConfigPackageListResponse(BaseModel):
    items: list[ConfigPackageResponse]


class ConfigPackageAccountResponse(BaseModel):
    """Một TK trong cây hệ thống tài khoản của một gói cụ thể (màn hình quản trị gói).

    Khác `AccountResponse` (`accounts_schemas.py`, tra TK theo ngày hạch toán
    cho form chứng từ): endpoint này trả **cả cây** của đúng một gói, kể cả TK
    ngừng dùng — người quản trị gói cần thấy toàn bộ, không phải chỉ phần đang
    hiệu lực cho một ngày.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    name_en: str | None
    parent_id: int | None
    path: str
    level: int
    balance_nature: int
    is_summary: bool
    is_foreign_currency: bool
    detail_tracking: list[str] | None
    is_locked: bool
    is_inactive: bool


class ConfigPackageAccountsResponse(BaseModel):
    package_id: int
    items: list[ConfigPackageAccountResponse]
