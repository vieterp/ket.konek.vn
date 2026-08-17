"""Chuẩn bị connection cho một hàm gieo mầm chạy trong schema dataset.

Gieo mầm (`role_service.ensure_admin_role`, `dimensions.seed`) có ba đặc điểm
chung, và cả ba đều dễ quên khi viết hàm gieo thứ hai:

1. Nó chạy bằng `ket_owner`, không `SET ROLE` — lúc dữ liệu kế toán vừa được
   tạo thì đó là vai trò duy nhất có quyền.
2. Nó là **đọc-rồi-ghi** (`SELECT` cái đã có → `INSERT` cái còn thiếu), nên hai
   lời gọi song song phải nối tiếp nhau. Đường song song có thật:
   `provision_dataset` chạy trong lúc ai đó gọi `ket.admin ensure-cluster`. Đo
   được trên schema trống: 4 lời gọi song song → 3 lỗi `UniqueViolation`.
3. Khóa phải giữ tới **hết transaction**, không phải hết câu lệnh: một cụm gieo
   chạm nhiều bảng (`permissions` → `roles` → `role_permissions`) và chúng phải
   nhất quán với nhau. `ON CONFLICT DO NOTHING` chỉ vá được từng câu lệnh.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from ket.kernel.security.rls import set_search_path_statement


def bind_seed_schema(connection: Connection, schema: str) -> None:
    """Trỏ `search_path` vào schema dataset và giữ khóa gieo mầm tới hết transaction.

    Dùng lại `rls.set_search_path_statement` chứ không tự ghép câu lệnh: nó là
    **chỗ duy nhất** được phép nối tên schema vào SQL, và nó mang theo hai thứ dễ
    quên — kiểm tên schema, và `pg_temp` nêu tường minh ở cuối để một bảng tạm
    không che được bảng thật.

    Gọi lại nhiều lần trong cùng transaction là an toàn: khóa cố vấn cấp
    transaction có thể lấy lại bởi chính phiên đang giữ nó. Nhờ vậy nhiều hàm
    gieo chạy nối nhau trong một `engine.begin()` mà không hàm nào phải biết
    hàm nào đã chạy trước.
    """
    connection.exec_driver_sql(set_search_path_statement(schema))
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": schema})
