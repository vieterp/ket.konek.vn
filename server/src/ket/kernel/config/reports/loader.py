"""Đọc + kiểm `data/builtin_reports.json` fail-closed (trước khi chạm DB).

Cùng triết lý với `packages/loader.py`: dữ liệu builtin sai là **lỗi của bản
cài**, phải nổ lúc cấp dữ liệu kế toán/lúc test, không phải lúc người dùng bấm
"Xem báo cáo". Mọi kiểm ở đây thuần dữ liệu (hình dạng, tham chiếu nội bộ,
placeholder, trần độ dài soi gương cột ORM); kiểm **chạy thử SQL** cần kết nối
DB nên nằm ở `seed.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ket.kernel.config.reports.models import (
    CATEGORY_MAX_LENGTH,
    DESCRIPTION_MAX_LENGTH,
    REPORT_CODE_MAX_LENGTH,
    LayoutKind,
    LedgerScope,
    SpecDocument,
)
from ket.kernel.config.reports.spec import (
    STANDARD_PARAMS,
    LayoutSpec,
    ParamSetSpec,
    coerce_param_value,
    parse_layout_spec,
    parse_param_set_spec,
)
from ket.kernel.errors import ReportDatasetInvalidError, ReportSpecInvalidError
from ket.kernel.periods.models import AccountingScheme

_DATA_ROOT: Final = resources.files("ket.kernel.config.reports").joinpath("data")

_CODE_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"

NAME_MAX_LENGTH_FOR_MANIFEST: Final = 255
"""Soi gương `NAME_MAX_LENGTH` của cột ORM — bài học 5A: trần kiểm ở loader
phải khớp trần cột, nếu không lỗi chỉ lộ ra dưới dạng lỗi driver lúc INSERT."""


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetEntry(_ManifestModel):
    code: str = Field(pattern=_CODE_PATTERN, max_length=REPORT_CODE_MAX_LENGTH)
    sql_file: str = Field(pattern=r"^[a-z0-9_]+\.sql$")
    """Chỉ tên tệp trong `data/datasets/` — không đường dẫn, không thư mục con
    (cùng bất biến chống path-traversal với importer gói RT-07)."""

    allowed_params: tuple[str, ...]
    supports_branch: bool = True
    supports_ledger: bool = True
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX_LENGTH)


class ParamSetEntry(_ManifestModel):
    code: str = Field(pattern=_CODE_PATTERN, max_length=REPORT_CODE_MAX_LENGTH)
    spec: SpecDocument


class LayoutEntry(_ManifestModel):
    code: str = Field(pattern=_CODE_PATTERN, max_length=REPORT_CODE_MAX_LENGTH)
    kind: str
    spec: SpecDocument


class DefinitionEntry(_ManifestModel):
    code: str = Field(pattern=_CODE_PATTERN, max_length=REPORT_CODE_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH_FOR_MANIFEST)
    name_en: str | None = Field(default=None, max_length=NAME_MAX_LENGTH_FOR_MANIFEST)
    category: str = Field(min_length=1, max_length=CATEGORY_MAX_LENGTH)
    module: str = Field(min_length=1, max_length=CATEGORY_MAX_LENGTH)
    dataset_code: str
    layout_code: str
    param_set_code: str
    ledger_scope: str = LedgerScope.BOTH

    fixed_params: dict[str, object] = Field(default_factory=dict)
    """Tham số ghim `{tên: giá trị}` — xem `ReportDefinition.fixed_params`. Tên
    phải là tham số đã khai trong `param_set_code` và giá trị phải đúng kiểu
    `ParamSpec.kind`; cả hai kiểm ở `_assert_definitions_wired`."""

    package_scheme: str | None = None
    """`TT99`/`TT133` — báo cáo theo mẫu thông tư thuộc gói cấu hình builtin
    cùng scheme (`report_definitions.package_id` gán lúc gieo); `None` = không
    phụ thuộc chế độ kế toán. Kiểm giá trị ở `load_builtin_reports` bằng chính
    `AccountingScheme` để mã mới của một thông tư tương lai không lọt qua dưới
    dạng chuỗi gõ nhầm."""


class ReportManifest(_ManifestModel):
    datasets: tuple[DatasetEntry, ...]
    param_sets: tuple[ParamSetEntry, ...]
    layouts: tuple[LayoutEntry, ...]
    definitions: tuple[DefinitionEntry, ...]


@dataclass(frozen=True)
class LoadedReports:
    """Manifest đã kiểm + spec đã parse + SQL đã đọc — đầu vào của seed."""

    manifest: ReportManifest
    sql_by_dataset: dict[str, str]
    layout_specs: dict[str, LayoutSpec]
    param_set_specs: dict[str, ParamSetSpec]


def load_builtin_reports() -> LoadedReports:
    """Đọc và kiểm toàn bộ dữ liệu báo cáo builtin. Ném lỗi nghiệp vụ khi sai."""
    raw_text = _DATA_ROOT.joinpath("builtin_reports.json").read_text("utf-8")
    raw = json.loads(raw_text)
    if not isinstance(raw, dict):
        raise ReportSpecInvalidError("builtin_reports.json phải là một object JSON")
    raw.pop("__doc__", None)
    try:
        manifest = ReportManifest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise ReportSpecInvalidError(
            f"builtin_reports.json sai hình dạng tại {location}: {first['msg']}"
        ) from exc

    _assert_unique_codes(manifest)

    layout_specs: dict[str, LayoutSpec] = {}
    for layout in manifest.layouts:
        if layout.kind not in LayoutKind.ALL:
            raise ReportSpecInvalidError(
                f"Layout {layout.code!r} mang kind lạ: {layout.kind!r}", layout_code=layout.code
            )
        if layout.kind not in (LayoutKind.TABLE, LayoutKind.GROUPED):
            raise ReportSpecInvalidError(
                f"Layout {layout.code!r}: kind {layout.kind!r} chưa có hợp đồng spec "
                "(statement dùng statement_layouts; form đến ở 5D)",
                layout_code=layout.code,
            )
        layout_specs[layout.code] = parse_layout_spec(layout.spec, layout_code=layout.code)

    param_set_specs = {
        entry.code: parse_param_set_spec(entry.spec, param_set_code=entry.code)
        for entry in manifest.param_sets
    }

    sql_by_dataset: dict[str, str] = {}
    for dataset in manifest.datasets:
        sql_text = _DATA_ROOT.joinpath("datasets").joinpath(dataset.sql_file).read_text("utf-8")
        if not sql_text.strip():
            raise ReportDatasetInvalidError(
                f"Tệp SQL của dataset {dataset.code!r} rỗng", dataset_code=dataset.code
            )
        sql_by_dataset[dataset.code] = sql_text

    _assert_definitions_wired(manifest, param_set_specs)
    return LoadedReports(
        manifest=manifest,
        sql_by_dataset=sql_by_dataset,
        layout_specs=layout_specs,
        param_set_specs=param_set_specs,
    )


def _assert_unique_codes(manifest: ReportManifest) -> None:
    for label, codes in (
        ("datasets", [d.code for d in manifest.datasets]),
        ("param_sets", [p.code for p in manifest.param_sets]),
        ("layouts", [layout.code for layout in manifest.layouts]),
        ("definitions", [d.code for d in manifest.definitions]),
    ):
        if len(set(codes)) != len(codes):
            raise ReportSpecInvalidError(f"builtin_reports.json: mã trùng nhau trong {label}")


def _assert_definitions_wired(
    manifest: ReportManifest, param_set_specs: dict[str, ParamSetSpec]
) -> None:
    """Definition chỉ được trỏ vào mảnh ghép có mặt trong CHÍNH manifest —
    builtin là bộ tự đủ, không dựa vào dữ liệu đã có sẵn trong một DB nào đó."""
    datasets = {d.code: d for d in manifest.datasets}
    layouts = {layout.code for layout in manifest.layouts}
    known_schemes = frozenset(member.value for member in AccountingScheme)
    for definition in manifest.definitions:
        if definition.package_scheme is not None and definition.package_scheme not in known_schemes:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r} mang package_scheme lạ: "
                f"{definition.package_scheme!r}",
                report_code=definition.code,
            )
        if definition.ledger_scope not in LedgerScope.ALL:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r} mang ledger_scope lạ: {definition.ledger_scope!r}",
                report_code=definition.code,
            )
        dataset = datasets.get(definition.dataset_code)
        if dataset is None:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r} trỏ dataset không có trong manifest: "
                f"{definition.dataset_code!r}",
                report_code=definition.code,
            )
        if definition.layout_code not in layouts:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r} trỏ layout không có trong manifest: "
                f"{definition.layout_code!r}",
                report_code=definition.code,
            )
        param_spec = param_set_specs.get(definition.param_set_code)
        if param_spec is None:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r} trỏ bộ tham số không có trong manifest: "
                f"{definition.param_set_code!r}",
                report_code=definition.code,
            )
        # Tham số NGOÀI bộ chuẩn phải nằm trong `allowed_params` của dataset —
        # một tham số client gửi lên mà SQL không nhận là một ô nhập vô dụng
        # trên màn hình, phát hiện lúc gieo chứ không lúc người dùng bấm thử.
        allowed = frozenset(dataset.allowed_params)
        for param in param_spec.params:
            if param.name not in allowed:
                raise ReportSpecInvalidError(
                    f"Báo cáo {definition.code!r}: tham số {param.name!r} không nằm trong "
                    f"allowed_params của dataset {dataset.code!r}",
                    report_code=definition.code,
                )
        _assert_fixed_params_wired(definition, param_spec)
    _assert_no_unprovided_allowed_params(manifest, datasets, param_set_specs)


def _assert_fixed_params_wired(definition: DefinitionEntry, param_spec: ParamSetSpec) -> None:
    """Tham số ghim phải TRỎ vào một tham số đã khai và mang đúng kiểu của nó.

    Ghim một tên không khai ở đâu cả là ô nhập ma: SQL nhận bind mà màn hình
    không có nhãn, và không cỗ máy nào kiểm được kiểu. Ghim tham số thuộc bộ
    CHUẨN cũng bị chặn — `from_date`/`to_date`/`branch_ids` là hợp đồng chung
    FR-RPT-002, còn `ledger` đã có `ledger_scope` làm đúng việc đó.
    """
    declared = {param.name: param for param in param_spec.params}
    for name, value in definition.fixed_params.items():
        if name in STANDARD_PARAMS:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r}: không ghim được tham số thuộc bộ chuẩn "
                f"{name!r} (sổ tài chính/quản trị dùng `ledger_scope`)",
                report_code=definition.code,
            )
        param = declared.get(name)
        if param is None:
            raise ReportSpecInvalidError(
                f"Báo cáo {definition.code!r}: ghim tham số {name!r} không khai trong bộ "
                f"tham số {definition.param_set_code!r}",
                report_code=definition.code,
            )
        coerce_param_value(value, param=param, where=f"Báo cáo {definition.code!r}")


def _assert_no_unprovided_allowed_params(
    manifest: ReportManifest,
    datasets: dict[str, DatasetEntry],
    param_set_specs: dict[str, ParamSetSpec],
) -> None:
    # Chiều ngược: mọi `allowed_params` ngoài bộ chuẩn phải được ÍT NHẤT một bộ
    # tham số (của một definition dùng dataset đó) cung cấp — gộp theo dataset
    # chứ không kiểm trong vòng lặp definition, vì một dataset nhiều definition
    # có thể chia nhau các tham số.
    provided_by_dataset: dict[str, set[str]] = {code: set() for code in datasets}
    for definition in manifest.definitions:
        provided_by_dataset[definition.dataset_code].update(
            param.name for param in param_set_specs[definition.param_set_code].params
        )
    for code, dataset in datasets.items():
        unknown_allowed = (
            frozenset(dataset.allowed_params)
            - STANDARD_PARAMS
            - frozenset(provided_by_dataset[code])
        )
        if unknown_allowed:
            raise ReportSpecInvalidError(
                f"Dataset {code!r} khai allowed_params không ai cung cấp (không thuộc bộ "
                f"chuẩn, không bộ tham số nào khai): {', '.join(sorted(unknown_allowed))}",
                dataset_code=code,
            )
