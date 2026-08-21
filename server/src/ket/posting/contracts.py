"""Ranh giới công khai của `ket.posting`.

Mọi thứ module khác được phép chạm phải khai ở đây (Protocol / Pydantic model
/ dataclass có kiểu). Cấm `dict[str, Any]` qua ranh giới module — ADR-015.
Đây cũng là đích neo cho `import-linter` (ADR-004).

Từ phase 4: module nghiệp vụ dựng `PostingRequest` rồi gọi `PostingService`
(luật phụ thuộc #3 — chỉ posting được chạm `gl_postings`); header chứng từ đi
qua `VoucherService`. Hai lớp dịch vụ nhận `Session` đang mở và không tự
commit (FR-NFR-003).
"""

from __future__ import annotations

from ket.posting.documents.models import EntryKind, Voucher, VoucherStatus
from ket.posting.documents.registry import (
    REGISTRY as POSTING_DOCUMENT_REGISTRY,
)
from ket.posting.documents.registry import (
    PostingDocumentType,
)
from ket.posting.documents.service import VoucherDraft, VoucherService
from ket.posting.documents.state_machine import VoucherAction
from ket.posting.engine.dimensions import (
    ExtendedDimensionValue,
    PartnerKind,
    PostingDimensions,
)
from ket.posting.engine.guards import (
    GUARD_REGISTRY,
    GUARD_WARNING_DETAIL,
    GuardFinding,
    PostingGuard,
)
from ket.posting.engine.models import AMOUNT_PRECISION, AMOUNT_SCALE, Ledger
from ket.posting.engine.requests import PostingLine, PostingRequest
from ket.posting.engine.service import PostingService

__all__ = [
    "AMOUNT_PRECISION",
    "AMOUNT_SCALE",
    "GUARD_REGISTRY",
    "GUARD_WARNING_DETAIL",
    "POSTING_DOCUMENT_REGISTRY",
    "EntryKind",
    "ExtendedDimensionValue",
    "GuardFinding",
    "Ledger",
    "PartnerKind",
    "PostingDimensions",
    "PostingDocumentType",
    "PostingGuard",
    "PostingLine",
    "PostingRequest",
    "PostingService",
    "Voucher",
    "VoucherAction",
    "VoucherDraft",
    "VoucherService",
    "VoucherStatus",
]
