"""Hình dạng request/response của tài khoản ngân hàng đối tác (FR-SYS-033)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ket.kernel.master_data.base import NAME_MAX_LENGTH
from ket.kernel.master_data.models.employee import BANK_ACCOUNT_MAX_LENGTH
from ket.kernel.master_data.models.partner_bank_account import BANK_BRANCH_MAX_LENGTH

AccountNumberField = Field(min_length=1, max_length=BANK_ACCOUNT_MAX_LENGTH)
AccountHolderField = Field(min_length=1, max_length=NAME_MAX_LENGTH)


class PartnerBankAccountResponse(BaseModel):
    """Một tài khoản ngân hàng của đối tác."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    partner_id: int
    bank_id: int
    account_number: str
    account_holder: str
    bank_branch: str | None
    is_default: bool
    is_active: bool
    row_version: int


class PartnerBankAccountListResponse(BaseModel):
    """Toàn bộ tài khoản của một đối tác — không phân trang, xem router."""

    items: list[PartnerBankAccountResponse]


class PartnerBankAccountCreateRequest(BaseModel):
    """Thêm một tài khoản.

    `is_default = false` **không** bảo đảm tài khoản này không thành mặc định:
    tài khoản đầu tiên của một đối tác luôn là mặc định (xem
    `PartnerBankAccountService.add`). Phản hồi trả về cờ thật, nên màn hình luôn
    thấy đúng trạng thái sau khi ghi.
    """

    model_config = ConfigDict(extra="forbid")

    bank_id: int = Field(ge=1)
    account_number: str = AccountNumberField
    account_holder: str = AccountHolderField
    bank_branch: str | None = Field(default=None, max_length=BANK_BRANCH_MAX_LENGTH)
    is_default: bool = False


class PartnerBankAccountUpdateRequest(BaseModel):
    """Sửa một tài khoản — gửi **trọn** giá trị mới, có kiểm phiên bản (FR-NFR-005).

    Mọi trường bắt buộc chứ không "chỉ gửi phần đổi", cùng lối
    `MasterDataBaseUpdateRequest` đã đi: form sửa hiện đủ các ô, nên thân request
    thiếu ô nào nghĩa là client tự quyết bỏ qua ô đó — và một cờ boolean thiếu
    không phân biệt được với `false`.
    """

    model_config = ConfigDict(extra="forbid")

    row_version: int = Field(ge=1)
    bank_id: int = Field(ge=1)
    account_number: str = AccountNumberField
    account_holder: str = AccountHolderField
    bank_branch: str | None = Field(default=None, max_length=BANK_BRANCH_MAX_LENGTH)
    is_default: bool
    is_active: bool
