"""Migration và model phải mô tả **cùng một** schema.

Kiểu lỗi mà test này bắt: ai đó thêm cột vào model, chạy được ở máy mình (vì DB
cục bộ đã có cột đó từ lần thử trước) rồi commit mà quên sinh migration. Ở máy
khách hàng, cột không tồn tại và lỗi hiện ra ở một truy vấn hoàn toàn khác.

Cách kiểm: sau khi chạy migration lên một schema sạch, so sánh metadata của
model với schema thật. Còn khác biệt = còn thiếu migration.

**Một điểm mù phải bịt bằng tay:** `compare_metadata` của Alembic **không so
sánh `CHECK`**. Một ràng buộc `CHECK` viết trong model mà quên chép sang
migration (hoặc ngược lại) đi qua được cổng trên mà không ai biết — review lát
3B-3 chứng minh bằng cách xóa hai `CHECK` khỏi migration `0005` và thấy cả bộ
test vẫn xanh. Mà migration mới là thứ dựng DB ở máy khách hàng, còn `CHECK` là
nơi phần lớn luật nghiệp vụ của lớp danh mục được ép. `test_check_constraints_...`
bên dưới đối chiếu thẳng với `pg_catalog`.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, Engine, text

from ket.kernel.datasets.provisioning import DatasetRef
from ket.model_registry import DatasetBase

pytestmark = pytest.mark.db


def test_no_pending_schema_changes(owner_engine: Engine, dataset_alpha: DatasetRef) -> None:
    with owner_engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{dataset_alpha.schema_name}"')
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True, "include_schemas": False},
        )
        diff = compare_metadata(context, DatasetBase.metadata)

    assert diff == [], (
        "Model và migration đã lệch nhau. Sinh migration mới:\n"
        "  uv run alembic -x schema=<schema> revision --autogenerate -m '<mô tả>'\n"
        f"Khác biệt: {diff}"
    )


def _expected_check_names() -> set[str]:
    """Tên `CHECK` mà model khai.

    Đọc thẳng `constraint.name`: SQLAlchemy đã áp `NAMING_CONVENTION` lúc ràng
    buộc được gắn vào bảng, nên tên ở đây **đã** là `ck_<bảng>_<tên>` — ghép tiền
    tố lần nữa cho ra `ck_items_ck_items_...` và test đỏ trên mọi bảng.

    Bỏ qua ràng buộc không đặt tên: không có tên thì PostgreSQL tự đặt và không
    đối chiếu được — mà `NAMING_CONVENTION` cũng đòi `constraint_name`, nên một
    `CHECK` vô danh sẽ nổ ngay lúc dựng metadata.
    """
    return {
        str(constraint.name)
        for table in DatasetBase.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str)
    }


def test_check_constraints_declared_in_models_exist_in_the_database(
    owner_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Mọi `CHECK` của model phải có thật trong schema đã migrate.

    Cổng riêng vì `compare_metadata` **không** so sánh `CHECK` (xem docstring
    module). Đây là chỗ duy nhất bắt được "model có ràng buộc, migration quên
    chép" — và ở lớp danh mục thì `CHECK` chính là nơi phần lớn luật nghiệp vụ
    được ép, nên bỏ sót một cái là mở một đường ghi dữ liệu sai ở máy khách hàng
    trong khi mọi test ở máy lập trình viên vẫn xanh.
    """
    with owner_engine.connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{dataset_alpha.schema_name}"')
        actual = {
            str(row[0])
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE contype = 'c' AND connamespace = current_schema()::regnamespace"
                )
            )
        }

    missing = _expected_check_names() - actual

    assert not missing, (
        f"`CHECK` khai trong model nhưng không có trong schema đã migrate: {sorted(missing)}. "
        "Chép chúng sang migration — `compare_metadata` của Alembic không bắt được thiếu sót này."
    )
