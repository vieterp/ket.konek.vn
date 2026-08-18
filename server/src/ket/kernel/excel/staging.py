"""Kiểm dữ liệu bằng **SQL tập hợp** trên bảng đệm (H82).

Đây là chỗ LD-14 ("tính khối lượng lớn phải set-based SQL trong PostgreSQL;
Python chỉ điều phối") có ý nghĩa nhất trong cả phase 3. Với 10.000 dòng của
FR-NFR-043, cách viết bằng Python là một vòng lặp gọi `resolve_by_code` cho mỗi
ô tra cứu — tức hàng chục nghìn lượt đi về DB, cộng một tầng cache tự viết để
bớt chúng, cộng cả phần làm cho cache đó đúng khi hai dòng cùng tệp tạo ra bản
ghi mà dòng sau tra tới. Bằng SQL thì cả ba biến mất: tra mã là một truy vấn
con, và mọi dòng lỗi về trong **một** lượt chạy kèm sẵn số dòng.

Mỗi phép kiểm là một `select` độc lập, kết quả gộp lại — nên thêm một luật là
thêm một hàm, không phải sửa một hàm đã dài. Mọi truy vấn dựng bằng biểu thức
Core; lý do (và nó là một lý do bảo mật, không phải thẩm mỹ) nằm ở `sql.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, Select, Text, cast, delete, exists, func, null, select
from sqlalchemy.orm import Session

from ket.kernel.excel.descriptors import CellKind, ColumnDescriptor, TemplateDescriptor
from ket.kernel.excel.models import ImportStagingRow
from ket.kernel.excel.report import ImportMode, RowError
from ket.kernel.excel.sql import (
    FALSE_WORDS,
    TRUE_WORDS,
    as_text,
    boolean_expression,
    cell,
    cell_or_null,
    code_matches,
    counts_expression,
    create_only_columns,
    first_occurrence_rank,
    reference_id,
    table_for,
    table_of,
    truthy,
    visible_to,
)
from ket.kernel.identifiers import uuid7
from ket.kernel.master_data.registry import CatalogSpec

INSERT_BATCH: Final[int] = 500
"""Số dòng mỗi lượt `executemany` khi đổ vào bảng đệm.

Bằng con số phase-03 bước 15 đặt ra. Lô lớn hơn không nhanh thêm đáng kể (chi
phí đã chuyển sang phía PostgreSQL) nhưng giữ nhiều dòng hơn trong bộ nhớ tiến
trình worker cùng lúc."""

_TYPE_PATTERNS: Final[dict[CellKind, str]] = {
    CellKind.INTEGER: r"^-?[0-9]+$",
    CellKind.DECIMAL: r"^-?[0-9]+([.][0-9]+)?$",
    CellKind.BOOLEAN: "^(" + "|".join(TRUE_WORDS + FALSE_WORDS) + ")$",
}
"""Định dạng chấp nhận được cho từng kiểu ô.

