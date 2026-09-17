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
Cổng `test_no_retired_metadata_outlives_the_manifest` là chuông báo cho lần sau.

**Xóa CÓ ĐIỀU KIỆN, không xóa trần.** `report_definitions.dataset_code` là khóa
ngoại `ondelete=RESTRICT`, nên một definition **không-builtin** (người dùng tự
đăng ký) trỏ vào `ar_ap_aging` sẽ làm lượt xóa đổ giữa chuỗi migration. Trạng
thái ấy hợp lệ: báo cáo riêng của người dùng phải tiếp tục chạy. Nên điều kiện
"không còn ai trỏ tới" là phép canh, và dòng ở lại là kết cục ĐÚNG khi có người
trỏ tới — không phải một lỗi cần dừng bản nâng cấp.

**`_refresh_builtin_data` và lượt dọn TỪNG Ở ĐÂY** (lát 7G-2b), rồi **cả hai cùng
dời sang `0042`** ở lát 7G-3 — doctrine 5B M-1 đòi bước làm mới đậu ở **head** của
chuỗi, vì nó chạy thử SQL của manifest HÔM NAY trên schema của revision nó đứng.

Hai bước ấy **không tách rời được**, và đó là lý do lượt dọn đi theo: guard "không
định nghĩa nào trỏ tới" chỉ đúng SAU khi bước làm mới đã gieo lại definition. Để
lượt dọn ở lại đây trong khi bước làm mới lên `0042` thì trên bản cài nâng cấp, ba
định nghĩa tuổi nợ cũ vẫn còn trỏ `ar_ap_aging` lúc revision này chạy — guard chặn,
dòng mồ côi ở lại vĩnh viễn, và không gì kêu.

Luật giữ nguyên: **đúng một lượt gọi, ở head**. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở đây.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Không còn việc gì ở revision này — **có chủ đích**.

    Lát 7G-2b đặt ở đây hai bước: làm mới metadata builtin, rồi dọn hai dòng đã
    rời manifest. Lát 7G-3 dời bước làm mới lên head (`0042`) theo doctrine 5B
    M-1, và lượt dọn **phải đi theo**: guard "không định nghĩa nào trỏ tới" chỉ
    đúng sau khi bước làm mới đã gieo lại definition, nên tách hai bước ra hai
    revision là dọn hụt trên mọi bản cài nâng cấp — và hụt im lặng.

    Revision giữ chỗ thay vì bị xóa: bản cài nào đã chạy `0041` thì đã dọn xong
    rồi, và một mã revision biến mất khỏi chuỗi là một bản cài không nâng cấp
    tiếp được.
    """


def downgrade() -> None:
    """Không có gì để hoàn: revision này không đổi schema, và hai dòng metadata
    đã xóa được gieo lại từ manifest của bản phát hành tương ứng — quay về bản
    trước nghĩa là quay về manifest có `ar_ap_aging`, nên chính bước làm mới của
    revision cũ dựng lại chúng."""
