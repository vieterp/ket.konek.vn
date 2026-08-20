"""Gieo mẫu in builtin lúc cấp dữ liệu kế toán — cùng khuôn `reports/seed.py`.

Idempotent theo từng dòng `(document_type, code)`: bản phát hành sau thêm mẫu
mới thì lấp được chỗ trống; KHÔNG ghi đè dòng đã có (người dùng có thể đã sửa
mẫu — FR-RPT-008 cho phép, nâng cấp nội dung một mẫu builtin là chuyện của bản
phát hành + mã mới).
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Connection, insert, select

from ket.kernel.config.printing.models import (
    DOCUMENT_TYPE_MAX_LENGTH,
    TEMPLATE_NAME_MAX_LENGTH,
    PrintTemplate,
)
from ket.kernel.config.reports.models import REPORT_CODE_MAX_LENGTH
from ket.kernel.errors import ReportSpecInvalidError
from ket.kernel.persistence.seeding import bind_seed_schema

logger = structlog.get_logger(__name__)

_DATA_ROOT: Final = resources.files("ket.kernel.config.printing").joinpath("data")

_CODE_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


class _TemplateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_type: str = Field(min_length=1, max_length=DOCUMENT_TYPE_MAX_LENGTH)
    code: str = Field(pattern=_CODE_PATTERN, max_length=REPORT_CODE_MAX_LENGTH)
    name: str = Field(min_length=1, max_length=TEMPLATE_NAME_MAX_LENGTH)
    html_file: str = Field(pattern=r"^[a-z0-9-]+\.html\.j2$")
    """Chỉ tên tệp trong `data/` — không đường dẫn, không thư mục con (cùng
    bất biến chống path-traversal với `reports/loader.DatasetEntry.sql_file`)."""

    is_default: bool = False


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    templates: tuple[_TemplateEntry, ...]


def load_builtin_print_templates() -> tuple[tuple[_TemplateEntry, str], ...]:
    """`(entry, nguồn HTML)` cho từng mẫu builtin — fail-closed khi dữ liệu sai."""
    raw_text = _DATA_ROOT.joinpath("builtin_print_templates.json").read_text("utf-8")
    raw = json.loads(raw_text)
    if not isinstance(raw, dict):
        raise ReportSpecInvalidError("builtin_print_templates.json phải là một object JSON")
    raw.pop("__doc__", None)
    try:
        manifest = _Manifest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise ReportSpecInvalidError(
            f"builtin_print_templates.json sai hình dạng tại {location}: {first['msg']}"
        ) from exc
    keys = [(entry.document_type, entry.code) for entry in manifest.templates]
    if len(set(keys)) != len(keys):
        raise ReportSpecInvalidError("builtin_print_templates.json: mã mẫu trùng nhau")
    defaults = [entry.document_type for entry in manifest.templates if entry.is_default]
    if len(set(defaults)) != len(defaults):
        raise ReportSpecInvalidError(
            "builtin_print_templates.json: một loại chứng từ có hai mẫu mặc định"
        )
    loaded = []
    for entry in manifest.templates:
        html = _DATA_ROOT.joinpath(entry.html_file).read_text("utf-8")
        if not html.strip():
            raise ReportSpecInvalidError(f"Tệp mẫu {entry.html_file!r} rỗng")
        loaded.append((entry, html))
    return tuple(loaded)


def ensure_builtin_print_templates(connection: Connection, schema: str) -> int:
    """Gieo mẫu in builtin còn thiếu. Trả về số dòng thêm mới."""
    bind_seed_schema(connection, schema)
    existing = {
        (row.document_type, row.code)
        for row in connection.execute(select(PrintTemplate.document_type, PrintTemplate.code))
    }
    added = 0
    for entry, html in load_builtin_print_templates():
        if (entry.document_type, entry.code) in existing:
            continue
        connection.execute(
            insert(PrintTemplate).values(
                document_type=entry.document_type,
                code=entry.code,
                name=entry.name,
                html_template=html,
                css_extra=None,
                # Mẫu builtin chỉ đặt mặc định khi loại chứng từ CHƯA có mặc
                # định nào — người dùng đã chọn mẫu riêng làm mặc định thì bản
                # phát hành sau không giành lại (index một phần canh bất biến).
                is_default=entry.is_default and not _has_default(connection, entry.document_type),
                is_builtin=True,
                package_id=None,
            )
        )
        added += 1
    if added:
        logger.info("printing.builtin_seeded", schema=schema, rows_added=added)
    return added


def _has_default(connection: Connection, document_type: str) -> bool:
    return (
        connection.execute(
            select(PrintTemplate.id)
            .where(PrintTemplate.document_type == document_type)
            .where(PrintTemplate.is_default.is_(True))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )
