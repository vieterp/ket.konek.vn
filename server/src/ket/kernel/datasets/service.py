"""Tra cứu dữ liệu kế toán — đường đọc cho middleware định tuyến schema.

Middleware `dataset_context` (bước 8, slice sau) gọi `resolve_dataset` một lần
mỗi request để đổi mã dataset trong token/header thành tên schema, rồi
`persistence.session` đặt `search_path`. Tách ra khỏi `provisioning` vì đây là
đường **đọc nóng** chạy bằng vai trò runtime, còn provisioning là đường ghi đặc
quyền chạy hiếm.
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ket.kernel.datasets.models import Dataset
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import DatasetNotFoundError


def _to_ref(dataset: Dataset) -> DatasetRef:
    return DatasetRef(
        id=dataset.id,
        code=dataset.code,
        schema_name=dataset.schema_name,
        scheme=dataset.scheme,
    )


def resolve_dataset(engine: Engine, code: str) -> DatasetRef:
    """Mã dataset → tham chiếu schema. Ném lỗi nếu không có hoặc đã vô hiệu hóa.

    Cố ý **không** tự suy tên schema từ mã bằng `schema_name_for`: chỉ dataset
    đã đăng ký trong bảng điều khiển mới được định tuyến tới. Nếu suy trực tiếp,
    một mã bịa trong token sẽ trỏ tới `search_path` của một schema không tồn
    tại và lỗi sẽ hiện ra ở tận tầng SQL.
    """
    with Session(engine) as session:
        dataset = session.scalar(
            select(Dataset).where(Dataset.code == code, Dataset.is_active.is_(True))
        )
        if dataset is None:
            raise DatasetNotFoundError("Không tìm thấy dữ liệu kế toán đang hoạt động", code=code)
        return _to_ref(dataset)


def list_datasets(engine: Engine, *, include_inactive: bool = False) -> tuple[DatasetRef, ...]:
    """Danh sách dữ liệu kế toán cho màn hình chọn lúc đăng nhập (FR-SYS-001)."""
    statement = select(Dataset).order_by(Dataset.code)
    if not include_inactive:
        statement = statement.where(Dataset.is_active.is_(True))
    with Session(engine) as session:
        return tuple(_to_ref(dataset) for dataset in session.scalars(statement))
