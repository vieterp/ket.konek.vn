"""Hình dạng phản hồi của endpoint tra hệ thống tài khoản (`/api/v1/accounts`)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class AccountResponse(BaseModel):
    """Một tài khoản trong gói cấu hình đang hiệu lực.

    `detail_tracking` là danh sách chiều bắt buộc khi hạch toán (FR-SYS-021) —
    form chứng từ đọc nó để hiện đúng cột chiều cho TK đã chọn. `is_summary`
    trả về để UI hiện cây nhưng chặn chọn: validator ghi sổ từ chối TK tổng
    hợp (BR-SYS-03), chặn sớm ở form đỡ một vòng gửi-nhận lỗi.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    name_en: str | None
    parent_id: int | None
    level: int
    is_summary: bool
    balance_nature: int
    is_foreign_currency: bool
    detail_tracking: list[str] | None


class AccountListResponse(BaseModel):
    """Kết quả tra TK — tối đa `limit` dòng, xếp theo số hiệu."""

    package_id: int
    items: list[AccountResponse]
