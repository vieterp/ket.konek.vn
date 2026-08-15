"""Điểm nhập duy nhất nạp **mọi** model của schema dataset cho Alembic.

Vì sao ở gốc gói chứ không trong `kernel`: từ phase 4 trở đi model còn nằm ở
`ket.posting` và `ket.modules.*`, mà `kernel` **không được** import hai chỗ
đó (luật phụ thuộc #5, contract C1 của import-linter). Một module gom ở gốc gói
không thuộc tầng nào nên nạp được tất cả mà không phá luật.

`migrations/env.py` import module này. Model nào không được import ở đây sẽ
**vắng mặt trong autogenerate** — Alembic sẽ sinh migration xóa bảng đó, hoặc
lặng lẽ bỏ qua bảng mới. Thêm model mới = thêm một dòng ở đây.
"""

from __future__ import annotations

from ket.kernel.auditing import models as auditing_models
from ket.kernel.idempotency import models as idempotency_models
from ket.kernel.jobs import models as jobs_models
from ket.kernel.numbering import models as numbering_models
from ket.kernel.persistence.base import ControlBase, DatasetBase
from ket.kernel.security import models as security_models

__all__ = [
    "ControlBase",
    "DatasetBase",
    "auditing_models",
    "idempotency_models",
    "jobs_models",
    "numbering_models",
    "security_models",
]
