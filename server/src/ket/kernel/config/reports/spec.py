"""Hình dạng có kiểu của cột `spec` (LD-13) — ranh giới duy nhất đọc JSONB thô.

Hai hợp đồng:

* `LayoutSpec` — cột, nhóm, tổng, sắp xếp, khổ giấy của một layout `table`/
  `grouped`. Layout `statement`/`form` có hợp đồng riêng ở lát sau (BCTC đã có
  `statement_layouts`; mẫu in chứng từ đến ở 5D).
* `ParamSetSpec` — tham số NGOÀI bộ chuẩn. Bộ chuẩn FR-RPT-002 khai ở
  `STANDARD_PARAMS`, áp cho mọi báo cáo, không khai lại trong dữ liệu.

Mọi cái tên đi vào SQL (`sort`, `group_by`, khóa cột) bị ràng
`_IDENTIFIER_RE` **và** engine chỉ chèn chúng sau khi đối chiếu với spec đã
parse — placeholder dữ liệu thì bind, identifier thì đối chiếu danh sách đóng,
không có đường nối chuỗi tự do nào (chống SQL injection tầng metadata).
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ket.kernel.config.reports.models import SpecDocument
from ket.kernel.errors import ReportSpecInvalidError

_IDENTIFIER_RE: Final = re.compile(r"^[a-z][a-z0-9_]*$")

STANDARD_PARAMS: Final[frozenset[str]] = frozenset({"from_date", "to_date", "branch_ids", "ledger"})
"""Bộ tham số chuẩn FR-RPT-002 — engine luôn bind, dataset dùng hay không tùy
`allowed_params`. `currency`/`org_unit_ids` của FR-RPT-002 vào khi có dataset
đầu tiên tiêu thụ (5D+, YAGNI) — thêm vào đây là client thấy ngay ô nhập."""


class ColumnType:
    """Kiểu dữ liệu một cột — quyết định định dạng số/ngày ở renderer."""

    TEXT = "text"
    DATE = "date"
    MONEY = "money"
    QUANTITY = "quantity"

    ALL = frozenset({TEXT, DATE, MONEY, QUANTITY})


class _SpecModel(BaseModel):
    """`extra="forbid"`: khóa gõ nhầm trong dữ liệu builtin/gói nhập ngoài —
    một trường lạ là một lỗi cấu hình, không phải thứ bị lờ đi."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ColumnSpec(_SpecModel):
    key: str = Field(pattern=_IDENTIFIER_RE.pattern, max_length=63)
    label: str = Field(min_length=1, max_length=200)
    label_en: str | None = Field(default=None, max_length=200)
    type: str
    align: Literal["left", "center", "right"] | None = None
    """Mặc định theo kiểu: số phải, ngày giữa, chữ trái — chỉ khai khi khác."""

    width: int | None = Field(default=None, ge=1, le=100)
    """Phần trăm bề ngang trang in; bỏ trống = chia đều phần còn lại."""

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ColumnType.ALL:
            raise ValueError(f"kiểu cột lạ: {value!r}")
        return value


class GroupSpec(_SpecModel):
    """Một cấp nhóm: ngắt theo `key`, tiêu đề nhóm ghép từ `heading_keys`.

    `key` không bắt buộc là cột hiển thị (Sổ Cái nhóm theo `account_code`
    nhưng mã TK nằm trên tiêu đề nhóm, không lặp lại từng dòng).
    """

    key: str = Field(pattern=_IDENTIFIER_RE.pattern, max_length=63)
    heading_keys: tuple[str, ...] = Field(min_length=1)

    @field_validator("heading_keys")
    @classmethod
    def _heading_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            if not _IDENTIFIER_RE.match(key):
                raise ValueError(f"heading_keys chứa tên không hợp lệ: {key!r}")
        return value


class PageSpec(_SpecModel):
    orientation: Literal["portrait", "landscape"] = "portrait"


