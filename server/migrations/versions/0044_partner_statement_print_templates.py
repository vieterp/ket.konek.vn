"""Ba mẫu in văn bản gửi đối tác — lát 7G-5.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-17

Chạy **một lần cho mỗi schema dataset** như `0001`..`0043`, bằng `ket_owner`.

Revision này **không đổi schema**. Lát 7G-5 thêm ba mẫu in builtin (biên bản
đối chiếu công nợ hai chiều + thông báo công nợ) vào `builtin_print_templates.json`;
bản in tính tại chỗ từ dataset công nợ, không bảng nào mới.

**`_refresh_builtin_data` CHUYỂN TỪ `0043` SANG ĐÂY** — doctrine 5B M-1, lần thứ
chín của dự án. Bản cài đang ở `0043` đã đi qua bước gieo mẫu in **trước khi**
manifest có ba mẫu này, và không dời lên head thì `GET /print-templates` của họ
không có mẫu nào cho ba văn bản — 404 ngay lượt in đầu. Lượt dọn `ar_ap_aging`
đi theo vì không tách rời được (xem docstring `0041`).

Luật giữ nguyên: **đúng một lượt gọi, ở head**. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op

from ket.kernel.config.printing.seed import ensure_builtin_print_templates
from ket.kernel.config.reports.seed import (
    drop_retired_builtin_metadata,
    refresh_builtin_reports,
)
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE

_RETIRED_DATASET = "ar_ap_aging"
_RETIRED_PARAM_SET = "ar_ap_aging_params"

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    _refresh_builtin_data()
    _drop_retired_metadata()


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — lát này gieo ba mẫu in
    văn bản gửi đối tác.

    Chỉ chạy online: bước đọc-rồi-ghi không diễn đạt được thành SQL tĩnh của
    `upgrade --sql`. Vị trí của bước này phải luôn ở **head** của chuỗi, và chỉ
    có **một** lượt gọi trong cả chuỗi — xem docstring đầu tệp.
    """
    if context.is_offline_mode():
        return
    schema = _target_schema()
    connection = op.get_bind()
    refresh_builtin_reports(connection, schema)
    ensure_builtin_print_templates(connection, schema)


def _drop_retired_metadata() -> None:
    """Dọn dataset + bộ tham số đã rời manifest (7G-2b), khi không còn ai trỏ
    tới — đi cùng bước làm mới, xem docstring đầu tệp và `0041`."""
    if context.is_offline_mode():
        return
    drop_retired_builtin_metadata(
        op.get_bind(),
        _target_schema(),
        dataset_codes=(_RETIRED_DATASET,),
        param_set_codes=(_RETIRED_PARAM_SET,),
    )


def downgrade() -> None:
    """Không có gì để hoàn: revision này không đổi schema, và metadata builtin
    được gieo lại từ manifest của bản phát hành tương ứng."""
