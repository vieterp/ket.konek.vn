"""Chứng từ nghiệp vụ khác (FR-GLE-001) — chứng từ đầu tiên đi qua posting engine.

Import gói này là đăng ký loại chứng từ vào **hai** registry:

* registry phân quyền — mã `general_ledger.journal_voucher.*` phải có mặt
  trước khi `provision_dataset` gieo bảng `permissions`; `model_registry`
  import `journal.models` (cho Alembic và `create_all`) nên mọi đường cấp
  dataset đều đi qua đây trước.
* registry loại chứng từ của posting — cho endpoint hành động chung
  `POST /vouchers/{id}/actions/post` biết kiểm quyền nào và dựng
  `PostingRequest` ra sao mà không import module này (contract C4).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    VOUCHER_ACTIONS,
    DocumentType,
)
from ket.posting.contracts import (
    POSTING_DOCUMENT_REGISTRY,
    PostingDocumentType,
    PostingRequest,
)

JOURNAL_DOCUMENT_TYPE = "GLE"
"""Mã trên `vouchers.document_type`."""

JOURNAL_PERMISSION_MODULE = "general_ledger"
JOURNAL_PERMISSION_CODE = "journal_voucher"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=JOURNAL_PERMISSION_MODULE,
        code=JOURNAL_PERMISSION_CODE,
        actions=VOUCHER_ACTIONS,
    )
)


def _build_posting_request(session: Session, voucher_id: UUID) -> PostingRequest:
    """Đọc dòng chi tiết đã lưu và dịch sang hợp đồng của engine.

    Import cục bộ để lúc `model_registry` nạp gói này không kéo theo cả
    `posting_mapper` — đăng ký chỉ cần *tên* callable, thân nó chạy khi có
    người bấm ghi sổ thật.
    """
    from ket.modules.general_ledger.journal.models import JournalLine
    from ket.modules.general_ledger.journal.posting_mapper import to_posting_request

    lines = (
        session.execute(
            select(JournalLine)
            .where(JournalLine.voucher_id == voucher_id)
            .order_by(JournalLine.line_no)
        )
        .scalars()
        .all()
    )
    return to_posting_request(session, voucher_id, lines)


POSTING_DOCUMENT_REGISTRY.register(
    PostingDocumentType(
        code=JOURNAL_DOCUMENT_TYPE,
        permission_module=JOURNAL_PERMISSION_MODULE,
        permission_name=JOURNAL_PERMISSION_CODE,
        title="Chứng từ nghiệp vụ khác",
        build_request=_build_posting_request,
    )
)
