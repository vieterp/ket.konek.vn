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

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Column,
    ColumnElement,
    Integer,
    Numeric,
    Select,
    Table,
    Text,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    null,
    or_,
    select,
)
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
    column_source,
    counts_expression,
    create_only_columns,
    editable_columns,
    first_occurrence_rank,
    keep_existing,
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


@dataclass(frozen=True)
class _StagedValues:
    """Khung nhìn `row_rules.StagedRow` trên một dòng đệm — giá trị **sẽ được ghi**.

    Dựng ở tầng `excel` chứ không ở `master_data` vì nó phải gọi `column_source`
    và `keep_existing`, mà cả hai sống trong `excel/sql.py`. Chiều import ngược
    lại (`master_data` → `excel`) là một vòng: `excel/sql.py` đã import registry
    danh mục để tra bảng đích của một mã. `StagedRow` là Protocol nên luật khai ở
    `master_data` không cần biết lớp này tồn tại.
    """

    descriptor: TemplateDescriptor
    table: Table
    branch_id: int | None
    autocreate: frozenset[str]
    """Danh mục đích sẽ được **tự tạo** ở bước ghi (FR-NFR-062).

    Luật liên-trường phải biết tập này, nếu không nó mô tả sai tương lai: một mã
    đơn vị tính chưa tồn tại tra ra `NULL` ở thời điểm kiểm, nên
    `stock_item_needs_base_unit` báo "hàng hóa phải có đơn vị tính chính" cho một
    dòng **đã** điền đơn vị tính — và bước ghi thì sẽ tạo đơn vị ấy rồi gắn vào
    bình thường. Câu báo lỗi ấy vừa sai vừa đẩy người dùng đi sửa nhầm chỗ.
    """

    updating: bool
    """Chế độ có cho phép cập nhật không.

    Ở `CREATE_ONLY` thì mọi dòng là dòng mới — kể cả dòng khớp một mã đã có, vì
    dòng ấy đã là một dòng **lỗi** (`_existing_code_errors`). Dùng
    `keep_existing` cho nó sẽ đánh giá luật trên giá trị của một bản ghi mà lượt
    nhập này không hề định chạm tới.
    """

    def _projected(self, field: str) -> ColumnElement[Any]:
        column = self.descriptor.column(field)
        if column is None:
            raise ValueError(f"Luật nhắc tới cột {field!r} không có trong tệp mẫu")
        target_name = column.target_field or column.field
        target = self.table.c.get(target_name)
        if target is None:
            raise ValueError(f"Cột {target_name!r} không có trong bảng {self.table.name!r}")
        projected = self._guarded(column, self._pending_aware(column, target))
        if not self.updating:
            return projected
        # Dòng đã có bản ghi → giá trị hiệu lực là thứ `keep_existing` cho ra;
        # dòng chưa có → `table.c.id` là `NULL` sau `LEFT JOIN`, và giá trị hiệu
        # lực là ngầm định của cột như mọi dòng tạo mới.
        return case(
            (self.table.c.id.is_(None), projected),
            else_=self._guarded(column, keep_existing(column, self.branch_id, target, widen=True)),
        )

    def _guarded(self, column: ColumnDescriptor, value: ColumnElement[Any]) -> ColumnElement[Any]:
        """Bọc một phép ép số trong `CASE` để nó **không bao giờ chạy trên ô hỏng**.

        Đây là vế còn lại của C1/R3-C1, và là vế duy nhất đóng được nó cho mọi
        đầu vào. `widen=True` bỏ được ca **tràn số**, nhưng `CAST('ba mươi' AS
        numeric)` vẫn ném `InvalidTextRepresentation` — mà truy vấn luật chạy
        trên **mọi** dòng đệm, kể cả dòng vừa bị `_type_errors` báo là sai.

        Lọc bằng danh sách số dòng (`NOT IN (…)`) thì đúng nhưng vỡ ở >65.535
        dòng lỗi vì mỗi dòng là một tham số ràng buộc, trong khi `reader.MAX_ROWS`
        cho phép 100.000 (R3-H1). `CASE` không cần biết dòng nào hỏng: nhánh
        không được chọn thì không được đánh giá, nên phép ép chỉ chạy trên ô đúng
        hình thức — và số dòng lỗi không còn ảnh hưởng gì tới truy vấn.

        Ô **để trống** vẫn đi vào nhánh ép: `column_source` đổi nó thành ngầm
        định của cột (`sql._with_default`), và cắt nó ra sẽ làm luật đánh giá
        `NULL` cho một dòng mà bước ghi sẽ điền `0`.
        """
        if column.kind not in {CellKind.INTEGER, CellKind.DECIMAL}:
            return value
        pattern = _TYPE_PATTERNS[column.kind]
        shaped = or_(
            cell_or_null(column.field).is_(None),
            truthy(func.lower(cell(column.field)).regexp_match(pattern)),
        )
        return case((shaped, value), else_=null())

    def _pending_aware(self, column: ColumnDescriptor, target: Column[Any]) -> ColumnElement[Any]:
        """Giá trị của một cột, coi mã **sắp được tạo** là đã có.

        `PENDING_REFERENCE_ID` không phải một id thật và không bao giờ được ghi
        xuống DB — nó chỉ đi vào biểu thức của một luật, và mọi luật hôm nay chỉ
        hỏi cột tra cứu một câu: *"có hay không"*. Giá trị âm để nếu có luật nào
        về sau đem nó đi so với một id thật thì kết quả sai một cách ồn ào chứ
        không lặng lẽ trùng với bản ghi số 0.
        """
        source = column_source(column, self.branch_id, target, widen=True)
        if column.kind is not CellKind.CODE_REF or column.reference_slug not in self.autocreate:
            return source
        return case(
            (cell_or_null(column.field).is_not(None), literal(PENDING_REFERENCE_ID)),
            else_=source,
        )

    def value(self, field: str) -> ColumnElement[Any]:
        return self._projected(field)

    def flag(self, field: str) -> ColumnElement[bool]:
        """Cột boolean. `cast` để mypy biết kiểu — `case(...)` trả `Any`."""
        return cast(self._projected(field), Boolean())


