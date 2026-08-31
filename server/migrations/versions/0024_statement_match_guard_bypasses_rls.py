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
* `SET search_path` ghim vào ĐÚNG schema của dataset. Đây là phần bắt buộc của
  `SECURITY DEFINER`: search_path thả nổi cho phép người gọi trỏ tên bảng sang
  một schema họ tạo, và hàm sẽ đọc bảng ấy với quyền chủ sở hữu.
* Chủ sở hữu là `ket_owner` — chủ các bảng, và không bảng nào bật
  `FORCE ROW LEVEL SECURITY`, nên chủ bảng vốn đã ngoài RLS.

Quyền EXECUTE để mặc định (PostgreSQL cấp cho `PUBLIC`): cố ý. Cấp tay cho vai
trò từng dataset sẽ là **bản chép thứ hai** của danh sách quyền, mà
`dataset_roles.ensure_cluster` là bản thứ nhất — đúng lỗi mà docstring
`rebuild_dataset_grants` kể lại ("danh sách chép làm hai bản… không test nào
đỏ"). Bề mặt hàm này hẹp tới mức ai gọi được cũng không đọc thêm được gì.

Bước `_refresh_builtin_data` **GIỮ ở 0023**: migration này không thêm cột nào
mà dataset báo cáo builtin đọc, nên nó không phải là "migration cuối chuỗi" theo
nghĩa của doctrine 6B (cùng lối `0018`/`0019` giữ bước ấy ở `0017`).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Không ghép chuỗi nào — kể cả tên schema.

    `migrations/env.py` đã `SET search_path` vào schema dataset trước khi chạy
    revision, nên `CREATE FUNCTION` không cần schema-qualify, và
    **`SET search_path FROM CURRENT`** ghim đúng giá trị ấy vào thân hàm lúc
    tạo. Bản đầu nội suy tên schema hai chỗ và phải xin miễn trừ ở
    `test_no_sql_string_interpolation`; miễn trừ theo TỆP đặt cả tệp ra ngoài
    tầm quét vĩnh viễn (xem docstring `ALLOWED_FILES`), nên không lấy khi còn
    đường tránh.
    """
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
