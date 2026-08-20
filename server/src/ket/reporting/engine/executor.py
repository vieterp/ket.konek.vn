"""Chạy SQL dataset theo lô — server-side cursor, bind-only (chống SQL injection).

Ba lớp phòng thủ của phase-05 §Report engine hội tụ ở đây:

1. Giá trị bind qua `text()` + params — `test_no_sql_string_interpolation` canh
   toàn repo, tệp này không ngoại lệ.
2. Câu lệnh cuối do `compose_scoped_query` (kernel) ghép — identifier đã ràng,
   lớp bọc `branch_id`/`ledger` là phòng thủ lớp hai (BR-RPT-04/05).
3. Cô lập thật là RLS trên bảng gốc (RT-04): engine chạy trong session của
   người gọi, `SUM() OVER ()` trong dataset không thấy nổi chi nhánh ngoài
   phạm vi vì RLS lọc ở tầng đọc bảng.

`yield_per` bật `stream_results` (server-side cursor của psycopg): 50.000 dòng
Sổ Cái không thành 50.000 đối tượng trong RAM cùng lúc — điều kiện để renderer
XLSX `constant_memory` và grouping streaming có ý nghĩa.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Final, cast

from sqlalchemy import text
from sqlalchemy.orm import Session

from ket.kernel.config.reports.models import ReportDataset
from ket.kernel.config.reports.scope import assert_placeholders_allowed, compose_scoped_query
from ket.kernel.config.reports.spec import LayoutSpec
from ket.kernel.errors import ReportDatasetNotExecutableError

FETCH_BATCH_SIZE: Final = 1000


def execute_dataset(
    session: Session,
    *,
    dataset: ReportDataset,
    layout_spec: LayoutSpec,
    binds: Mapping[str, object],
) -> Iterator[Mapping[str, object]]:
    """Dòng kết quả của một báo cáo, theo đúng `ORDER BY` từ layout.

    Trả `Mapping` theo tên cột (không tuple theo vị trí): grouping và renderer
    tra cột theo `key` của layout — dataset thêm cột mới không xô lệch báo cáo
    đang chạy.
    """
    if not dataset.is_builtin:
        # RT-07: SQL từ gói nhập ngoài phải chạy bằng role read-only RLS-bound.
        # Role đó đến ở 5D — từ chối rõ ràng thay vì chạy nhầm quyền runtime.
        raise ReportDatasetNotExecutableError(
            "Dataset từ gói nhập ngoài chưa chạy được ở bản này",
            dataset_code=dataset.code,
        )
    assert_placeholders_allowed(dataset)
    statement = text(compose_scoped_query(dataset, layout_spec)).execution_options(
        yield_per=FETCH_BATCH_SIZE
    )
    result = session.execute(statement, dict(binds))
    for row in result:
        # `RowMapping` khai khóa union (str | Column) vì Core cho tra theo cả
        # hai; kết quả `text()` chỉ có khóa chuỗi — cast thay vì copy từng dòng
        # sang dict (50k dòng Sổ Cái không cần 50k dict trung gian).
        yield cast("Mapping[str, object]", row._mapping)
