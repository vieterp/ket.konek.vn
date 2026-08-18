"""Mô tả một tệp mẫu Excel — **sinh từ `CatalogSpec`**, không viết tay (H77).

Docstring của `master_data/registry.py` đã đặt tên tệp mẫu Excel là **bản sao
thứ ba** mà registry sinh ra để thay thế; hai bản kia (router, mã quyền) đã dùng
nó từ lát 3B-1. Đây là chỗ bản sao thứ ba được nối vào.

Hệ quả trực tiếp: thêm một danh mục ở phase 5 hay phase 9 thì tệp mẫu của nó có
sẵn, đúng cột, đúng thứ tự. Viết tay hai mươi mẫu thì mẫu thứ mười một sẽ thiếu
một cột vừa thêm — và triệu chứng không phải một lỗi biên dịch mà là một tệp
nhập liệu hoàn toàn hợp lệ bị từ chối vì "sai tên cột" (FR-SYS-082), tức là
người dùng bị đổ lỗi cho một sai sót của máy chủ.

**Ô tra cứu là mã, không phải id (H79).** Người điền tệp là kế toán ngồi trước
Excel: `parent_code` là thứ họ đọc được trên chính danh mục đang xuất ra, còn
`parent_id` là chi tiết cài đặt không ai gõ đúng. Cột `*_id` trong tệp mẫu là
lời mời dán nhầm khóa ngoại — và khóa ngoại dán nhầm thì hợp lệ về kiểu, nên
không có phép kiểm nào bắt được.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final, get_args, get_origin

from pydantic.fields import FieldInfo

from ket.kernel.master_data.base import CODE_MAX_LENGTH, NAME_MAX_LENGTH
from ket.kernel.master_data.registry import CatalogReference, CatalogSpec


class CellKind(StrEnum):
    """Kiểu dữ liệu của một cột trong tệp mẫu.

    Hẹp có chủ đích — năm kiểu, không phải một ánh xạ đầy đủ tới kiểu Python.
    Mỗi kiểu ở đây phải có **một phép ép kiểu bằng SQL** tương ứng ở
    `staging.py`, vì kiểm dữ liệu chạy set-based (H82). Thêm một kiểu mà quên
    phép ép của nó thì mọi ô của cột đó báo lỗi kiểu — nên số kiểu ít là một
    tính chất, không phải một thiếu sót.
    """

    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    CODE_REF = "code_ref"
    """Mã của một bản ghi ở danh mục khác — phân giải bằng `JOIN` (H79)."""


@dataclass(frozen=True)
class ColumnDescriptor:
    """Một cột của tệp mẫu.

    `header` là **hợp đồng**: FR-SYS-082 nói tệp đổi tên cột thì không nhận, nên
    chuỗi này đi vào tệp mẫu người dùng tải về và được so lại từng ký tự lúc họ
    nộp tệp. Đổi nó là một breaking change với mọi tệp khách hàng đang giữ.
    """

    field: str
    """Tên cột trên model ORM, hoặc tên cột tra cứu (`parent_code`)."""

    header: str
    """Nhãn tiếng Việt trên dòng tiêu đề của sheet dữ liệu."""

    kind: CellKind
    required: bool = False
    max_length: int | None = None

    reference_slug: str | None = None
    """Danh mục đích, chỉ có nghĩa với `CellKind.CODE_REF`."""

    target_field: str | None = None
    """Cột khóa ngoại mà mã này phân giải ra (`parent_code` → `parent_id`)."""

    allowed_values: tuple[str, ...] = ()
    """Tập giá trị hợp lệ, khi cột là một enum.

    Lấy từ **kiểu cột thật** chứ không khai tay: `items.nature` là một enum bốn
    giá trị, và một ô gõ sai ghi được vào DB (cột là `varchar`, và 3B-3 cố ý
    không sinh `CHECK` liệt kê). Hậu quả không nằm ở dòng đó: **mọi** lượt đọc
    `Item` qua ORM sau đấy ném `LookupError`, nên `GET /api/v1/master/items` hỏng
    cho cả dữ liệu kế toán và bản ghi sai thì không sửa được từ giao diện."""

    note: str = ""
    """Dòng giải thích trên sheet **Hướng dẫn** (FR-SYS-080)."""

    @property
    def display_header(self) -> str:
        """Tiêu đề như nó xuất hiện trên tệp: cột bắt buộc mang dấu `*` (FR-SYS-083).

        Dấu sao nằm trong **chính tiêu đề** chứ không ở một dòng chú thích riêng:
        người dùng sao chép dữ liệu vào mẫu bằng cách nhìn dòng tiêu đề, và một
        chú thích ở dòng khác là chú thích họ đã cuộn qua.
        """
        return f"{self.header} *" if self.required else self.header


@dataclass(frozen=True)
class TemplateDescriptor:
    """Tệp mẫu của một danh mục: tên sheet + danh sách cột.

    Dùng cho **cả** ba việc — sinh tệp mẫu, kiểm cấu trúc tệp nộp lên, và (ở lát
    3C-2) xuất dữ liệu ra. Một mô tả cho ba việc là điều kiện để tệp xuất ra
    luôn nhập lại được: nếu bộ xuất giữ danh sách cột riêng thì nó sẽ lệch, và
    lệch đúng vào lúc ai đó thêm một cột.
    """

    slug: str
    sheet_name: str
    columns: tuple[ColumnDescriptor, ...]

    @property
    def headers(self) -> tuple[str, ...]:
        return tuple(column.display_header for column in self.columns)

    def column(self, field: str) -> ColumnDescriptor | None:
        return next((item for item in self.columns if item.field == field), None)

    @property
    def references(self) -> tuple[ColumnDescriptor, ...]:
        """Cột tra cứu — phần cần một `JOIN` khi kiểm dữ liệu."""
        return tuple(item for item in self.columns if item.kind is CellKind.CODE_REF)


SHEET_NAME_MAX_LENGTH: Final[int] = 31
"""Trần của chính định dạng xlsx. Excel **cắt** tên dài hơn thay vì báo lỗi, nên
một tên 40 ký tự sẽ tạo ra một tệp mà phép kiểm cấu trúc của chính chúng ta từ
chối — lỗi chỉ lộ ra ở tệp mẫu của danh mục có tiêu đề dài nhất."""

INSTRUCTIONS_SHEET: Final[str] = "Hướng dẫn"
"""Tên sheet hướng dẫn (FR-SYS-080). Hằng số vì cả bộ sinh mẫu lẫn phép kiểm cấu
trúc phải đồng ý về nó: sheet này **được phép** vắng mặt ở tệp nộp lên (người
dùng hay xóa nó), nên phép kiểm phải biết tên để bỏ qua đúng nó."""


_COMMON_COLUMNS: Final[tuple[ColumnDescriptor, ...]] = (
    ColumnDescriptor(
        field="code",
        header="Mã",
        kind=CellKind.TEXT,
        required=True,
        max_length=CODE_MAX_LENGTH,
        note="Mã duy nhất trong phạm vi (dùng chung toàn công ty, hoặc trong một chi nhánh).",
    ),
    ColumnDescriptor(
        field="name",
        header="Tên",
        kind=CellKind.TEXT,
        required=True,
        max_length=NAME_MAX_LENGTH,
    ),
    ColumnDescriptor(
        field="name_en",
        header="Tên tiếng Anh",
        kind=CellKind.TEXT,
        max_length=NAME_MAX_LENGTH,
        note="Để trống nếu không dùng giao diện song ngữ.",
    ),
    ColumnDescriptor(
        field="parent_code",
        header="Mã nhóm cha",
        kind=CellKind.CODE_REF,
        target_field="parent_id",
        max_length=CODE_MAX_LENGTH,
        note="Để trống nếu bản ghi nằm ở cấp gốc. Nhóm cha phải có trước trong cùng tệp "
        "hoặc đã có sẵn trong danh mục.",
    ),
    ColumnDescriptor(
        field="is_group",
        header="Là nhóm",
        kind=CellKind.BOOLEAN,
        note="x / có / 1 = đây là nút nhóm, chỉ để gom, không hạch toán vào được.",
    ),
    ColumnDescriptor(
        field="is_active",
        header="Còn theo dõi",
        kind=CellKind.BOOLEAN,
        note="Để trống = còn theo dõi. x / không / 0 ở cột này nghĩa là đã ngừng theo dõi.",
    ),
)
"""Bộ cột chung của mọi danh mục, theo đúng thứ tự trên tệp mẫu.

