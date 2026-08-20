"""Chạy báo cáo metadata-driven (lát 5C) — phần THI HÀNH của FR-RPT-001.

Metadata (4 bảng `report_*`, spec có kiểu, seed builtin) nằm ở
`kernel/config/reports` — cùng lý do 5B đặt formula engine ở kernel: provision
phải gieo + probe fail-closed, C1 cấm kernel import reporting. Gói này chỉ mang
phần chạm dữ liệu và kết xuất: kiểm tham số (`params`), chạy SQL theo lô
(`executor`), tổng nhóm (`grouping`), điều phối render (`engine`).

Import gói này (qua `ket.model_registry`) đăng ký mã quyền
`reporting.report.{view,export}` trước khi `provision_dataset` gieo bảng
`permissions` — cùng cơ chế `reporting.statements`.
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

REPORT_PERMISSION_MODULE: Final[str] = "reporting"
REPORT_PERMISSION_CODE: Final[str] = "report"

REPORT_VIEW: Final[str] = permission_code(
    REPORT_PERMISSION_MODULE, REPORT_PERMISSION_CODE, Action.VIEW
)
REPORT_EXPORT: Final[str] = permission_code(
    REPORT_PERMISSION_MODULE, REPORT_PERMISSION_CODE, Action.EXPORT
)

PERMISSION_REGISTRY.register(
    DocumentType(
        module=REPORT_PERMISSION_MODULE,
        code=REPORT_PERMISSION_CODE,
        # `view` = danh mục báo cáo + spec tham số; `export` = kết xuất PDF/XLSX
        # (FR-RPT-006). `print` để dành cho mẫu in CHỨNG TỪ (print_templates,
        # 5D) — in một chứng từ và kết xuất một báo cáo là hai quyền khác nhau
        # trong ma trận phân quyền của khách hàng.
        actions=frozenset({Action.VIEW, Action.EXPORT}),
    )
)
