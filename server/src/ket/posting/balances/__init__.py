"""Snapshot số dư + hàng đợi tính lại + bảng cân đối TK (ADR-011, RT-22).

Import gói này (qua `ket.model_registry`, như mọi đường cấp dataset) là đăng ký
hai thứ, cùng khuôn với `modules/general_ledger/journal/__init__.py`:

* mã quyền `posting.balance.view` (xem bảng cân đối) và
  `posting.balance.create` (xếp job tính lại) — phải có mặt trước khi
  `provision_dataset` gieo bảng `permissions`;
* loại job `posting.balances.recalc` vào registry hàng đợi — worker và API
  cùng nhìn thấy nó.
"""

from __future__ import annotations

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    Action,
    DocumentType,
)
from ket.posting.balances import recalc_job as recalc_job

PERMISSION_REGISTRY.register(
    DocumentType(
        module="posting",
        code="balance",
        # Chỉ hai hành vi: số dư là dữ liệu SUY RA — không ai "sửa" hay "xóa"
        # một dòng snapshot; `create` là quyền xếp job tính lại.
        actions=frozenset({Action.VIEW, Action.CREATE}),
    )
)
