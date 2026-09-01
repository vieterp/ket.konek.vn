"""Hàm đếm dòng sao kê đã khớp, chạy NGOÀI RLS — lát 6G-2 vòng review (H-3).

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-31

Chạy **một lần cho mỗi schema dataset** như `0001`..`0023`, bằng `ket_owner`.

Vấn đề: guard "chứng từ đã khớp sao kê thì không bỏ ghi sổ" (6D H-3) đọc
`bank_statement_lines` **dưới RLS của người gọi**. Từ migration 0022 bảng ấy
mang `branch_id` = chi nhánh của TÀI KHOẢN ngân hàng, nên người bỏ ghi sổ ở
chi nhánh B **không nhìn thấy** dòng sao kê của tài khoản chi nhánh A — guard
im lặng cho qua, và dòng sao kê "đã khớp" ở lại trỏ một phiếu nháp sửa được số
tiền. Đây là mẫu "phép kiểm chạy dưới phạm vi nào, và nó có nhìn thấy thứ nó
đang đi tìm không?" — lần thứ ba của dự án (6C H-1, 3B-2 B-8).

Ba cách vá được cân nhắc (quyết định user 2026-08-31): cổng phạm vi công ty cho
cửa bỏ ghi sổ (chặn kế toán chi nhánh làm việc hằng ngày vì một ca hiếm),
trigger DB (hợp doctrine nhất nhưng dự án chưa có trigger nghiệp vụ nào), hoặc
**hàm đếm chạy ngoài RLS** — chọn cách này.

Vì sao `SECURITY DEFINER` an toàn ở đây, đủ hẹp để không mở cửa nào khác:

* Hàm trả về **một boolean** về **một** `voucher_id` truyền vào. Nó không trả
  dữ liệu sao kê, không nhận điều kiện lọc tự do, không ghi gì.
* `SET search_path FROM CURRENT` ghim search_path của phiên **lúc tạo hàm** vào
  chính hàm. Đây là phần bắt buộc của `SECURITY DEFINER`: search_path thả nổi
  cho phép người gọi trỏ tên bảng sang một schema họ tạo, và hàm sẽ đọc bảng ấy
  với quyền chủ sở hữu.

  `upgrade()` vì thế **tự đặt** search_path bằng `set_search_path_statement`
  ngay trước `CREATE FUNCTION`, không tin vào giá trị sẵn có trên connection
  (review pre-landing 6G-2 M-2). Hình dạng đúng là `<schema>, public, pg_temp`
  — **`pg_temp` nêu tường minh ở CUỐI**; không nêu thì PostgreSQL tìm nó TRƯỚC
  schema dataset, và một `CREATE TEMP TABLE bank_statement_lines(…)` rỗng sẽ
  làm guard này trả `false` vĩnh viễn (luật đầy đủ ở `kernel/security/rls.py`).
  Bản đầu ghim đúng giá trị ấy hoàn toàn do TÌNH CỜ: nó thừa hưởng `SET LOCAL`
  mà bước gieo dữ liệu của 0023 để lại trên cùng connection — một thuộc tính an
  toàn không được phép phụ thuộc vào một revision khác.
* Chủ sở hữu là `ket_owner` — chủ các bảng, và không bảng nào bật
  `FORCE ROW LEVEL SECURITY`, nên chủ bảng vốn đã ngoài RLS.

Quyền EXECUTE để mặc định (PostgreSQL cấp cho `PUBLIC`): cố ý. Cấp tay cho vai
trò từng dataset sẽ là **bản chép thứ hai** của danh sách quyền, mà
`dataset_roles.ensure_cluster` là bản thứ nhất — đúng lỗi mà docstring
`rebuild_dataset_grants` kể lại ("danh sách chép làm hai bản… không test nào
đỏ"). Bề mặt hàm này hẹp tới mức ai gọi được cũng không đọc thêm được gì.

Bước `_refresh_builtin_data` KHÔNG ở revision này: migration này không thêm cột
nào mà dataset báo cáo builtin đọc, nên nó không phải là "migration cuối chuỗi"
theo nghĩa của doctrine 6B (cùng lối `0018`/`0019` giữ bước ấy ở `0017`). Lúc
0024 được viết bước ấy nằm ở 0023; lát 7A đã dời nó xuống **`0025`** vì 0025
thêm bảng `ar_ap_ledger` mà dataset `ar_ap_aging` đọc. Tra chỗ hiện tại của nó
bằng `grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con
số trong một docstring cũ — nó đi theo đuôi chuỗi.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op

from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.rls import set_search_path_statement

revision: str = "0024"
down_revision: str | None = "0023"
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
    """Không ghép chuỗi nào ở tệp này — kể cả tên schema.

    `set_search_path_statement` là hàm SẴN CÓ của kernel, tự gọi
    `validate_schema_name` bên trong — nó *trả về* chuỗi DDL chứ không tự chạy,
    nên `test_no_sql_string_interpolation` không đụng tới (bộ quét chỉ bắt
    `text()`/`execute()` NHẬN f-string). `CREATE FUNCTION` sau đó không cần
    schema-qualify vì search_path đã trỏ đúng chỗ.

    Bản đầu nội suy tên schema hai chỗ và phải xin miễn trừ; miễn trừ theo TỆP
    đặt cả tệp ra ngoài tầm quét vĩnh viễn (xem docstring `ALLOWED_FILES`), nên
    không lấy khi còn đường tránh.
    """
    # Đặt search_path TƯỜNG MINH trước khi tạo hàm — `FROM CURRENT` chỉ chụp
    # lại thứ đang có, nên "thứ đang có" phải do chính revision này quyết định.
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "CREATE OR REPLACE FUNCTION voucher_has_matched_statement_line(p_voucher_id uuid)"
        " RETURNS boolean"
        " LANGUAGE sql"
        " STABLE"
        " SECURITY DEFINER"
        " SET search_path FROM CURRENT"
        " AS $$"
        "     SELECT EXISTS ("
        "         SELECT 1 FROM bank_statement_lines"
        "         WHERE matched_voucher_id = p_voucher_id"
        "     )"
        " $$"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS voucher_has_matched_statement_line(uuid)")
