"""Cô lập chi nhánh bằng Row Level Security của PostgreSQL (RT-04, FR-SYS-072).

Vì sao ở DB chứ không ở tầng ứng dụng: một `WHERE branch_id IN (...)` quên ở
**một** truy vấn báo cáo là đủ rò dữ liệu chi nhánh khác, và hàm cửa sổ kiểu
`SUM() OVER ()` còn rò cả số tổng mà không rò dòng nào — nhìn qua tưởng an
toàn. RLS đặt hàng rào ở nơi mọi truy vấn đều phải đi qua, kể cả SQL text của
report engine (phase 5). Bộ lọc ở tầng ứng dụng chỉ còn là **phòng thủ lớp
hai**.

Điều kiện cần để RLS có tác dụng: runtime đăng nhập bằng `ket_app` — **không
sở hữu bảng, không `BYPASSRLS`, không superuser**. Chủ sở hữu bảng tự bỏ qua
policy của chính mình (trừ khi bật `FORCE ROW LEVEL SECURITY`), nên tách vai
trò là phần không thể thiếu của cơ chế này, không phải "làm thêm cho chắc".
"""

from __future__ import annotations

import re
from typing import Final

from ket.kernel.datasets.naming import validate_schema_name

BRANCH_SCOPE_GUC: Final[str] = "ket.branch_ids"
"""GUC chứa danh sách chi nhánh mà request hiện tại được xem, dạng "1,4,7".

Đặt bằng `set_config(..., is_local => true)` nên nó **thuộc về transaction**:
hết transaction là hết hiệu lực, không rò sang request kế tiếp dùng chung
connection trong pool.
"""

POLICY_NAME: Final[str] = "p_branch_scope"


def branch_scope_predicate(branch_column: str, *, allow_null_branch: bool) -> str:
    """Biểu thức SQL cho policy RLS.

    `nullif(..., '')` là phần quan trọng nhất: khi GUC **chưa được đặt**,
    `current_setting(..., true)` trả NULL và cả biểu thức thành NULL → không
    dòng nào lọt. Fail-closed. Nếu viết `string_to_array('' , ',')` thì kết quả
    là mảng một phần tử rỗng và phép ép sang `int[]` nổ lỗi giữa câu truy vấn —
    tệ hơn, nếu ai đó "sửa" bằng `coalesce(..., '{}')` thì mất luôn tính
    fail-closed.
    """
    validate_identifier(branch_column)
    membership = (
        f"{branch_column} = ANY ("
        f"string_to_array(nullif(current_setting('{BRANCH_SCOPE_GUC}', true), ''), ',')::int[]"
        f")"
    )
    if allow_null_branch:
        # Bản ghi không thuộc chi nhánh nào (sự kiện mức hệ thống: đăng nhập,
        # tạo dữ liệu kế toán, đổi cấu hình chung). Không cho NULL đi qua thì
        # các sự kiện này vô hình với **mọi** người dùng, kể cả quản trị.
        return f"({branch_column} IS NULL OR {membership})"
    return membership


_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z_][a-z0-9_]*")


def validate_identifier(identifier: str) -> None:
    """Chặn identifier lạ trước khi ghép vào DDL.

    Tên bảng/cột ở đây do **mã nguồn migration** truyền vào, không phải người
    dùng — nhưng hàm sinh SQL thì không biết điều đó, và một ngày nào đó sẽ có
    người gọi nó bằng biến.

    Dùng regex ASCII chứ không phải `str.isalnum()`: `isalnum()` chấp nhận chữ
    Unicode, nên `"café"` hay `"ｄｒｏｐ"` lọt qua và tạo ra identifier mà
    PostgreSQL đòi trích dẫn. Cùng bộ luật với `datasets/naming.py` — hai bộ
    luật cho cùng một khái niệm là cách sinh ra lỗ hổng ở bộ lỏng hơn.
    """
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"Identifier không hợp lệ: {identifier!r}")


def enable_branch_rls_statements(
    table: str,
    *,
    branch_column: str = "branch_id",
    allow_null_branch: bool = False,
) -> tuple[str, ...]:
    """Sinh DDL bật RLS + policy chi nhánh cho một bảng trong schema hiện hành.

    Dùng trong migration: `for sql in enable_branch_rls_statements("audit_log"): op.execute(sql)`.

    Policy áp cho `ALL` (SELECT/INSERT/UPDATE/DELETE) với cùng biểu thức ở
    `USING` và `WITH CHECK`: đọc thì không thấy chi nhánh khác, mà ghi cũng
    không **tạo** được bản ghi cho chi nhánh khác.
    """
    validate_identifier(table)
    predicate = branch_scope_predicate(branch_column, allow_null_branch=allow_null_branch)
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}",
        (
            f"CREATE POLICY {POLICY_NAME} ON {table} "
            f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})"
        ),
    )


def set_search_path_statement(schema: str) -> str:
    """`SET LOCAL search_path` cho một transaction.

    Tên schema là identifier nên không tham số hóa được — đây là lý do
    `validate_schema_name` tồn tại và là chỗ duy nhất được phép ghép nó vào SQL.
    Giữ `public` ở cuối để các extension cài trong `public` vẫn dùng được; bảng
    dataset luôn đứng trước nên không có chuyện truy vấn trúng nhầm bảng điều
    khiển (`tests/test_dataset_routing.py` khóa bất biến "không trùng tên bảng").

    **`pg_temp` phải nêu tường minh ở CUỐI.** Khi không nêu, PostgreSQL tìm nó
    **đầu tiên** — trước cả schema dataset. Vai trò runtime tạo được bảng tạm
    (quyền `TEMP` mặc định thuộc PUBLIC), nên một `CREATE TEMP TABLE audit_log
    (…)` sẽ khiến mọi `INSERT INTO audit_log` sau đó rơi vào bảng tạm còn ghi
    nghiệp vụ vẫn vào bảng thật: nhật ký bất biến bị vô hiệu **mà không cần
    UPDATE hay DELETE dòng nào**. Cùng thủ thuật che được `branches`,
    `settings`, `idempotency_keys`.

    Đây là phòng thủ lớp một; `roles.sql` thu hồi luôn quyền `TEMPORARY` của
    PUBLIC là lớp hai.
    """
    return f'SET LOCAL search_path TO "{validate_schema_name(schema)}", public, pg_temp'