class LayoutSpec(_SpecModel):
    """Toàn bộ phần trình bày của một báo cáo dạng bảng/nhóm."""

    columns: tuple[ColumnSpec, ...] = Field(min_length=1)
    group_by: tuple[GroupSpec, ...] = ()
    totals: tuple[str, ...] = ()
    """Cột cộng tổng (tổng nhóm + tổng cuối) — phải là cột `money`/`quantity`."""

    sort: tuple[str, ...] = Field(min_length=1)
    """Thứ tự dòng — engine dựng `ORDER BY` từ đây (identifier đã ràng, không
    phải dữ liệu người dùng). Grouping đọc dòng tuần tự nên các khóa nhóm PHẢI
    là tiền tố của `sort` — bất biến kiểm ở `parse_layout_spec`."""

    page: PageSpec = PageSpec()


class ParamSpec(_SpecModel):
    name: str = Field(pattern=_IDENTIFIER_RE.pattern, max_length=63)
    kind: Literal["date", "int", "text", "bool", "decimal"]
    label: str = Field(min_length=1, max_length=200)
    label_en: str | None = Field(default=None, max_length=200)
    required: bool = False


class ParamSetSpec(_SpecModel):
    params: tuple[ParamSpec, ...] = ()


def parse_layout_spec(raw: SpecDocument, *, layout_code: str) -> LayoutSpec:
    """JSONB thô → `LayoutSpec`, fail-closed kèm mã layout để sửa đúng chỗ."""
    try:
        spec = LayoutSpec.model_validate(raw)
    except ValidationError as exc:
        raise ReportSpecInvalidError(
            f"Layout báo cáo {layout_code!r} sai hình dạng: {exc.errors()[0]['msg']}",
            layout_code=layout_code,
        ) from exc

    column_keys = [column.key for column in spec.columns]
    if len(set(column_keys)) != len(column_keys):
        raise ReportSpecInvalidError(
            f"Layout báo cáo {layout_code!r} có khóa cột trùng nhau", layout_code=layout_code
        )
    by_key = {column.key: column for column in spec.columns}
    for key in spec.totals:
        column = by_key.get(key)
        if column is None or column.type not in (ColumnType.MONEY, ColumnType.QUANTITY):
            raise ReportSpecInvalidError(
                f"Layout báo cáo {layout_code!r}: cột tổng {key!r} phải là cột "
                "money/quantity có hiển thị",
                layout_code=layout_code,
            )
    group_keys = tuple(group.key for group in spec.group_by)
    if spec.sort[: len(group_keys)] != group_keys:
        raise ReportSpecInvalidError(
            f"Layout báo cáo {layout_code!r}: khóa nhóm phải là tiền tố của `sort` "
            "(grouping đọc dòng tuần tự — dòng cùng nhóm phải liền nhau)",
            layout_code=layout_code,
        )
    return spec


def parse_param_set_spec(raw: SpecDocument, *, param_set_code: str) -> ParamSetSpec:
    """JSONB thô → `ParamSetSpec`; tên tham số không được đè bộ chuẩn."""
    try:
        spec = ParamSetSpec.model_validate(raw)
    except ValidationError as exc:
        raise ReportSpecInvalidError(
            f"Bộ tham số {param_set_code!r} sai hình dạng: {exc.errors()[0]['msg']}",
            param_set_code=param_set_code,
        ) from exc
    names = [param.name for param in spec.params]
    if len(set(names)) != len(names):
        raise ReportSpecInvalidError(
            f"Bộ tham số {param_set_code!r} có tên tham số trùng nhau",
            param_set_code=param_set_code,
        )
    clash = STANDARD_PARAMS.intersection(names)
    if clash:
        raise ReportSpecInvalidError(
            f"Bộ tham số {param_set_code!r} khai lại tham số chuẩn: {', '.join(sorted(clash))}",
            param_set_code=param_set_code,
        )
    return spec
