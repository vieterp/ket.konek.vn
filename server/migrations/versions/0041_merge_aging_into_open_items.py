"""Gộp dataset tuổi nợ vào dataset công nợ chi tiết + báo cáo phải thu — lát 7G-2b.

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-15

Chạy **một lần cho mỗi schema dataset** như `0001`..`0040`, bằng `ket_owner`.
Revision này **không đổi schema** — nó chỉ dọn hai dòng metadata và làm mới phần
builtin.

**Vì sao cần một revision cho một lượt dọn.** `refresh_builtin_reports` xóa mọi
`report_definitions` builtin rồi gieo lại theo manifest, nhưng với dataset /
layout / param set nó chỉ `UPDATE` **theo mã còn trong manifest**. Mã nào rời
manifest thì không đường nào chạm tới nữa: dòng ấy ở lại bảng, mang SQL của bản
phát hành cũ, vô hình vì không definition nào trỏ tới. Lát này rút `ar_ap_aging`
vào `ar_ap_open_items`, nên nếu không dọn tay thì mọi bản cài đang chạy giữ một
`report_datasets('ar_ap_aging')` chết cùng `report_param_sets('ar_ap_aging_params')`
— và ngày ai đó đăng ký một báo cáo trỏ vào mã ấy, họ nhận SQL của tháng trước.
Cổng `test_no_builtin_report_metadata_outlives_the_manifest` là chuông báo cho
lần sau.

**Xóa CÓ ĐIỀU KIỆN, không xóa trần.** `report_definitions.dataset_code` là khóa
ngoại `ondelete=RESTRICT`, nên một definition **không-builtin** (người dùng tự
đăng ký) trỏ vào `ar_ap_aging` sẽ làm lượt xóa đổ giữa chuỗi migration. Trạng
thái ấy hợp lệ: báo cáo riêng của người dùng phải tiếp tục chạy. Nên điều kiện
"không còn ai trỏ tới" là phép canh, và dòng ở lại là kết cục ĐÚNG khi có người
trỏ tới — không phải một lỗi cần dừng bản nâng cấp.

**`_refresh_builtin_data` CHUYỂN TỪ `0040` SANG ĐÂY** — doctrine 5B M-1, lần thứ
sáu của dự án (6A, 6G-1, 7A, 7G-1, 7G-2a, và lát này). Bản cài đang ở `0040` đã
đi qua bước làm mới **trước khi** manifest có chín báo cáo phải thu, và trước khi
ba định nghĩa tuổi nợ đổi sang dataset mới — không dời lên **head** thì chúng giữ
ba báo cáo tuổi nợ trỏ vào một dataset đã rời manifest, còn màn hình công nợ phải
thu thì trống.

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

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETIRED_DATASET = "ar_ap_aging"
_RETIRED_PARAM_SET = "ar_ap_aging_params"


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    # Thứ tự bắt buộc: làm mới TRƯỚC, dọn SAU. Bước làm mới gieo lại ba định
    # nghĩa tuổi nợ trỏ sang `ar_ap_open_items`; dọn trước thì chính chúng còn
    # đang trỏ vào dòng sắp xóa và khóa ngoại `RESTRICT` chặn lại.
    _refresh_builtin_data()
    _drop_retired_metadata()


def _refresh_builtin_data() -> None:
    """Làm mới metadata báo cáo + mẫu in builtin — lát này đổi nguồn ba báo cáo
    tuổi nợ và gieo chín định nghĩa công nợ phải thu.

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

    Phép canh và phép xóa nằm ở `drop_retired_builtin_metadata` để có bài kiểm
    chạm tới được: lượt xóa của một migration chỉ chạy trên bản cài NÂNG CẤP, còn
    schema dựng mới trong test đi thẳng tới head nên nó không bao giờ có dòng để
    xóa — một lượt xóa không bài kiểm nào chạy qua là một lượt xóa không ai biết
    nó có chạy không.
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
    """Không có gì để hoàn: revision này không đổi schema, và hai dòng metadata
    đã xóa được gieo lại từ manifest của bản phát hành tương ứng — quay về bản
    trước nghĩa là quay về manifest có `ar_ap_aging`, nên chính bước làm mới của
    revision cũ dựng lại chúng."""
