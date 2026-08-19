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


@dataclass(frozen=True)
class PostingDocumentType:
    """Một loại chứng từ ghi sổ được qua endpoint hành động chung."""

    code: str
    """Mã trên `vouchers.document_type` (`GLE`, `PT`, …)."""

    permission_module: str
    permission_name: str
    """Cặp ghép thành mã quyền `{module}.{name}.{action}` — trùng với
    `DocumentType` đã đăng ký bên registry phân quyền."""

    title: str
    build_request: RequestBuilder

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
