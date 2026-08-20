"""Ghép câu SQL cuối cùng của một báo cáo: bọc phạm vi + `ORDER BY` (BR-RPT-04/05).

Ở kernel chứ không ở `reporting.engine` vì seed (kernel) phải **chạy thử** đúng
câu SQL mà engine sẽ chạy (probe `WHERE FALSE`, xem `seed.py`) — kiểm một bản
sao của câu lệnh là kiểm cái bóng, và C1 cấm kernel import reporting.

Ba tầng chống trộn dữ liệu vào SQL, không tầng nào là nối chuỗi tự do:

1. **Giá trị** đi qua bind (`:from_date`, …) — không bao giờ nằm trong chuỗi.
2. **Identifier** (cột `ORDER BY`) lấy từ `LayoutSpec.sort` — đã ràng
   `^[a-z][a-z0-9_]*$` lúc parse, và ở đây ràng lại lần nữa (phòng đường gọi
   nào đó không đi qua parser).
3. **Cô lập thật nằm ở RLS** trên bảng gốc (RT-04) — lớp bọc `branch_id`/
   `ledger` chỉ là phòng thủ lớp hai để người viết SQL dataset không thể *quên*
   phạm vi.
"""

from __future__ import annotations

import re
from typing import Final

from ket.kernel.config.reports.models import ReportDataset
from ket.kernel.config.reports.spec import LayoutSpec
from ket.kernel.errors import ReportDatasetInvalidError

_IDENTIFIER_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")

_PLACEHOLDER_RE: Final = re.compile(r"(?<![:\w']):([A-Za-z_][A-Za-z0-9_]*)")
"""Placeholder kiểu SQLAlchemy `text()`. `(?<![:\\w'])` loại `::int` (ép kiểu
PostgreSQL) và `':x'` trong chuỗi ký tự — dataset là SQL biên soạn có kiểm
soát, không phải văn bản tùy ý, nên regex mức này là đủ và seed còn chạy thử
câu lệnh thật để chốt."""


def sql_placeholders(sql_text: str) -> frozenset[str]:
    """Mọi placeholder có tên xuất hiện trong một câu SQL dataset."""
    return frozenset(_PLACEHOLDER_RE.findall(sql_text))


def compose_scoped_query(dataset: ReportDataset, layout_spec: LayoutSpec) -> str:
    """Câu SQL cuối cùng mà engine chạy (và seed chạy thử).

    `branch_ids IS NULL` = không thu hẹp thêm — RLS đã lọc những chi nhánh
    người gọi không thấy; truyền danh sách là xem MỘT phần trong phạm vi đó.
    Cộng đa chi nhánh theo **dòng phát sinh** (BR-RPT-05): mỗi dòng thuộc đúng
    một chi nhánh nên không có đường cộng trùng nút cha–con.
    """
    conditions: list[str] = []
    if dataset.supports_branch:
        conditions.append(
            "(CAST(:branch_ids AS INTEGER[]) IS NULL OR scoped.branch_id = ANY(:branch_ids))"
        )
    if dataset.supports_ledger:
        conditions.append("scoped.ledger = :ledger")
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    order_columns: list[str] = []
    for key in layout_spec.sort:
        if not _IDENTIFIER_RE.match(key):
            raise ReportDatasetInvalidError(
                "Khóa sắp xếp của layout không phải identifier hợp lệ", sort_key=key
            )
        order_columns.append(f"scoped.{key}")
    order_clause = f" ORDER BY {', '.join(order_columns)}" if order_columns else ""

    return f"SELECT scoped.* FROM (\n{dataset.sql_text}\n) AS scoped{where_clause}{order_clause}"  # noqa: S608 — sql_text là metadata do quản trị/gói kiểm soát, giá trị bind riêng; identifier đã ràng regex ở trên


def compose_count_query(dataset: ReportDataset, layout_spec: LayoutSpec) -> str:
    """`COUNT(*)` bọc quanh CHÍNH câu của `compose_scoped_query` — cho ngưỡng
    chuyển-job (lát 5E).

    Bọc câu đã ghép thay vì tự dựng câu đếm riêng: ước lượng phải đếm đúng
    những dòng mà lượt render thật sẽ thấy (cùng lớp bọc branch/ledger, cùng
    RLS); ORDER BY thừa trong subquery là giá chấp nhận được — PostgreSQL bỏ
    sort khi kế hoạch không cần nó.
    """
    return (
        f"SELECT COUNT(*) FROM (\n{compose_scoped_query(dataset, layout_spec)}\n) AS dataset_rows"  # noqa: S608 — cùng hạng tin cậy với compose_scoped_query ngay trên
    )


def assert_placeholders_allowed(dataset: ReportDataset) -> None:
    """`sql_text` chỉ được dùng placeholder trong `allowed_params` (fail-closed).

    Chiều ngược lại (khai trong `allowed_params` mà SQL không dùng) hợp lệ:
    `branch_ids` do lớp bọc dùng, và một tham số tùy chọn có thể chỉ xuất hiện
    trong một nhánh `CASE` đã bỏ.
    """
    unknown = sql_placeholders(dataset.sql_text) - frozenset(dataset.allowed_params)
    if unknown:
        raise ReportDatasetInvalidError(
            f"SQL của dataset {dataset.code!r} dùng placeholder ngoài allowed_params: "
            f"{', '.join(sorted(unknown))}",
            dataset_code=dataset.code,
        )
