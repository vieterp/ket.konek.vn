"""Registry loại chứng từ của posting — mã loại → quyền + cách dựng định khoản.

Vì sao tồn tại: endpoint `POST /vouchers/{id}/actions/post` là **một** cho mọi
loại chứng từ (phase-04 §API surface), nhưng ghi sổ cần hai thứ chỉ module sở
hữu mới biết — mã quyền phải kiểm (`general_ledger.journal_voucher.post`) và
cách dịch chi tiết thành `PostingRequest`. Module đăng ký hai thứ đó lúc
import; posting không import module nào (luật phụ thuộc, contract C4) mà chỉ
gọi qua callable đã đăng ký — cùng khuôn "Protocol đăng ký lúc khởi động" của
plan §Luật phụ thuộc #2.

Khác với registry **phân quyền** (`kernel/security/permissions.py`): bên đó
trả lời "mã quyền nào tồn tại", bên này trả lời "chứng từ loại này ghi sổ thế
nào". Một loại chứng từ ghi sổ được phải có mặt ở **cả hai**.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.errors import DocumentTypeUnknownError
from ket.kernel.security.permissions import Action, permission_code
from ket.posting.engine.requests import PostingRequest

RequestBuilder = Callable[[Session, UUID], PostingRequest]
"""Dựng `PostingRequest` từ chi tiết đã lưu của một chứng từ."""

LifecycleHook = Callable[[Session, UUID, int], None]
"""`(session, voucher_id, user_id)` — việc riêng của module quanh một bước
vòng đời, chạy trong CÙNG transaction với bước đó."""


@dataclass(frozen=True)
class PostingDocumentType:
    """Một loại chứng từ ghi sổ được qua endpoint hành động chung.

    Ba hook vòng đời (thêm ở lát 6B): phiếu thu/chi phải cộng số đã trả vào
    chứng từ công nợ khi ghi sổ, gỡ ra khi bỏ ghi sổ, và trả bộ đếm tham chiếu
    danh mục trước khi xóa — mà endpoint hành động chung (`routers/vouchers.py`)
    gọi thẳng `PostingService`/`VoucherService`, không đi qua service của
    module. Không có hook thì mỗi module có việc-đi-kèm sẽ phải tự mở endpoint
    hành động riêng, phá "một bộ endpoint cho mọi loại chứng từ" của phase-04.
    Hook chạy cùng transaction: ghi sổ và việc-đi-kèm cùng sống cùng chết.
    """

    code: str
    """Mã trên `vouchers.document_type` (`GLE`, `PT`, …)."""

    permission_module: str
    permission_name: str
    """Cặp ghép thành mã quyền `{module}.{name}.{action}` — trùng với
    `DocumentType` đã đăng ký bên registry phân quyền."""

    title: str
    build_request: RequestBuilder

    after_post: LifecycleHook | None = None
    """Chạy SAU khi `PostingService.post` xong, cùng transaction."""

    after_unpost: LifecycleHook | None = None
    """Chạy SAU khi `PostingService.unpost` xong, cùng transaction."""

    before_delete: LifecycleHook | None = None
    """Chạy TRƯỚC `VoucherService.delete`, cùng transaction — dọn những gì
    `ON DELETE CASCADE` không tự dọn được (bộ đếm tham chiếu, dấu vết ngoài)."""

    def permission(self, action: Action) -> str:
        return permission_code(self.permission_module, self.permission_name, action)


class PostingDocumentRegistry:
    """Sổ đăng ký của một tiến trình — đối tượng để test dựng registry riêng."""

    def __init__(self) -> None:
        self._types: dict[str, PostingDocumentType] = {}

    def register(self, document_type: PostingDocumentType) -> None:
        if document_type.code in self._types:
            raise ValueError(f"Loại chứng từ {document_type.code} đã được đăng ký")
        self._types[document_type.code] = document_type

    def get(self, code: str) -> PostingDocumentType:
        document_type = self._types.get(code)
        if document_type is None:
            raise DocumentTypeUnknownError(
                "Loại chứng từ này chưa được đăng ký với posting engine", document_type=code
            )
        return document_type

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))


REGISTRY: Final[PostingDocumentRegistry] = PostingDocumentRegistry()
"""Registry của tiến trình. Module đăng ký loại của mình lúc import — cùng chỗ
với đăng ký mã quyền (xem `modules/general_ledger/journal/__init__.py`)."""
