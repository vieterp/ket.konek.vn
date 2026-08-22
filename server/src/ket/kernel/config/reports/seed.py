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

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Final

import structlog
from sqlalchemy import Connection, delete, insert, select, text, update

from ket.kernel.config.accounts_models import ConfigPackage
from ket.kernel.config.reports.loader import LoadedReports, load_builtin_reports
from ket.kernel.config.reports.models import (
    ReportDataset,
    ReportDefinition,
    ReportLayout,
    ReportParamSet,
)
from ket.kernel.config.reports.scope import assert_placeholders_allowed, compose_scoped_query
from ket.kernel.config.reports.spec import LayoutSpec, ParamSpec, coerce_param_value
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


def refresh_builtin_reports(connection: Connection, schema: str) -> None:
    """Đưa metadata báo cáo BUILTIN của một schema đã có dữ liệu về đúng nội
    dung đóng gói của bản phát hành hiện tại — đường dành cho migration.

    Khác `ensure_builtin_reports` (chỉ lấp chỗ trống, không bao giờ ghi đè):
    hàm này **thay** nội dung dòng builtin — hợp lệ khi chưa có bản cài phát
    hành nào nên "người dùng đã sửa layout builtin" chưa phải một trạng thái
    cần bảo toàn. Lời gọi duy nhất nằm ở migration **MỚI NHẤT** của chuỗi
    (0014 lát 5D → chuyển sang 0017 lát 6B): hàm đọc dữ liệu đóng gói hiện tại
    và probe SQL của từng dataset, nên chạy giữa chuỗi sẽ đổ ngay khi một
    dataset builtin tham chiếu cột ra đời sau nó. Sau bản phát hành đầu tiên,
    nâng cấp nội dung builtin phải quay về doctrine mã-mới-cho-nội-dung-mới
    của `ensure_builtin_reports` (docstring đầu tệp) — khi đó bước này rời
    khỏi chuỗi migration hẳn.

    Thứ tự: xóa definition builtin (không gì tham chiếu definition) → cập nhật
    dataset/layout/param set builtin còn tồn tại về nội dung đóng gói → gieo
    lại phần thiếu (kèm probe `LIMIT 0`). Dòng KHÔNG-builtin (đăng ký lúc chạy)
    giữ nguyên.
    """
    bind_seed_schema(connection, schema)
    loaded = load_builtin_reports()
    connection.execute(delete(ReportDefinition).where(ReportDefinition.is_builtin.is_(True)))
    for entry in loaded.manifest.datasets:
        connection.execute(
            update(ReportDataset)
            .where(ReportDataset.code == entry.code, ReportDataset.is_builtin.is_(True))
            .values(
                sql_text=loaded.sql_by_dataset[entry.code],
                allowed_params=list(entry.allowed_params),
                supports_branch=entry.supports_branch,
                supports_ledger=entry.supports_ledger,
                description=entry.description,
            )
        )
    for layout in loaded.manifest.layouts:
        connection.execute(
            update(ReportLayout)
            .where(ReportLayout.code == layout.code, ReportLayout.is_builtin.is_(True))
            .values(kind=layout.kind, spec=layout.spec)
        )
    # `report_param_sets` không có cột `is_builtin` (khác dataset/layout): mã
    # trong manifest là danh tính — UPDATE theo đúng mã builtin không chạm được
    # dòng người dùng đăng ký (mã khác). Người dùng đặt trùng một mã builtin
    # thì bị chính seed coi là dòng builtin — đó là lý do mã builtin thuộc
    # không-gian-tên của manifest, không phải của người dùng.
    for param_set in loaded.manifest.param_sets:
        connection.execute(
            update(ReportParamSet)
            .where(ReportParamSet.code == param_set.code)
            .values(spec=param_set.spec)
        )
    ensure_builtin_reports(connection, schema)


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
    package_ids = _builtin_package_ids(connection)
    added = 0
    for entry in loaded.manifest.definitions:
        if entry.code in existing:
            continue
        if entry.package_scheme is not None and entry.package_scheme not in package_ids:
            # Gói builtin của scheme chưa được gieo — xảy ra đúng một trường
            # hợp: migration gọi seed này trên schema đang provision (packages
            # gieo SAU chuỗi migration). Bỏ qua chứ không lỗi: lượt gọi trong
            # `provision_dataset` chạy sau `ensure_builtin_packages` sẽ lấp
            # đúng những dòng này.
            logger.info(
                "reports.definition_deferred_missing_package",
                code=entry.code,
                scheme=entry.package_scheme,
            )
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
                package_id=(
                    package_ids[entry.package_scheme] if entry.package_scheme is not None else None
                ),
                fixed_params=entry.fixed_params,
            )
        )
        added += 1
    return added


def _builtin_package_ids(connection: Connection) -> dict[str, int]:
    """`scheme` → id gói cấu hình **builtin** của scheme đó.

    Neo vào gói builtin chứ không gói "đã kích hoạt": mã mẫu (`S03a-DN`) thuộc
    về THÔNG TƯ, không thuộc phiên bản gói nào đang chạy — một dataset kích
    hoạt gói TT99 nhập ngoài vẫn in Sổ Nhật ký chung theo đúng mẫu S03a-DN.
    Mỗi scheme có đúng một gói builtin (`ensure_builtin_packages` gieo theo
    `code` cố định); nếu về sau một scheme có nhiều gói builtin theo ngày hiệu
    lực, dòng mới nhất thắng — cùng phép xếp với `resolve_package`.
    """
    rows = connection.execute(
        select(ConfigPackage.scheme, ConfigPackage.id)
        .where(ConfigPackage.is_builtin.is_(True))
        .order_by(ConfigPackage.effective_from, ConfigPackage.id)
    ).all()
    return {row.scheme: row.id for row in rows}


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
        binds = _probe_binds(entry.allowed_params, params, definition.fixed_params)
        result = connection.execute(
            text(f"SELECT probe.* FROM (\n{composed}\n) AS probe LIMIT 0"),  # noqa: S608 — composed từ compose_scoped_query trên dữ liệu builtin đóng gói
            binds,
        )
        returned = set(result.keys())
        _assert_layout_columns_present(definition.code, layout_spec, returned)


def _probe_binds(
    allowed_params: tuple[str, ...],
    params: tuple[ParamSpec, ...],
    fixed_params: Mapping[str, object] = MappingProxyType({}),
) -> dict[str, object]:
    """Bind giả cho probe. Tham số GHIM bind giá trị THẬT chứ không giá trị
    giả: một giá trị ghim mà SQL không nuốt nổi (sai kiểu trong `CAST`, không
    thuộc tập enum câu lệnh so khớp) phải nổ ở đây, không phải ở lượt render
    đầu tiên của người dùng."""
    by_name = {param.name: param for param in params}
    binds: dict[str, object] = {}
    for name in allowed_params:
        if name == "branch_ids":
            continue  # lớp bọc bind riêng bên dưới
        if name == "ledger":
            binds[name] = 0
        elif name in ("from_date", "to_date"):
            binds[name] = date(2000, 1, 1)
        elif name in fixed_params:
            param = by_name[name]
            binds[name] = coerce_param_value(
                fixed_params[name], param=param, where="Probe báo cáo builtin"
            )
        else:
            binds[name] = _PROBE_DUMMIES[by_name[name].kind]
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
