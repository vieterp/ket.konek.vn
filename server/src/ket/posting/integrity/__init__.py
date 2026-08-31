"""Bộ kiểm tra toàn vẹn sổ sách (FR-NFR-007, FR-GLE-032/033 — phase 4, lát 4D).

Import gói này (qua `ket.model_registry`, như mọi đường cấp dataset) là đăng
ký hai thứ, cùng khuôn với `posting/balances/__init__.py`:

* mã quyền `posting.integrity.view` (xem báo cáo chênh lệch) và
  `posting.integrity.create` (xếp job kiểm) — phải có mặt trước khi
  `provision_dataset` gieo bảng `permissions`;
* loại job `posting.integrity.check` vào registry hàng đợi — worker và API
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
from ket.posting.integrity import job as job

PERMISSION_REGISTRY.register(
    DocumentType(
        module="posting",
        code="integrity",
        # `view` + `create`: bộ kiểm toàn vẹn chỉ đọc và chỉ ra (phase-04
        # §Bộ kiểm tra toàn vẹn: "không tự sửa"). `edit` thêm ở lát 6G-2 cho
        # MỘT việc duy nhất — job `posting.dimensions.apply` ghi lại chiều
        # suy-ra đã lệch. Tách khỏi `create` có chủ đích (review 6G-2 H-1): vai
        # trò "soát sổ" đang được cấp `create` hôm nay KHÔNG được kèm theo
        # quyền ghi vào `gl_postings`.
        actions=frozenset({Action.VIEW, Action.CREATE, Action.EDIT}),
    )
)