INTEGER_MAX: Final[int] = 2_147_483_647
"""Trần của cột `integer` 32 bit trong PostgreSQL.

So với trần **thật** được vì phép so chạy trên `numeric` không ràng buộc
(`sql.widened`), nên nó không bao giờ là chỗ tràn — bản đầu phải đếm chữ số và
vì thế chặn nhầm cả những giá trị hợp lệ từ 1.000.000.000 trở lên."""

PENDING_REFERENCE_ID: Final[int] = -1
"""Chỗ đứng cho một khóa ngoại **sẽ** tồn tại sau bước ghi — xem `_StagedValues`."""


class StagingTable:
    """Vòng đời bảng đệm của **một** lượt nhập, khóa theo `job_id`.

    Là một lớp chứ vài hàm rời vì `job_id` đi vào mọi truy vấn, và một tham số
    lặp lại ở mười chỗ là mười cơ hội để một chỗ quên nó — mà chỗ quên nó sẽ đọc
    hoặc **xóa** dòng của một lượt nhập khác đang chạy song song.
    """

    def __init__(
        self,
        session: Session,
        job_id: UUID,
        descriptor: TemplateDescriptor,
        catalog_table: Table,
    ) -> None:
        self._session = session
        self._job_id = job_id
        self._descriptor = descriptor
        self._catalog_table = catalog_table
        """Bảng danh mục đích, để `_range_errors` đọc được `precision`/`scale` của
        cột thật.

        **Bắt buộc**, không có ngầm định: với `None` thì `_magnitude_bound` trả
        `None` và **toàn bộ `_range_errors` tắt trong im lặng** — tức lỗi C1 quay
        lại mà không cổng nào đỏ. Một tham số bắt buộc biến chỗ quên thành lỗi
        lúc gọi (R3-L1)."""

    @property
    def _mine(self) -> ColumnElement[bool]:
        """Điều kiện "dòng của lượt nhập này" — mọi truy vấn bắt đầu bằng nó."""
        return ImportStagingRow.job_id == self._job_id

    # ------------------------------------------------------------------ nạp

    def load(
        self,
        rows: Iterable[tuple[int, dict[str, str | None]]],
        *,
        on_batch: Callable[[int], None] | None = None,
    ) -> int:
        """Đổ dòng thô vào bảng đệm theo lô. Trả về số dòng đã nạp.

        `uid` sinh **ở đây**, một giá trị cho mỗi dòng, chứ không bằng một hàm
        SQL trong câu `INSERT` của bước ghi. Lý do là RT-19: `MasterDataRow.uid`
        phải là UUIDv7 (tăng dần theo thời gian, để nối dữ liệu nhiều bản cài về
        sau), mà PostgreSQL 16 chỉ có `gen_random_uuid()` — sinh ra v4 ngẫu nhiên
        đều. Nó sẽ chạy trót lọt, không lỗi, và mất đúng tính chất mà cột này tồn
        tại vì nó. `tests/test_uuid7_identifiers.py` canh `version == 7`, nhưng
        nó canh đường ghi thông thường; nhập liệu là đường thứ hai.

        `on_batch` được gọi sau **mỗi lô** với tổng số dòng đã nạp — đây là ranh
        giới lô mà thân job dùng để gia hạn lease và kiểm cờ hủy (audit phase
        1–3, C1/C2): vòng đọc tệp là phần chạy lâu nhất của cả lượt nhập, và nó
        là vòng lặp Python duy nhất nên chỉ nó mới có "ranh giới" để đứng.
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
                if on_batch is not None:
                    on_batch(total)
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
        self,
        spec: CatalogSpec,
        *,
        mode: ImportMode,
        branch_id: int | None,
        autocreate: frozenset[str] = frozenset(),
        on_check: Callable[[], None] | None = None,
    ) -> Iterator[RowError]:
        """Mọi sai sót của lượt nhập, gộp từ các phép kiểm độc lập.

        Thứ tự: lỗi *hình thức* (thiếu, quá dài, sai kiểu) trước lỗi *quan hệ*
        (mã không tra được, mã trùng). Người dùng sửa được nhóm đầu mà không cần
        biết gì về dữ liệu đang có trong hệ thống, nên đọc danh sách từ trên
        xuống cũng là thứ tự việc phải làm.

        `on_check` được gọi trước **mỗi nhóm phép kiểm** — cùng hợp đồng nhịp
        với `load(on_batch=…)`: pha kiểm là một chuỗi câu SQL tập hợp, và trên
        tệp cây lớn thì mỗi câu (EXISTS tương quan trên jsonb bảng đệm) đủ dài
        để cả pha vượt lease nếu không có nhịp nào ở giữa (review lát vá, M1).
        Một câu **đơn lẻ** vượt lease thì nhịp nào cũng không cứu — hàng rào
        trước-commit của worker nhận phần đó.
        """

        def _pulse() -> None:
            if on_check is not None:
                on_check()

        # Vắt cạn **trước**, không `yield` dần: tập dòng đã lỗi phải đầy đủ trước
        # khi truy vấn của `_row_rule_errors` chạy, vì phép loại trừ nay nằm
        # trong chính câu SQL đó chứ không ở Python.
        #
        # Vì sao bắt buộc phải thế (review vòng 2, C1): truy vấn luật liên-trường
        # `CAST(ô AS integer)` trên **mọi** dòng đệm. Một ô `Số ngày được nợ` gõ
        # `"1.000"` hay `"ba mươi"` — lỗi gõ số kiểu Việt Nam phổ biến nhất — làm
        # PostgreSQL ném `InvalidTextRepresentation`, hỏng transaction, và
        # `finally: staging.clear()` ném tiếp `InFailedSqlTransaction` **đè lên**
        # nguyên nhân gốc. Người dùng mất **toàn bộ** danh sách lỗi đã tính đúng
        # và nhận một câu không nói được ô nào sai. Lọc ở Python sau khi truy vấn
        # đã chạy thì đã muộn.
        earlier: list[RowError] = []
        for column in self._descriptor.columns:
            _pulse()
            earlier.extend(self._required_errors(column))
            earlier.extend(self._length_errors(column))
            earlier.extend(self._type_errors(column))
            earlier.extend(self._range_errors(column))
            earlier.extend(self._allowed_value_errors(column))
        _pulse()
        earlier.extend(self._duplicate_in_file_errors())
        _pulse()
        earlier.extend(self._reference_errors(branch_id, autocreate=autocreate))
        _pulse()
        earlier.extend(self._parent_not_group_errors(branch_id))
        _pulse()
        earlier.extend(self._existing_code_errors(spec, mode=mode, branch_id=branch_id))
        _pulse()
        earlier.extend(self._shared_record_errors(spec, mode=mode, branch_id=branch_id))
        _pulse()
        earlier.extend(self._reference_moved_errors(spec, mode=mode, branch_id=branch_id))
        flagged = {error.row for error in earlier}
        yield from earlier
        _pulse()
        # Luật liên-trường chạy **cuối**, và bỏ qua dòng đã có lỗi khác: chúng
        # đọc *giá trị hiệu lực*, nên một ô hỏng ở trên biến thành `NULL` ở đây
        # và kéo theo một lỗi thứ hai mô tả **hệ quả** thay vì nguyên nhân. Ca
        # cụ thể: mã đơn vị tính tra không ra → `base_unit_code` thành `NULL` →
        # "hàng hóa phải có đơn vị tính chính". Câu thứ hai ấy nói sai sự thật —
        # người dùng **đã** điền đơn vị tính — và nó đẩy họ đi sửa nhầm chỗ.
        # Lọc ở **Python**, và nay đó là lựa chọn về **chất lượng thông điệp**
        # chứ không về tính đúng: phép ép kiểu của luật dùng `widen=True` nên
        # truy vấn không thể nổ dù dòng nào có mặt (R3-C1). Lọc bằng
        # `NOT IN (…)` trong SQL thì mỗi dòng lỗi là **một tham số ràng buộc**,
        # và trần giao thức PostgreSQL là 65.535 trong khi `reader.MAX_ROWS` là
        # 100.000 — hai trần mâu thuẫn nhau (R3-H1).
        for error in self._row_rule_errors(
            spec, mode=mode, branch_id=branch_id, autocreate=autocreate
        ):
            if error.row not in flagged:
                yield error

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

    def _range_errors(self, column: ColumnDescriptor) -> Iterator[RowError]:
        """Ô **đúng hình thức** nhưng không nằm vừa cột đích (review vòng 2 C1, vòng 3 R3-C1).

        `_type_errors` chỉ hỏi "có phải một con số không". Một giá trị vượt tầm
        cột trả lời **có**, rồi làm câu ghi nổ với `NumericValueOutOfRange`.

        Mô hình **đúng** cách PostgreSQL kiểm `numeric(p, s)`: nó **làm tròn theo
        `s` trước**, rồi mới đòi phần nguyên không quá `p − s` chữ số. Bản đầu
        đếm chữ số phần nguyên của chuỗi *trước* khi ép, nên `"999.99999"` vào
        `Numeric(7, 4)` lọt qua (ba chữ số, đúng trần) rồi tràn sau khi làm tròn
        thành `1000.0000`. Phép so ở đây vì thế `round(…, s)` trước khi so với
        `10^(p−s)` — cùng thứ tự với DB.

        Ép về `numeric` **không ràng buộc** để chính phép kiểm này không bao giờ
        là chỗ tràn (xem `sql.widened`).
        """
        if column.kind not in {CellKind.INTEGER, CellKind.DECIMAL}:
            return
        bound = self._magnitude_bound(column)
        if bound is None:
            return
        limit, scale = bound
        pattern = _TYPE_PATTERNS[column.kind]
        value = func.abs(cast(cell_or_null(column.field), Numeric()))
        statement = (
            self._base(cell(column.field))
            .where(cell_or_null(column.field).is_not(None))
            # **Chỉ** dòng đã qua phép kiểm hình thức: phép ép ở đây tuy không
            # tràn được (`Numeric()` không precision) nhưng vẫn ném với một ô
            # không phải số — tức chính phép kiểm miền lại thành chỗ nuốt cả
            # báo cáo lỗi, đúng vòng lặp mà C1/R3-C1 đang đóng.
            .where(truthy(func.lower(cell(column.field)).regexp_match(pattern)))
            .where(func.round(value, scale) >= limit)
        )
        for row_number, cell_value in self._rows(statement):
            yield RowError(
                row=row_number,
                column=column.display_header,
                code="import.out_of_range",
                message=f"Cột {column.header!r}: giá trị vượt sức chứa của cột",
                value=cell_value,
            )

    def _magnitude_bound(self, column: ColumnDescriptor) -> tuple[Decimal, int] | None:
        """`(ngưỡng, số chữ số thập phân)` của cột đích, hoặc `None` nếu không rõ.

        Trả về **ngưỡng** chứ không số chữ số: phép so `round(|x|, s) >= 10^(p−s)`
        diễn đạt đúng luật của PostgreSQL, còn đếm chữ số thì không mô tả được
        phần làm tròn.
        """
        target = self._table_column(column)
        if target is None:
            return None
        precision = getattr(target.type, "precision", None)
        scale = getattr(target.type, "scale", None)
        if isinstance(precision, int) and isinstance(scale, int):
            return Decimal(10) ** (precision - scale), scale
        if isinstance(target.type, Integer):
            # `integer` 32 bit: trần thật là 2.147.483.647. So với trần **thật**
            # chứ không một mức chữ số tròn — giá trị hợp lệ không bị chặn nhầm.
            return Decimal(INTEGER_MAX + 1), 0
        return None

    def _table_column(self, column: ColumnDescriptor) -> Column[Any] | None:
        """Cột thật mà một cột của tệp mẫu sẽ ghi vào."""
        return self._catalog_table.c.get(column.target_field or column.field)

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

    def _reference_errors(
        self, branch_id: int | None, *, autocreate: frozenset[str] = frozenset()
    ) -> Iterator[RowError]:
        """Ô tra cứu mang một mã không tìm thấy (H79).

        Mã nhóm cha được phép trỏ tới **một dòng khác trong cùng tệp**: người
        dùng khai cả cây trong một lần nhập, và bắt họ tạo nhóm trước bằng một
        lượt nhập riêng là chia đôi một việc vốn liền mạch. Khóa ngoại tới danh
        mục *khác* thì không có ngoại lệ đó — bảng kia không nằm trong tệp này.
        """
        for column in self._descriptor.references:
            if column.reference_slug in autocreate and column.target_field != "parent_id":
                # Mã thiếu ở danh mục này sẽ được tự tạo (FR-NFR-062), nên nó
                # không còn là lỗi. `missing_references.eligible_columns` là chỗ
                # quyết định cột nào đủ điều kiện, và `autocreate` chính là kết
                # quả của nó — hai chỗ đọc cùng một tập nên không lệch được.
                continue
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

    def _parent_not_group_errors(self, branch_id: int | None) -> Iterator[RowError]:
        """Mã nhóm cha trỏ vào một nút **lá** — cùng luật với `MasterDataService`.

        `_ensure_parent_is_group` chặn đường tạo/chuyển cha trên API (audit phase
        1–3, H2 trục kernel); nhập Excel là đường ghi cây thứ hai và không đi qua
        service, nên thiếu phép kiểm này thì nó là đường vòng quanh đúng luật đó.

        Hai đường phân giải của mã nhóm cha, hai phép kiểm:

        * mã **đã có trong danh mục** — bước ghi sẽ nối vào bản ghi đó, nên nó
          phải là nhóm;
        * mã **chỉ khai trong tệp** — bước ghi sẽ tạo nó ở vòng trước rồi nối
          con vào, nên chính dòng khai nó phải đánh dấu "Là nhóm". Ô bỏ trống
          nghĩa là "không phải nhóm" (cùng cách đọc với `boolean_expression`).
        """
        for column in self._descriptor.references:
            if column.target_field != "parent_id":
                continue
            target = table_of(column.reference_slug)
            visible_parent = and_(
                target.c.code == cell(column.field), visible_to(target, branch_id)
            )
            points_at_leaf_in_catalog = exists(
                select(target.c.id).where(visible_parent).where(target.c.is_group.is_(False))
            )
            statement = (
                self._base(cell(column.field))
                .where(cell_or_null(column.field).is_not(None))
                .where(points_at_leaf_in_catalog)
            )
            for row_number, value in self._rows(statement):
                yield RowError(
                    row=row_number,
                    column=column.display_header,
                    code="import.parent_not_group",
                    message=f"Mã nhóm cha {value!r} trỏ vào một bản ghi không phải nhóm",
                    value=value,
                )

            declared = table_for(ImportStagingRow).alias("declared_parent")
            declared_as_leaf = exists(
                select(declared.c.id)
                .where(declared.c.job_id == self._job_id)
                .where(func.btrim(declared.c["cells"]["code"].astext) == cell(column.field))
                .where(
                    func.lower(
                        func.coalesce(func.btrim(declared.c["cells"]["is_group"].astext), "")
                    ).notin_(TRUE_WORDS)
                )
            )
            in_catalog = exists(select(target.c.id).where(visible_parent))
            statement = (
                self._base(cell(column.field))
                .where(cell_or_null(column.field).is_not(None))
                .where(~in_catalog)
                .where(declared_as_leaf)
            )
            for row_number, value in self._rows(statement):
                yield RowError(
                    row=row_number,
                    column=column.display_header,
                    code="import.parent_not_group",
                    message=(
                        f"Dòng khai mã {value!r} trong tệp không đánh dấu 'Là nhóm' "
                        "nên không nhận con được"
                    ),
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

    def _shared_record_errors(
        self, spec: CatalogSpec, *, mode: ImportMode, branch_id: int | None
    ) -> Iterator[RowError]:
        """Dòng trỏ vào một mã **dùng chung toàn công ty** trong lúc đứng ở chi nhánh.

        Review vòng 1, C1 — và nó là lỗ hổng của chính tiêu chí trung tâm của
        lát này. Bộ xuất lọc theo phạm vi **đọc** (`visible_to`: dùng chung +
        riêng chi nhánh), còn bộ nhập so khớp theo phạm vi **ghi** (`owned_by`:
        đúng chi nhánh, H86). Đứng ở chi nhánh A xuất danh mục rồi nhập lại
        nguyên xi, mọi mã dùng chung **không khớp gì** nên chúng được tạo mới
        thành bản ghi riêng của A: danh mục nhân đôi, không một dòng lỗi nào,
        và hai bản ghi cùng mã từ đó trôi vào mọi báo cáo.

        **Chỉ ở chế độ cập nhật**, và ranh giới ấy là chỗ giữ nguyên H86. H86 nói
        rõ: đứng ở chi nhánh A mà nhập một mã đã có ở phần dùng chung thì đó
        *không* phải "mã đã tồn tại" — nó tạo bản ghi riêng của A, đúng bằng thứ
        `POST /api/v1/master/{slug}` vẫn cho phép (FR-SYS-018). Ở `CREATE_ONLY`
        đó vẫn là ý định hợp lệ và duy nhất đọc được từ tệp.

        Ở `CREATE_AND_UPDATE` thì ý định đọc được là ngược lại: người dùng nói
        "cập nhật những gì đã có". Lặng lẽ tạo một bản sao riêng chi nhánh thay vì
        cập nhật là làm điều họ không xin, nên đây thành một dòng lỗi nói thẳng
        việc phải làm.
        """
        if mode is not ImportMode.CREATE_AND_UPDATE or branch_id is None:
            return
        table = table_for(spec.model)
        shared = exists(
            select(table.c.id)
            .where(table.c.code == cell("code"))
            .where(table.c.branch_id.is_(None))
        )
        # Trừ ra dòng đã khớp một bản ghi **riêng của chi nhánh này**: mã dùng
        # chung và mã riêng cùng tên được phép tồn tại song song (H86), và khi
        # bản riêng đã có thì lượt nhập cập nhật đúng nó — không có gì để cảnh báo.
        owned = exists(select(table.c.id).where(code_matches(table, branch_id)))
        statement = self._base(cell("code")).where(shared).where(~owned)
        for row_number, value in self._rows(statement):
            yield RowError(
                row=row_number,
                column="Mã *",
                code="import.shared_record_not_editable_from_branch",
                message=f"Mã {value!r} thuộc danh mục dùng chung toàn công ty — "
                "không sửa được từ chi nhánh. Hãy nhập ở phạm vi toàn công ty, "
                "hoặc bỏ dòng này khỏi tệp",
                value=value,
            )

    def _reference_moved_errors(
        self, spec: CatalogSpec, *, mode: ImportMode, branch_id: int | None
    ) -> Iterator[RowError]:
        """Mã tra cứu nay phân giải ra một bản ghi **khác** bản ghi đang được trỏ.

        Review vòng 1 (H4) rồi vòng 2 (H3), tái hiện được nguyên vẹn: đối tác trỏ
        điều khoản dùng chung `PT01` (id 1); chi nhánh khai `PT01` riêng của mình
        (id 2 — hợp lệ theo H86, hai chỉ mục duy nhất tách theo điều kiện); xuất
        đối tác ở chi nhánh rồi **nhập lại nguyên xi** ở chế độ cập nhật →
        `payment_term_id` đổi **1 → 2**, `is_valid=True`, không một dòng lỗi.

        Điều khoản thanh toán của một khách hàng đổi sau một thao tác mà người
        dùng tin là "không đổi gì" — và hạn nợ của mọi hóa đơn sau đó tính theo
        cái mới. Áp cho cả ba cột tra cứu sửa được (`partners.payment_term_code`,
        `items.warehouse_code`, `employees.bank_code`).

        Chỉ ở chế độ **cập nhật**: dòng tạo mới chưa có giá trị cũ nào để đổi.
        """
        if mode is not ImportMode.CREATE_AND_UPDATE:
            return
        table = table_for(spec.model)
        for column in editable_columns(spec, self._descriptor):
            if column.kind is not CellKind.CODE_REF or column.target_field == "parent_id":
                continue
            current = table.c[column.target_field or column.field]
            resolved = reference_id(column, branch_id)
            statement = (
                self._base(cell(column.field))
                .join(table, code_matches(table, branch_id))
                # Ô để trống = "không nói gì" (H87), và mã tra không ra đã là một
                # dòng lỗi khác — cả hai đều không phải một lần **đổi**.
                .where(cell_or_null(column.field).is_not(None))
                .where(current.is_not(None))
                .where(resolved.is_not(None))
                .where(resolved != current)
            )
            for row_number, value in self._rows(statement):
                yield RowError(
                    row=row_number,
                    column=column.display_header,
                    code="import.reference_moved",
                    message=f"Mã {value!r} nay trỏ tới một bản ghi khác bản ghi dòng này "
                    "đang dùng (thường vì chi nhánh có một mã riêng trùng mã dùng chung). "
                    "Bỏ trống ô này nếu muốn giữ nguyên giá trị đang có",
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

    def _row_rule_errors(
        self,
        spec: CatalogSpec,
        *,
        mode: ImportMode,
        branch_id: int | None,
        autocreate: frozenset[str],
    ) -> Iterator[RowError]:
        """Luật liên-trường của danh mục, đánh giá trên **giá trị hiệu lực** (H3).

        Nợ H3 của lát 3C-1: bước kiểm không biết ràng buộc `CHECK` liên-trường
        của DB, nên vẫn có ca "hợp lệ" rồi job hỏng ở bước ghi với một thông báo
        `IntegrityError` thô. Không mất dữ liệu (một transaction, quay lui trọn
        vẹn), nhưng người dùng đọc "tệp hợp lệ" rồi nhận tên một ràng buộc nội bộ.

        **Giá trị hiệu lực, không phải ô thô** — đây là điểm dễ sai nhất. Một
        dòng ở chế độ cập nhật chỉ sửa tên của một mã hàng thì tệp không nhắc lại
        đơn vị tính của nó, và ô trống nghĩa là "giữ nguyên" (H87/H91). Đánh giá
        `stock_item_needs_base_unit` trên ô thô sẽ báo lỗi cho một mã hàng hoàn
        toàn hợp lệ. `_StagedValues` vì thế chọn `keep_existing` cho dòng đã có
        bản ghi và `column_source` cho dòng mới — cùng hai hàm mà **bước ghi**
        dùng, nên phép kiểm nói đúng thứ sắp được ghi.
        """
        if not spec.row_rules:
            return
        table = table_for(spec.model)
        updating = mode is ImportMode.CREATE_AND_UPDATE
        values = _StagedValues(
            descriptor=self._descriptor,
            table=table,
            branch_id=branch_id,
            autocreate=autocreate,
            updating=updating,
        )
        for rule in spec.row_rules:
            column = self._descriptor.column(rule.field)
            header = column.display_header if column is not None else rule.field
            statement = (
                self._base(cell(rule.field))
                .outerjoin(table, code_matches(table, branch_id))
                .where(rule.violated(values))
            )
            for row_number, value in self._rows(statement):
                yield RowError(
                    row=row_number,
                    column=header,
                    code=rule.code,
                    message=rule.message,
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
