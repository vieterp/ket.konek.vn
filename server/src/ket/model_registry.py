"""Điểm nhập duy nhất nạp **mọi** model ORM — cả hai gốc khai báo.

Vì sao ở gốc gói chứ không trong `kernel`: từ phase 4 trở đi model còn nằm ở
`ket.posting` và `ket.modules.*`, mà `kernel` **không được** import hai chỗ
đó (luật phụ thuộc #5, contract C1 của import-linter). Một module gom ở gốc gói
không thuộc tầng nào nên nạp được tất cả mà không phá luật.

Hai bên đọc module này, và cả hai đều hỏng **âm thầm** nếu thiếu một dòng import:

* `migrations/env.py` → `DatasetBase.metadata`. Model dataset vắng mặt ở đây sẽ
  vắng mặt trong autogenerate — Alembic sinh lệnh xóa bảng đó, hoặc lặng lẽ bỏ
  qua bảng mới.
* `kernel/datasets/bootstrap.py` → `ControlBase.metadata.create_all`. Bảng điều
  khiển vắng mặt ở đây sẽ **không được tạo** trên cụm mới, và lỗi chỉ lộ ra ở
  lần `INSERT` đầu tiên, thường là giữa luồng đăng nhập.

Thêm model mới = thêm một dòng ở đây.

Alembic không bị nhiễu bởi model điều khiển: `env.py` chỉ lấy
`DatasetBase.metadata` và chạy với `include_schemas=False`.
"""

from __future__ import annotations

from ket.kernel.auditing import control_log as control_audit_models
from ket.kernel.auditing import models as auditing_models
from ket.kernel.datasets import models as control_models
from ket.kernel.idempotency import models as idempotency_models
from ket.kernel.jobs import models as jobs_models
from ket.kernel.numbering import models as numbering_models
from ket.kernel.persistence.base import ControlBase, DatasetBase
from ket.kernel.security import auth_models
from ket.kernel.security import models as security_models

__all__ = [
    "ControlBase",
    "DatasetBase",
    "auditing_models",
    "auth_models",
    "control_audit_models",
    "control_models",
    "idempotency_models",
    "jobs_models",
    "numbering_models",
    "security_models",
]
