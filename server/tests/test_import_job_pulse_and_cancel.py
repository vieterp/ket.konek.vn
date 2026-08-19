"""Vòng đời job của một lượt nhập: nhịp gia hạn lease theo lô, và nút Hủy có thật.

Hai lời hứa mà mã trước đây chỉ giữ một nửa:

* `WorkerProgress.report` "vừa là tiến độ vừa là nhịp gia hạn lease" — nhưng
  thân job chỉ gọi nó ở **ba mốc cố định**, nên một pha chạy quá lease (đọc tệp
  10.000 dòng là vòng lặp Python dài nhất của cả kernel) là đủ để reaper coi
  worker đã chết và giao job cho tiến trình khác.
* `request_cancel` hứa "worker dừng ở ranh giới lô gần nhất" — nhưng không có
  dòng nào trong `kernel/excel/` đọc cờ hủy, nên lời hứa chỉ đúng với job chưa
  bắt đầu chạy.

Bộ test này đo cả hai bằng chính `run_import` trên PostgreSQL thật.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import unique_code
from import_support import FakeProgress, import_source, minimal_row, spec_of, workbook_bytes
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.excel.models import ImportStagingRow
from ket.kernel.excel.pipeline import PROGRESS_LOADED
from ket.kernel.excel.report import ImportMode
from ket.kernel.excel.sql import table_for
from ket.kernel.excel.staging import INSERT_BATCH
from ket.kernel.jobs.registry import JobCancelled
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

SLUG = "units_of_measure"


@pytest.fixture
def scope(dataset_alpha: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())


def _rows(count: int) -> list[list[object]]:
    spec = spec_of(SLUG)
    return [
        minimal_row(spec, unique_code("NHIP"), f"Đơn vị nhịp {index}") for index in range(count)
    ]


def test_loading_reports_progress_at_every_batch_boundary(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Tệp dài hơn hai lô phải sinh ra lượt `report` **giữa** 0% và mốc "đã đọc".

    Khẳng định nhịp chứ không đếm chính xác: số lượt = ⌊dòng / lô⌋ là chi tiết
    cài đặt; thứ hợp đồng đòi là "pha đọc không im lặng quá một lô" — vì mỗi
    lượt `report` của bản thật chính là một lần gia hạn lease.
    """
    progress = FakeProgress(reports=[])
    import_source(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        SLUG,
        workbook_bytes(SLUG, _rows(INSERT_BATCH * 2 + 10)),
        progress=progress,
    )

    mid_load = [percent for percent, _ in progress.reports if percent < PROGRESS_LOADED]
    assert len(mid_load) >= 2, (
        f"pha đọc {INSERT_BATCH * 2 + 10} dòng phải báo nhịp ở từng lô {INSERT_BATCH} dòng, "
        f"chỉ thấy: {progress.reports}"
    )


def test_cancel_stops_at_a_batch_boundary_and_commits_nothing(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Cờ hủy đã đặt → `JobCancelled` ở ranh giới lô, và không gì được giữ lại.

    `JobCancelled` phải thoát ra khỏi `run_import` để `unit_of_work` của thân
    job rollback trọn — worker sau đó mới `mark_cancelled`. Nuốt nó bên trong
    là giữ lại một lượt nhập nửa chừng mà màn hình báo "đã hủy".
    """
    spec = spec_of(SLUG)
    table = table_for(spec.model)
    progress = FakeProgress(reports=[], cancelled=True)

    with pytest.raises(JobCancelled):
        import_source(
            session_factory,
            dataset_alpha,
            scope,
            tmp_path,
            SLUG,
            workbook_bytes(SLUG, _rows(INSERT_BATCH + 10)),
            mode=ImportMode.CREATE_ONLY,
            commit=True,
            progress=progress,
        )

    with unit_of_work(session_factory, scope) as session:
        staged = session.execute(select(func.count()).select_from(ImportStagingRow)).scalar_one()
        created = session.execute(
            select(func.count()).select_from(table).where(table.c.name.like("Đơn vị nhịp %"))
        ).scalar_one()
    assert staged == 0, "rollback phải cuốn cả bảng đệm"
    assert created == 0, "không bản ghi nào của lượt bị hủy được commit"
