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

from ket.kernel.attachments import models as attachment_models
from ket.kernel.auditing import control_log as control_audit_models
from ket.kernel.auditing import models as auditing_models
from ket.kernel.bank_import import profile_models as bank_import_models
from ket.kernel.config import accounts_models
from ket.kernel.config.reports import models as report_models
from ket.kernel.config.statements import models as statement_models
from ket.kernel.currency import models as currency_models
from ket.kernel.datasets import models as control_models
from ket.kernel.dimensions import models as dimension_models
from ket.kernel.excel import models as excel_models
from ket.kernel.idempotency import models as idempotency_models
from ket.kernel.jobs import models as jobs_models
from ket.kernel.master_data import models as master_data_models
from ket.kernel.master_data import usage as master_data_usage_models
from ket.kernel.numbering import models as numbering_models
from ket.kernel.periods import models as period_models
from ket.kernel.persistence.base import ControlBase, DatasetBase
from ket.kernel.security import auth_models
from ket.kernel.security import models as security_models
from ket.modules.general_ledger.journal import models as gl_journal_models

# Hai gói không có model — import để đăng ký mã quyền (`posting.period.*`,
# `posting.integrity.*`) và loại job trước khi `provision_dataset` gieo bảng
# `permissions` và trước khi worker nhận job. Cùng cơ chế với việc import
# `balance_models` kéo theo `ket.posting.balances.__init__` ở dưới.
from ket.posting import integrity as integrity_registration
from ket.posting import periods as periods_registration
from ket.posting.balances import models as balance_models
from ket.posting.documents import models as voucher_models
from ket.posting.engine import models as gl_posting_models
from ket.posting.opening_balances import models as opening_balance_models

# Không có model — import để đăng ký mã quyền `reporting.statement.view` /
# `reporting.report.*` trước khi `provision_dataset` gieo bảng `permissions`
# (cùng cơ chế `posting.periods`).
from ket.reporting import engine as report_permission_registration
from ket.reporting import statements as statement_permission_registration

__all__ = [
    "ControlBase",
    "DatasetBase",
    "accounts_models",
    "attachment_models",
    "auditing_models",
    "auth_models",
    "balance_models",
    "bank_import_models",
    "control_audit_models",
    "control_models",
    "currency_models",
    "dimension_models",
    "excel_models",
    "gl_journal_models",
    "gl_posting_models",
    "idempotency_models",
    "integrity_registration",
    "jobs_models",
    "master_data_models",
    "master_data_usage_models",
    "numbering_models",
    "opening_balance_models",
    "period_models",
    "periods_registration",
    "report_models",
    "report_permission_registration",
    "security_models",
    "statement_models",
    "statement_permission_registration",
    "voucher_models",
]