`branch_id` **không** có mặt: chi nhánh là lựa chọn của cả lượt nhập chứ không
của từng dòng (FR-SYS-084 "nhập khẩu theo từng chi nhánh"), và một cột chi nhánh
trên từng dòng là đường để một tệp ghi vào chi nhánh mà người nhập không đứng ở
đó. `path`/`level`/`uid`/`row_version` cũng không: chúng là hệ quả tính ra được,
và một cột người dùng gõ được thì sớm muộn sẽ có người gõ vào."""


_PYDANTIC_KIND: Final[dict[type, CellKind]] = {
    str: CellKind.TEXT,
    int: CellKind.INTEGER,
    Decimal: CellKind.DECIMAL,
    bool: CellKind.BOOLEAN,
}


def _unwrap_optional(annotation: object) -> object:
    """`str | None` → `str`. Trả lại nguyên vẹn nếu không phải union có `None`.

    Cần vì gần như mọi cột riêng của danh mục đều `| None` — và kiểu của một ô
    Excel không đổi vì cột cho phép để trống. Việc "được để trống hay không" đã
    do `required` trả lời ở chỗ khác.
    """
    if get_origin(annotation) is None:
        return annotation
    members = [item for item in get_args(annotation) if item is not type(None)]
    return members[0] if len(members) == 1 else annotation


def _max_length_of(info: FieldInfo) -> int | None:
    """Trần độ dài khai trong `Field(max_length=...)` của model cột riêng.

    Đọc từ metadata của Pydantic thay vì chép lại con số: cột riêng khai trần
    của mình cạnh model ORM, và FR-SYS-083 đòi kiểm đúng trần đó. Hai chỗ giữ
    cùng một con số là hai chỗ để chúng lệch.
    """
    for item in info.metadata:
        limit = getattr(item, "max_length", None)
        if isinstance(limit, int):
            return limit
    return None


def _reference_for(spec: CatalogSpec, field: str) -> CatalogReference | None:
    return next((item for item in spec.references if item.field == field), None)


def _column_limits(spec: CatalogSpec, field: str) -> tuple[int | None, tuple[str, ...]]:
    """`(trần độ dài, tập giá trị hợp lệ)` đọc từ **cột thật** của bảng.

    Nguồn thứ hai sau `Field(max_length=...)` của Pydantic, và nó bắt đúng phần
    Pydantic bỏ sót: `swift_code` khai `String(11)` ở ORM nhưng không khai
    `max_length` ở model API, nên phép kiểm độ dài của bước kiểm không có trần để
    so — và giá trị dài quá chỉ đổ ở tầng DB, sau khi người dùng đã được báo
    "hợp lệ".
    """
    column = spec.model.__table__.columns.get(field)
    if column is None:
        return None, ()
    length = getattr(column.type, "length", None)
    enum_class = getattr(column.type, "enum_class", None)
    values = tuple(member.value for member in enum_class) if enum_class is not None else ()
    return (length if isinstance(length, int) else None), values


def _extra_column(spec: CatalogSpec, field: str, info: FieldInfo) -> ColumnDescriptor:
    """Một cột riêng của danh mục → một cột của tệp mẫu.

    Cột khóa ngoại (`base_unit_id`) thành cột **mã** (`base_unit_code`) theo H79.
    Nhận ra chúng bằng `CatalogSpec.references` chứ không bằng hậu tố `_id` của
    tên trường: registry là chỗ đã khai danh mục đích, và đoán theo tên thì một
    cột tên `parent_id` của một bảng không phải danh mục cũng bị đổi.
    """
    reference = _reference_for(spec, field)
    header = info.title or field
    if reference is not None:
        return ColumnDescriptor(
            field=f"{_reference_stem(field)}_code",
            # Nhãn lấy từ `title` của **chính trường** ("Kho ngầm định" → "Mã kho
            # ngầm định"), không phải tiêu đề của danh mục đích: một mã hàng có
            # thể trỏ vào `warehouses` với vai trò "kho ngầm định", còn chứng từ
            # ở phase 8 trỏ vào cùng bảng ấy với vai trò khác hẳn. Vai trò là thứ
            # người điền tệp cần đọc, không phải tên bảng đích.
            header=f"Mã {header.lower()}",
            kind=CellKind.CODE_REF,
            required=info.is_required(),
            max_length=CODE_MAX_LENGTH,
            reference_slug=reference.slug,
            target_field=field,
            note=info.description or "",
        )
    annotation = _unwrap_optional(info.annotation)
    kind = _PYDANTIC_KIND.get(annotation, CellKind.TEXT) if isinstance(annotation, type) else None
    column_length, allowed = _column_limits(spec, field)
    return ColumnDescriptor(
        field=field,
        header=header,
        kind=kind or CellKind.TEXT,
        required=info.is_required(),
        # Trần của Pydantic trước, trần của cột sau: model API có thể siết chặt
        # hơn cột, nhưng không bao giờ được lỏng hơn.
        max_length=_max_length_of(info) or column_length,
        allowed_values=allowed,
        note=info.description or "",
    )


def _reference_stem(field: str) -> str:
    """`base_unit_id` → `base_unit`. Trả nguyên nếu không có hậu tố `_id`."""
    return field[: -len("_id")] if field.endswith("_id") else field


def _sheet_name(spec: CatalogSpec) -> str:
    """Tên sheet dữ liệu = tiêu đề tiếng Việt của danh mục, cắt theo trần của xlsx.

    Cắt ở đây chứ không để Excel tự cắt: nếu Excel cắt thì tệp mẫu sinh ra mang
    một tên mà chính phép kiểm cấu trúc của chúng ta không nhận.
    """
    return spec.title[:SHEET_NAME_MAX_LENGTH]


def template_for(spec: CatalogSpec) -> TemplateDescriptor:
    """Tệp mẫu của một danh mục — bộ cột chung, rồi tới cột riêng.

    Thứ tự cố định (chung trước, riêng sau, mỗi nhóm theo thứ tự khai) vì nó là
    một phần của hợp đồng: người dùng dán dữ liệu theo vị trí cột, và một thứ tự
    đổi giữa hai bản phát hành làm hỏng mọi bảng tính họ đã dựng sẵn.
    """
    # `parent_code` trỏ về **chính danh mục này**, nên `reference_slug` của nó
    # chỉ biết được ở đây — `_COMMON_COLUMNS` là hằng số dùng chung cho cả hai
    # mươi danh mục nên nó không thể mang giá trị ấy sẵn. Không gắn thì
    # `sql.table_of(None)` ném `ValueError` ở **mọi** lượt nhập, vì cột nhóm cha
    # có mặt trong mọi tệp mẫu.
    columns = [
        replace(column, reference_slug=spec.slug) if column.field == "parent_code" else column
        for column in _COMMON_COLUMNS
    ]
    if spec.extra_fields is not None:
        columns.extend(
            _extra_column(spec, field, info)
            for field, info in spec.extra_fields.model_fields.items()
        )
    return TemplateDescriptor(
        slug=spec.slug,
        sheet_name=_sheet_name(spec),
        columns=tuple(columns),
    )


def instructions_for(
    spec: CatalogSpec, descriptor: TemplateDescriptor
) -> tuple[tuple[str, str], ...]:
    """Nội dung sheet **Hướng dẫn**: mỗi cột một dòng (FR-SYS-080).

    Sinh từ chính descriptor nên nó không thể lệch với sheet dữ liệu — một trang
    hướng dẫn viết tay sẽ mô tả cột mà tệp không còn có, và đó là kiểu tài liệu
    làm người đọc mất lòng tin vào cả phần còn lại.
    """
    rows = [
        (
            column.display_header,
            " ".join(
                part
                for part in (
                    "Bắt buộc." if column.required else "Có thể để trống.",
                    _kind_hint(column),
                    f"Tối đa {column.max_length} ký tự." if column.max_length else "",
                    column.note,
                )
                if part
            ),
        )
        for column in descriptor.columns
    ]
    return tuple(rows)


def _kind_hint(column: ColumnDescriptor) -> str:
    """Câu mô tả kiểu dữ liệu mong đợi, bằng tiếng Việt của người dùng cuối."""
    match column.kind:
        case CellKind.INTEGER:
            return "Số nguyên."
        case CellKind.DECIMAL:
            return "Số, dùng dấu chấm cho phần thập phân."
        case CellKind.BOOLEAN:
            return "Đánh x (hoặc để trống)."
        case CellKind.CODE_REF:
            return f"Mã lấy từ danh mục `{column.reference_slug}`."
        case CellKind.TEXT:
            return ""
