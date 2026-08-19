"""Khóa / mở kỳ kế toán — nửa `FOR UPDATE` của cơ chế RT-09 (phase 4, lát 4D).

Vì sao nằm ở `posting` chứ không `kernel.periods`: cổng khóa sổ phải nhìn
`balance_recalc_queue`, `opening_balances` và `vouchers` — toàn bảng của
posting, mà `kernel` **không được** import `ket.posting` (luật phụ thuộc #5).
Kernel giữ phần "kỳ là gì" (`PeriodService`); ở đây là phần "khi nào được
khóa nó".

Import gói này (qua `ket.model_registry`, như mọi đường cấp dataset) là đăng
ký mã quyền `posting.period.post` (khóa kỳ) và `posting.period.unpost` (mở kỳ
— quyền **riêng**, FR-GLE-031) — phải có mặt trước khi `provision_dataset`
gieo bảng `permissions`. Dùng cặp hành vi post/unpost của bộ `Action` đóng:
khóa sổ chính là "ghi sổ" một kỳ — nó chốt số liệu và chỉ đảo được bằng một
thao tác có vết, có quyền riêng.
"""

from __future__ import annotations

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    Action,
    DocumentType,
)

PERMISSION_REGISTRY.register(
    DocumentType(
        module="posting",
        code="period",
        # Chỉ hai hành vi: kỳ kế toán do `create_fiscal_year` sinh ra cả loạt,
        # không ai "tạo"/"sửa"/"xóa" lẻ một kỳ qua màn phân quyền này.
        actions=frozenset({Action.POST, Action.UNPOST}),
    )
)