`TEXT` và `CODE_REF` vắng mặt có chủ đích: chữ thì không có định dạng để kiểm
(độ dài đã do `_length_errors` lo), còn mã tra cứu thì phép kiểm thật của nó là
"có tra ra không" — một biểu thức chính quy trên mã chỉ loại thêm được những mã
sai *hình thức*, mà danh mục không có luật hình thức nào cho mã."""

_TYPE_HINTS: Final[dict[CellKind, str]] = {
    CellKind.INTEGER: "phải là số nguyên",
    CellKind.DECIMAL: "phải là số (dùng dấu chấm cho phần thập phân)",
    CellKind.BOOLEAN: "chỉ nhận x, có, không (hoặc để trống)",
}


class StagingTable:
    """Vòng đời bảng đệm của **một** lượt nhập, khóa theo `job_id`.

    Là một lớp chứ vài hàm rời vì `job_id` đi vào mọi truy vấn, và một tham số
    lặp lại ở mười chỗ là mười cơ hội để một chỗ quên nó — mà chỗ quên nó sẽ đọc
    hoặc **xóa** dòng của một lượt nhập khác đang chạy song song.
    """

    def __init__(self, session: Session, job_id: UUID, descriptor: TemplateDescriptor) -> None:
        self._session = session
        self._job_id = job_id
        self._descriptor = descriptor

    @property
    def _mine(self) -> ColumnElement[bool]:
        """Điều kiện "dòng của lượt nhập này" — mọi truy vấn bắt đầu bằng nó."""
        return ImportStagingRow.job_id == self._job_id

    # ------------------------------------------------------------------ nạp

    def load(self, rows: Iterable[tuple[int, dict[str, str | None]]]) -> int:
        """Đổ dòng thô vào bảng đệm theo lô. Trả về số dòng đã nạp.

        `uid` sinh **ở đây**, một giá trị cho mỗi dòng, chứ không bằng một hàm
        SQL trong câu `INSERT` của bước ghi. Lý do là RT-19: `MasterDataRow.uid`
        phải là UUIDv7 (tăng dần theo thời gian, để nối dữ liệu nhiều bản cài về
        sau), mà PostgreSQL 16 chỉ có `gen_random_uuid()` — sinh ra v4 ngẫu nhiên
        đều. Nó sẽ chạy trót lọt, không lỗi, và mất đúng tính chất mà cột này tồn
        tại vì nó. `tests/test_uuid7_identifiers.py` canh `version == 7`, nhưng
        nó canh đường ghi thông thường; nhập liệu là đường thứ hai.
        """
        total = 0
        batch: list[dict[str, object]] = []
        for row_number, values in rows:
            batch.append(
                {
                    "job_id": self._job_id,
                    "row_number": row_number,
                    "uid": uuid7(),
                    "cells": values,
                }
            )
            if len(batch) >= INSERT_BATCH:
                total += self._flush(batch)
                batch = []
        total += self._flush(batch)
        return total

    def _flush(self, batch: list[dict[str, object]]) -> int:
        if not batch:
            return 0
        self._session.execute(table_for(ImportStagingRow).insert(), batch)
        return len(batch)

    def clear(self) -> None:
        """Xóa dòng đệm của **lượt này**.

        Gọi ở cuối mỗi job, kể cả job hỏng: bảng đệm là nơi duy nhất trong dataset
        giữ một bản sao nguyên văn dữ liệu người dùng ngoài chính bản ghi nghiệp
        vụ, nên để nó tồn đọng là để một bản sao không ai quản lý nằm lại trong DB.
        """
        self._session.execute(delete(ImportStagingRow).where(self._mine))

    # ---------------------------------------------------------------- kiểm

    def errors(
        self, spec: CatalogSpec, *, mode: ImportMode, branch_id: int | None
    ) -> Iterator[RowError]:
        """Mọi sai sót của lượt nhập, gộp từ các phép kiểm độc lập.

        Thứ tự: lỗi *hình thức* (thiếu, quá dài, sai kiểu) trước lỗi *quan hệ*
        (mã không tra được, mã trùng). Người dùng sửa được nhóm đầu mà không cần
        biết gì về dữ liệu đang có trong hệ thống, nên đọc danh sách từ trên
        xuống cũng là thứ tự việc phải làm.
        """
        for column in self._descriptor.columns:
            yield from self._required_errors(column)
            yield from self._length_errors(column)
            yield from self._type_errors(column)
            yield from self._allowed_value_errors(column)
        yield from self._duplicate_in_file_errors()
        yield from self._reference_errors(branch_id)
        yield from self._existing_code_errors(spec, mode=mode, branch_id=branch_id)

    def _rows(self, statement: Select[tuple[int, str | None]]) -> Sequence[tuple[int, str | None]]:
        return [(int(row[0]), row[1]) for row in self._session.execute(statement)]

    def _base(self, value: ColumnElement[Any] | None = None) -> Select[tuple[int, str | None]]:
        """Khung chung của mọi truy vấn kiểm: số dòng + một giá trị để trích dẫn.

        Mọi phép kiểm trả về cùng hình dạng nên `_rows` chỉ có một cách đọc, và
        thứ tự `ORDER BY row_number` được đặt **ở đây** — báo cáo lỗi phải theo
        thứ tự người dùng thấy trong Excel, và để mỗi phép kiểm tự sắp là để một
        phép kiểm quên sắp.
        """
        selected = value if value is not None else cast(null(), Text())
        return (
            select(ImportStagingRow.row_number, selected)
            .where(self._mine)
            .order_by(ImportStagingRow.row_number)
        )

    def _required_errors(self, column: ColumnDescriptor) -> Iterator[RowError]:
        if not column.required:
            return
        statement = self._base().where(cell_or_null(column.field).is_(None))
        for row_number, _ in self._rows(statement):
            yield RowError(
                row=row_number,
                column=column.display_header,
                code="import.required",
                message=f"Cột {column.header!r} bắt buộc nhập",
            )

    def _length_errors(self, column: ColumnDescriptor) -> Iterator[RowError]:
        if column.max_length is None:
            return
        statement = self._base(cell(column.field)).where(
            func.char_length(cell(column.field)) > column.max_length
        )
        for row_number, value in self._rows(statement):
            yield RowError(
                row=row_number,
                column=column.display_header,
                code="import.too_long",
                message=f"Cột {column.header!r} tối đa {column.max_length} ký tự",
                value=value,
            )

    def _type_errors(self, column: ColumnDescriptor) -> Iterator[RowError]:
        """Ô không ép được về kiểu của cột.

        Kiểm bằng biểu thức chính quy của PostgreSQL thay vì thử `int(...)` trong
        Python: phép kiểm phải chạy trên cả tập cùng lúc, và biểu thức chính quy
        nói rõ **định dạng được chấp nhận** nên câu thông báo lỗi mô tả được nó.
        """
        pattern = _TYPE_PATTERNS.get(column.kind)
        if pattern is None:
            return
        statement = (
            self._base(cell(column.field))
            .where(cell_or_null(column.field).is_not(None))
            .where(truthy(~func.lower(cell(column.field)).regexp_match(pattern)))
        )
        for row_number, value in self._rows(statement):
            yield RowError(
                row=row_number,
                column=column.display_header,
                code=f"import.not_{column.kind.value}",
                message=f"Cột {column.header!r}: {_TYPE_HINTS[column.kind]}",
                value=value,
            )

    def _allowed_value_errors(self, column: ColumnDescriptor) -> Iterator[RowError]:
        """Ô mang một giá trị ngoài tập cho phép của cột enum (review vòng 2, R2-1).

        Đây là phép kiểm có hậu quả xa nhất trong cả tệp. `items.nature` lưu ở
        `varchar` nên một giá trị bịa **ghi được**; nhưng SQLAlchemy đọc cột đó
        thành `ItemNature`, nên sau đó **mọi** lượt đọc `Item` qua ORM ném
        `LookupError` — `GET /api/v1/master/items` hỏng cho cả dữ liệu kế toán, và
        bản ghi sai thì không sửa hay xóa được từ giao diện, vì mọi đường sửa cũng
        phải đọc nó lên trước.

        Tập giá trị đọc từ **kiểu cột thật** (`descriptors._column_limits`) nên
        thêm một tính chất ở phase sau không phải nhớ cập nhật chỗ này.
        """
        if not column.allowed_values:
            return
        for row_number, value in self._rows(
            self._base(cell(column.field))
            .where(cell_or_null(column.field).is_not(None))
            .where(cell(column.field).notin_(column.allowed_values))
        ):
            yield RowError(
                row=row_number,
                column=column.display_header,
                code="import.value_not_allowed",
                message=f"Cột {column.header!r} chỉ nhận: " + ", ".join(column.allowed_values),
                value=value,
            )

    def _duplicate_in_file_errors(self) -> Iterator[RowError]:
        """Hai dòng **trong cùng tệp** mang cùng mã.

        Kiểm riêng khỏi "mã đã tồn tại trong hệ thống" vì cách sửa khác hẳn: ở
        đây người dùng xóa một trong hai dòng, còn ở kia họ đổi mã hoặc bật chế
        độ cập nhật. Gộp hai thứ vào một thông báo là bắt họ tự đoán mình đang
        gặp cái nào.
        """
        ranked = (
            select(
                ImportStagingRow.row_number.label("row_number"),
                cell("code").label("code"),
                first_occurrence_rank().label("seen"),
            )
            .where(self._mine)
            .where(cell_or_null("code").is_not(None))
            .subquery()
        )
        statement = (
            select(ranked.c.row_number, ranked.c.code)
            .where(ranked.c.seen > 1)
            .order_by(ranked.c.row_number)
        )
        for row_number, value in self._rows(statement):
            yield RowError(
                row=row_number,
                column="Mã *",
                code="import.duplicate_in_file",
                message=f"Mã {value!r} xuất hiện nhiều lần trong tệp",
                value=value,
            )

    def _reference_errors(self, branch_id: int | None) -> Iterator[RowError]:
        """Ô tra cứu mang một mã không tìm thấy (H79).

        Mã nhóm cha được phép trỏ tới **một dòng khác trong cùng tệp**: người
        dùng khai cả cây trong một lần nhập, và bắt họ tạo nhóm trước bằng một
        lượt nhập riêng là chia đôi một việc vốn liền mạch. Khóa ngoại tới danh
        mục *khác* thì không có ngoại lệ đó — bảng kia không nằm trong tệp này.
        """
        for column in self._descriptor.references:
            target = table_of(column.reference_slug)
            in_catalog = exists(
                select(target.c.id)
                .where(target.c.code == cell(column.field))
                .where(visible_to(target, branch_id))
            )
            statement = (
                self._base(cell(column.field))
                .where(cell_or_null(column.field).is_not(None))
                .where(~in_catalog)
            )
            if column.target_field == "parent_id":
                statement = statement.where(~self._declared_in_file(column))
            for row_number, value in self._rows(statement):
                yield RowError(
                    row=row_number,
                    column=column.display_header,
                    code="import.reference_not_found",
                    message=f"Không tìm thấy mã {value!r} trong danh mục {column.reference_slug!r}",
                    value=value,
                )

    def _declared_in_file(self, column: ColumnDescriptor) -> ColumnElement[bool]:
        """ "Có dòng nào khác trong chính tệp này khai mã đó không".

        Bí danh riêng cho bảng đệm (`aliased`-style qua `__table__.alias()`) vì
        truy vấn con tham chiếu **cùng** bảng với truy vấn ngoài: không có bí
        danh thì điều kiện `job_id` của truy vấn con che mất điều kiện bên ngoài
        và phép so mã trở thành "dòng này khai mã của chính nó".
        """
        other = table_for(ImportStagingRow).alias("declared")
        return exists(
            select(other.c.id)
            .where(other.c.job_id == self._job_id)
            .where(func.btrim(other.c["cells"]["code"].astext) == cell(column.field))
        )

    def _existing_code_errors(
        self, spec: CatalogSpec, *, mode: ImportMode, branch_id: int | None
    ) -> Iterator[RowError]:
        """Mã đã có trong danh mục — lỗi hay không tùy chế độ (H80, H81)."""
        if mode is ImportMode.CREATE_AND_UPDATE:
            yield from self._create_only_field_errors(spec, branch_id=branch_id)
            return
        table = table_for(spec.model)
        already_there = exists(select(table.c.id).where(code_matches(table, branch_id)))
        statement = self._base(cell("code")).where(already_there)
        for row_number, value in self._rows(statement):
            yield RowError(
                row=row_number,
                column="Mã *",
                code="import.code_exists",
                message=f"Mã {value!r} đã có trong danh mục — chọn chế độ "
                "'tạo mới và cập nhật' nếu muốn ghi đè",
                value=value,
            )

    def _create_only_field_errors(
        self, spec: CatalogSpec, *, branch_id: int | None
    ) -> Iterator[RowError]:
        """Trường chốt một lần mang giá trị khác bản ghi đang có (H81).

        `nature` và `base_unit_id` của vật tư hàng hóa là hai bất biến mà H69 và
        H73 đã canh ở đường sửa và đường gộp. Bảng tính là đường ghi **thứ ba**,
        và nếu nó không bị canh thì hai phép kiểm kia chỉ còn là nghi thức: đổi
        đơn vị chính của một mã hàng chỉ cần một lượt nhập ở chế độ cập nhật.
        """
        table = table_for(spec.model)
        for column in create_only_columns(spec, self._descriptor):
            # So **giá trị đã diễn giải**, không phải chuỗi thô: một ô boolean
            # ghi `x` và cột đang lưu `true` là cùng một giá trị, nhưng
            # `'x' <> 'true'` về mặt chuỗi. So thô ở đây sẽ biến mọi lượt nhập
            # lại đúng tệp vừa xuất thành một danh sách lỗi.
            staged: ColumnElement[Any]
            if column.kind is CellKind.CODE_REF:
                staged = reference_id(column, branch_id)
            elif column.kind is CellKind.BOOLEAN:
                staged = boolean_expression(column.field)
            else:
                staged = cell_or_null(column.field)
            current = table.c[column.target_field or column.field]
            statement = (
                self._base(cell(column.field))
                .join(table, code_matches(table, branch_id))
                # Ô để trống = "không nói gì về cột này" (H87), nên nó không
                # bao giờ là một lần đổi. Với cột boolean thì "để trống" đã có
                # nghĩa riêng (`false`), nên nó được xét như mọi giá trị khác.
                .where(
                    boolean_expression(column.field).is_not(None)
                    if column.kind is CellKind.BOOLEAN
                    else cell_or_null(column.field).is_not(None)
                )
                .where(as_text(staged).is_distinct_from(as_text(current)))
            )
            for row_number, value in self._rows(statement):
                yield RowError(
                    row=row_number,
                    column=column.display_header,
                    code="import.create_only_field_changed",
                    message=f"Cột {column.header!r} chỉ khai được lúc tạo mới, "
                    "không sửa được bằng nhập liệu",
                    value=value,
                )

    # ---------------------------------------------------------------- đếm

    def counts(self, spec: CatalogSpec, *, branch_id: int | None) -> tuple[int, int]:
        """`(số dòng sẽ tạo mới, số dòng sẽ cập nhật)` — con số của H80.

        Đếm bằng chính phép so mã mà bước ghi dùng (`code_matches`), nên báo cáo
        nói đúng thứ sắp xảy ra. Một phép đếm gần đúng ở đây tệ hơn không đếm:
        người dùng bấm ghi **vì** con số đó.
        """
        table = table_for(spec.model)
        to_create, to_update = counts_expression(table.c.id)
        row = self._session.execute(
            select(to_create, to_update)
            .select_from(ImportStagingRow)
            .outerjoin(table, code_matches(table, branch_id))
            .where(self._mine)
        ).one()
        return int(row[0]), int(row[1])
