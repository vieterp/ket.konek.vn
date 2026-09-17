"""Bốn báo cáo hóa đơn điện tử + dải số hóa đơn — lát 7G-3.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-17

Chạy **một lần cho mỗi schema dataset** như `0001`..`0041`, bằng `ket_owner`.
Revision này **không đổi schema** — lát 7G-3 đọc nguyên những bảng 7D/7E/7F đã
dựng (`einvoices`, `invoice_registrations`, `invoice_forms`) và không thêm cột
nào.

**`_refresh_builtin_data` CHUYỂN TỪ `0041` SANG ĐÂY** — doctrine 5B M-1, lần thứ
bảy của dự án (6A, 6G-1, 7A, 7G-1, 7G-2a, 7G-2b, và lát này). Bước ấy chạy thử
SQL của manifest HÔM NAY trên schema của revision nó đứng, nên nó phải đậu ở
**head**: bản cài đang ở `0041` đã đi qua bước làm mới **trước khi** manifest có
bốn báo cáo này, và không dời lên head thì màn hình báo cáo hóa đơn điện tử trống
trên đúng những bản cài đang chạy.

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

revision: str = "0042"
down_revision: str | None = "0041"
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
    # Thứ tự bắt buộc, và đó là lý do hai bước này đi CÙNG NHAU: bước làm mới gieo
    # lại definition builtin theo manifest hôm nay (không còn cái nào trỏ
    # `ar_ap_aging`), rồi lượt dọn mới xóa được — khóa ngoại là `RESTRICT`, nên
    # dọn trước là dọn hụt, im lặng.
    _refresh_builtin_data()
    _drop_retired_metadata()


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — lát này gieo bốn định nghĩa
    hóa đơn điện tử cùng ba dataset của chúng.

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
    """Dọn dataset + bộ tham số đã rời manifest, khi không còn ai trỏ tới.

    **Chuyển từ `0041` sang đây cùng lượt với bước làm mới**, và nó KHÔNG tách rời
    được: guard "không định nghĩa nào trỏ tới" chỉ đúng SAU khi bước làm mới đã
    gieo lại definition. Để nó ở `0041` trong khi bước làm mới lên `0042` thì trên
    bản cài nâng cấp, ba định nghĩa tuổi nợ cũ vẫn còn trỏ `ar_ap_aging` lúc `0041`
    chạy — guard chặn, dòng ở lại, và không gì kêu.
    """
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
