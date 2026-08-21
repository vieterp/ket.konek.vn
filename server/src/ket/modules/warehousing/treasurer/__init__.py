"""Hàng đợi và sổ quỹ của thủ quỹ (SRS 17 §3.1) — phần thủ quỹ của module
`warehousing`. Lát 6A: model sổ quỹ; lát 6C: hàng đợi + ghi sổ hàng loạt.

Import gói này (qua `ket.model_registry`) là đăng ký:

* **phân quyền** — `treasurer.cash_book.{view,post}`: vai trò Thủ quỹ chỉ cần
  hai mã này (+ quyền XEM phiếu thu/chi để đọc chi tiết đề nghị) — không có
  `edit`/`delete` nào để cấp, đúng FR-WHK-020/BR-WHK-04: thủ quỹ không sửa
  được chứng từ kế toán bằng THIẾT KẾ bộ quyền, không phải bằng kỷ luật gán.
* **bản cài `TreasurerCashBook`** (kernel Protocol) — cửa duy nhất ghi/gỡ dòng
  `treasurer_cash_book`, cho cả hàng đợi lẫn đường phân-hệ-tắt của `cash_book`.
"""

from __future__ import annotations

from ket.kernel.protocols import PROVIDERS
from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    Action,
    DocumentType,
)

TREASURER_PERMISSION_MODULE = "treasurer"
TREASURER_CASH_BOOK_CODE = "cash_book"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=TREASURER_PERMISSION_MODULE,
        code=TREASURER_CASH_BOOK_CODE,
        actions=frozenset({Action.VIEW, Action.POST}),
    )
)


def _register_cash_book() -> None:
    from ket.modules.warehousing.treasurer.book import TreasurerCashBookImpl

    PROVIDERS.register_treasurer_cash_book(TreasurerCashBookImpl())


_register_cash_book()
