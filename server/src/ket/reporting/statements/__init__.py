"""Lập BCTC từ layout công thức (lát 5B) — phần ĐỌC SỐ của FR-GLE-043.

Import gói này (qua `ket.model_registry`) đăng ký mã quyền
`reporting.statement.view` — phải có mặt trước khi `provision_dataset` gieo
bảng `permissions`, cùng cơ chế với `posting.balances`.

Grammar/evaluator nằm ở `kernel.config.statements` (loader gói cấu hình cần
chúng, C1 cấm kernel import reporting); gói này chỉ mang phần chạm sổ cái:
đọc số dư/phát sinh (`balance_source`) và ráp cột báo cáo (`builder`).
"""

from __future__ import annotations

from typing import Final

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    Action,
    DocumentType,
    permission_code,
)

STATEMENT_PERMISSION_MODULE: Final[str] = "reporting"
STATEMENT_PERMISSION_CODE: Final[str] = "statement"

STATEMENT_VIEW: Final[str] = permission_code(
    STATEMENT_PERMISSION_MODULE, STATEMENT_PERMISSION_CODE, Action.VIEW
)

PERMISSION_REGISTRY.register(
    DocumentType(
        module=STATEMENT_PERMISSION_MODULE,
        code=STATEMENT_PERMISSION_CODE,
        # Chỉ xem: BCTC là dữ liệu SUY RA từ sổ cái — không ai "sửa" một bản
        # xem trước; kết xuất PDF/XLSX (print/export) vào ở lát 5C/5D.
        actions=frozenset({Action.VIEW}),
    )
)
