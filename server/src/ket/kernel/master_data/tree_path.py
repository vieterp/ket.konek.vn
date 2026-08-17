"""Cây danh mục bằng **materialized path** — `'1.4.17.'` (FR-SYS-011).

Danh mục kế toán là cây sâu ít nhất 5 cấp (hệ thống tài khoản, cơ cấu tổ chức,
nhóm vật tư) và được **đọc liên tục, sửa hiếm**: mỗi lần mở ô chọn tài khoản là
một lần đọc cả nhánh, còn việc chuyển một nhóm sang nhóm cha khác là việc của
kế toán trưởng vài lần một năm.

Ba cách lưu cây và lý do chọn cách này:

* **`parent_id` đệ quy** (`WITH RECURSIVE`): sửa rẻ nhất, nhưng mỗi lần đọc một
  nhánh là một phép duyệt đệ quy — và nhánh được đọc trên gần như mọi màn hình
  nhập liệu.
* **Nested set** (`lft`/`rgt`): đọc rẻ, nhưng **mọi** lần chèn một nút phải ghi
  lại chỉ số của một nửa cây. Trên danh mục vật tư vài chục nghìn dòng thì thêm
  một mã hàng khóa cả bảng.
* **Materialized path** (chọn): đọc nhánh là `path LIKE '1.4.%'` — một phép quét
  chỉ mục B-tree bình thường; chèn chỉ chạm đúng dòng mới; chuyển nhánh chạm
  đúng các dòng thật sự đổi chỗ, bằng **một** câu `UPDATE`.

`path` là **giá trị dẫn xuất** từ `parent_id`, nên nó chỉ được sinh ở module này
— không service nào tự ghép chuỗi path. Hai nguồn sinh path là hai cây sẽ lệch
nhau, và lệch ở đây nghĩa là báo cáo tổng hợp theo nhóm cộng thiếu một nhánh mà
không có gì báo lỗi.

Hàm ở đây **trả về câu SQL**, không tự chạy: đó là quy ước chung của repo cho
mọi chỗ phải ghép tên bảng vào SQL (tên bảng là identifier, không tham số hóa
được — xem `tests/test_no_sql_string_interpolation.py`).
"""

from __future__ import annotations

from typing import Final

from ket.kernel.security.rls import validate_identifier

PATH_SEPARATOR: Final[str] = "."

PATH_PATTERN: Final[str] = r"^([0-9]+\.)+$"
"""Biểu thức chính quy cho ràng buộc `CHECK` trên cột `path`.

Không phải để bắt lỗi gõ tay — không ai gõ cột này. Nó chốt **giả định** mà mọi
truy vấn cây dựa vào: path chỉ gồm chữ số và dấu chấm. Nhờ giả định đó, tiền tố
path ghép thẳng vào mẫu `LIKE` được mà không phải thoát `%` và `_`; nếu một ngày
nào đó có ai cho ký tự khác lọt vào cột này thì ràng buộc đổ ngay tại chỗ ghi,
chứ không lặng lẽ biến một phép so tiền tố thành một mẫu ký tự đại diện."""

ROOT_LEVEL: Final[int] = 1
"""Nút gốc ở cấp 1 — cấp đọc lên cho người dùng, không phải chỉ số mảng."""


def child_path(parent_path: str | None, node_id: int) -> str:
    """Path của một nút, biết path của cha và khóa chính của chính nó.

    Dấu chấm **cuối** không phải để cho đẹp: thiếu nó thì tiền tố `'1.4'` khớp
    luôn nút `'1.40'`, và một nhánh lạ lọt vào mọi báo cáo tổng hợp theo nhóm.
    """
    if node_id <= 0:
        raise ValueError(f"node_id phải dương, nhận {node_id}")
    prefix = parent_path or ""
    return f"{prefix}{node_id}{PATH_SEPARATOR}"


def level_of(path: str) -> int:
    """Cấp của nút = số đoạn trong path."""
    return path.count(PATH_SEPARATOR)


def like_prefix(path: str) -> str:
    """Mẫu `LIKE` lấy nút đó **và** toàn bộ con cháu.

    An toàn nhờ `PATH_PATTERN`: path không chứa `%` hay `_` nên không cần thoát.
    """
    return f"{path}%"


def is_descendant_of(candidate_path: str, ancestor_path: str) -> bool:
    """Nút này có nằm trong nhánh kia không (tính cả chính nó)."""
    return candidate_path.startswith(ancestor_path)


def move_subtree_sql(table: str) -> str:
    """Một câu `UPDATE` đổi chỗ cả nhánh (FR-SYS-011, tiêu chí phase 3).

    Tham số ràng buộc: `old_prefix`, `new_prefix`, `old_prefix_length`,
    `level_delta`, `old_prefix_pattern`.

    Vì sao ghép bằng `substring` chứ không `replace(path, cũ, mới)`: `substring`
    nói đúng thứ đang cần — **cắt đúng tiền tố ở đầu** — còn `replace` nói "thay
    ở bất kỳ đâu". Trên dữ liệu hợp lệ hai cách cho cùng kết quả (mọi id trên một
    đường từ gốc đều khác nhau, nên tiền tố không lặp lại được trong cùng một
    path), nên đây không phải một lỗ đang mở mà là chọn phép toán khớp với ý
    định: ngày nào đó `path` mang thêm thành phần không phải id, `replace` sẽ sai
    âm thầm còn `substring` thì không.

    `row_version` cũng tăng: `path` là dữ liệu dẫn xuất, nhưng một client đang
    giữ bản cũ của nút con **đã** cầm một giá trị lỗi thời, và để nó lưu đè im
    lặng là đúng thứ khóa lạc quan sinh ra để chặn (FR-NFR-005).
    """
    validate_identifier(table)
    # S608 (ghép chuỗi vào SQL): tên bảng là **identifier**, không tham số hóa
    # được, và đã qua whitelist ký tự của `validate_identifier`. Mọi *giá trị*
    # trong câu lệnh này đều là tham số ràng buộc — nhờ đó psycopg dùng extended
    # protocol và PostgreSQL từ chối nhiều câu lệnh trong một lần gửi (lý do đầy
    # đủ ở `tests/test_no_sql_string_interpolation.py`).
    return (
        f"UPDATE {table} "  # noqa: S608
        "SET path = :new_prefix || substring(path FROM :old_prefix_length + 1), "
        "    level = level + :level_delta, "
        "    row_version = row_version + 1 "
        "WHERE path LIKE :old_prefix_pattern"
    )


def move_subtree_params(*, old_path: str, new_path: str) -> dict[str, str | int]:
    """Tham số cho `move_subtree_sql`, dựng từ path cũ và path mới của nút gốc nhánh."""
    return {
        "old_prefix": old_path,
        "new_prefix": new_path,
        "old_prefix_length": len(old_path),
        "level_delta": level_of(new_path) - level_of(old_path),
        "old_prefix_pattern": like_prefix(old_path),
    }
