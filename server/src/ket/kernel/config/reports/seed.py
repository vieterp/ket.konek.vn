"""Gieo metadata báo cáo builtin lúc cấp dữ liệu kế toán (`provision_dataset`).

Cùng khuôn `packages/seed.py`: nhận `Connection`, Core `insert` (bảng `Audited`
mà lúc cấp chưa có ai để khai `AuditContext`), idempotent. Khác một điểm có chủ
đích: idempotent **theo từng dòng** (`code` nào thiếu thì thêm) chứ không
"tồn tại thì bỏ qua cả bộ" — báo cáo builtin không có vòng đời kích hoạt như
gói cấu hình, và một bản phát hành sau thêm báo cáo mới phải lấp được chỗ
trống trên dataset cấp từ trước (cùng tinh thần `_ensure_statements_backfilled`
của 5B). Không bao giờ ghi đè dòng đã có: người dùng/quản trị có thể đã sửa
layout — nâng cấp nội dung một báo cáo builtin là chuyện của bản phát hành +
mã mới, không phải của đường gieo mầm.

**Probe `LIMIT 0` — kiểm bằng chính câu SQL engine sẽ chạy:** với mỗi
definition, ghép đúng câu lệnh của `compose_scoped_query` rồi chạy với bind
giả và `LIMIT 0`. Chứng minh được bốn thứ mà không kiểm tĩnh nào chứng minh
nổi: SQL đúng cú pháp trên schema thật; cột phạm vi (`branch_id`/`ledger`) và
cột `sort` tồn tại; kiểu bind khớp; và **mọi cột layout hiển thị có mặt trong
kết quả**. Chạy lúc gieo (dataset rỗng) nên miễn phí.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import Connection, insert, select, text

from ket.kernel.config.reports.loader import LoadedReports, load_builtin_reports
from ket.kernel.config.reports.models import (
    ReportDataset,
    ReportDefinition,
    ReportLayout,
    ReportParamSet,
)
from ket.kernel.config.reports.scope import assert_placeholders_allowed, compose_scoped_query
from ket.kernel.config.reports.spec import LayoutSpec, ParamSpec
from ket.kernel.errors import ReportDatasetInvalidError
from ket.kernel.persistence.seeding import bind_seed_schema

logger = structlog.get_logger(__name__)

_PROBE_DUMMIES: Final[dict[str, object]] = {
    "date": date(2000, 1, 1),
    "int": 0,
    "text": "",
    "bool": False,
    "decimal": Decimal(0),
}
"""Giá trị giả CÓ KIỂU cho probe — `None` trần sẽ để PostgreSQL đoán kiểu và
bỏ lọt lỗi ép kiểu mà lượt render thật mới lộ ra."""


def ensure_builtin_reports(connection: Connection, schema: str) -> int:
    """Gieo mọi mảnh metadata báo cáo còn thiếu. Trả về số dòng thêm mới."""
    bind_seed_schema(connection, schema)
    loaded = load_builtin_reports()
    added = 0
    added += _seed_datasets(connection, loaded)
    added += _seed_param_sets(connection, loaded)
    added += _seed_layouts(connection, loaded)
    added += _seed_definitions(connection, loaded)
    _probe_definitions(connection, loaded)
    if added:
        logger.info("reports.builtin_seeded", schema=schema, rows_added=added)
    return added


def _seed_datasets(connection: Connection, loaded: LoadedReports) -> int:
    existing = set(connection.execute(select(ReportDataset.code)).scalars())
    added = 0
    for entry in loaded.manifest.datasets:
        if entry.code in existing:
            continue
        dataset = ReportDataset(
            code=entry.code,
            sql_text=loaded.sql_by_dataset[entry.code],
            allowed_params=list(entry.allowed_params),
            supports_branch=entry.supports_branch,
            supports_ledger=entry.supports_ledger,
            is_builtin=True,
            description=entry.description,
        )
        assert_placeholders_allowed(dataset)
        connection.execute(
            insert(ReportDataset).values(
                code=dataset.code,
                sql_text=dataset.sql_text,
                allowed_params=dataset.allowed_params,
                supports_branch=dataset.supports_branch,
                supports_ledger=dataset.supports_ledger,
                is_builtin=True,
                description=dataset.description,
            )
        )
        added += 1
    return added


def _seed_param_sets(connection: Connection, loaded: LoadedReports) -> int:
    existing = set(connection.execute(select(ReportParamSet.code)).scalars())
    added = 0
    for entry in loaded.manifest.param_sets:
        if entry.code in existing:
            continue
        connection.execute(insert(ReportParamSet).values(code=entry.code, spec=entry.spec))
        added += 1
    return added


def _seed_layouts(connection: Connection, loaded: LoadedReports) -> int:
    existing = set(connection.execute(select(ReportLayout.code)).scalars())
    added = 0
    for entry in loaded.manifest.layouts:
        if entry.code in existing:
            continue
        connection.execute(
            insert(ReportLayout).values(
                code=entry.code,
                kind=entry.kind,
                spec=entry.spec,
                is_builtin=True,
                package_id=None,
            )
        )
        added += 1
    return added


def _seed_definitions(connection: Connection, loaded: LoadedReports) -> int:
    existing = set(connection.execute(select(ReportDefinition.code)).scalars())
    added = 0
    for entry in loaded.manifest.definitions:
        if entry.code in existing:
            continue
        connection.execute(
            insert(ReportDefinition).values(
                code=entry.code,
                name=entry.name,
                name_en=entry.name_en,
                category=entry.category,
                module=entry.module,
                dataset_code=entry.dataset_code,
                layout_code=entry.layout_code,
                param_set_code=entry.param_set_code,
                ledger_scope=entry.ledger_scope,
                is_builtin=True,
                package_id=None,
            )
        )
        added += 1
    return added


def _probe_definitions(connection: Connection, loaded: LoadedReports) -> None:
    """Chạy thử từng báo cáo builtin bằng chính câu SQL của engine (docstring đầu tệp).

    Chạy cả trên báo cáo đã gieo từ trước, không chỉ dòng mới thêm: migration
    có thể vừa đổi bảng gốc (`gl_postings` thêm/bớt cột) làm một dataset cũ hỏng
    — probe lúc gieo là chỗ duy nhất phát hiện sớm việc đó.
    """
    datasets = {entry.code: entry for entry in loaded.manifest.datasets}
    for definition in loaded.manifest.definitions:
        entry = datasets[definition.dataset_code]
        dataset = ReportDataset(
            code=entry.code,
            sql_text=loaded.sql_by_dataset[entry.code],
            allowed_params=list(entry.allowed_params),
            supports_branch=entry.supports_branch,
            supports_ledger=entry.supports_ledger,
        )
        layout_spec = loaded.layout_specs[definition.layout_code]
        params = loaded.param_set_specs[definition.param_set_code].params
        composed = compose_scoped_query(dataset, layout_spec)
        binds = _probe_binds(entry.allowed_params, params)
        result = connection.execute(
            text(f"SELECT probe.* FROM (\n{composed}\n) AS probe LIMIT 0"),  # noqa: S608 — composed từ compose_scoped_query trên dữ liệu builtin đóng gói
            binds,
        )
        returned = set(result.keys())
        _assert_layout_columns_present(definition.code, layout_spec, returned)


def _probe_binds(
    allowed_params: tuple[str, ...], params: tuple[ParamSpec, ...]
) -> dict[str, object]:
    kinds = {param.name: param.kind for param in params}
    binds: dict[str, object] = {}
    for name in allowed_params:
        if name == "branch_ids":
            continue  # lớp bọc bind riêng bên dưới
        if name == "ledger":
            binds[name] = 0
        elif name in ("from_date", "to_date"):
            binds[name] = date(2000, 1, 1)
        else:
            binds[name] = _PROBE_DUMMIES[kinds[name]]
    binds["branch_ids"] = None
    binds.setdefault("ledger", 0)
    return binds


def _assert_layout_columns_present(
    report_code: str, layout_spec: LayoutSpec, returned: set[str]
) -> None:
    needed = {column.key for column in layout_spec.columns}
    needed.update(group.key for group in layout_spec.group_by)
    for group in layout_spec.group_by:
        needed.update(group.heading_keys)
    missing = needed - returned
    if missing:
        raise ReportDatasetInvalidError(
            f"Báo cáo {report_code!r}: layout cần cột không có trong dataset: "
            f"{', '.join(sorted(missing))}",
            report_code=report_code,
        )
