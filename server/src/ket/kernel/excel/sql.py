"""Biểu thức SQL dùng chung giữa bước **kiểm** và bước **ghi** của một lượt nhập.

Hai lý do tệp này tồn tại, và lý do thứ hai quan trọng hơn.

**Một — hai bước phải đồng ý với nhau về từng chi tiết.** Mỗi chỗ chúng lệch là
một lỗi thuộc loại tệ nhất: dữ liệu qua được phép kiểm rồi được ghi thành một
thứ khác. Ba ví dụ, cả ba đều suýt xảy ra khi hai bước giữ bản sao riêng:

* `"Có"` được phép kiểm nhận là boolean nhưng bước ghi chỉ nhận `"x"` → cột
  thành `false`, im lặng.
* Phép kiểm tra mã theo thứ tự "riêng chi nhánh trước", bước ghi theo `id` nhỏ
  nhất → hai bản ghi khác nhau cho cùng một mã.
* Phép kiểm đọc ô đã `btrim`, bước ghi đọc chưa → `"VT001 "` tra không ra dù vừa
  được báo là hợp lệ.

**Hai — không có chuỗi SQL nào được ghép ở lát này.** Mọi truy vấn dựng bằng
biểu thức Core (`select`, `insert().from_select`, `update`), lấy bảng từ
`spec.model.__table__` và ô JSONB từ `ImportStagingRow.values[...].astext`.

Đó không phải sở thích. `tests/test_no_sql_string_interpolation.py` giải thích
vì sao ràng buộc tham số là **hàng rào chính** chứ không phải lớp phòng thủ phụ:
psycopg dùng simple protocol khi câu lệnh không có tham số, và simple protocol
cho phép **nhiều câu lệnh** trong một lần gửi — tức một đường leo sang dataset
khác (`"; SET ROLE ds_beta_app; …"`), thứ mà cô lập bằng vai trò DB không chặn
được. Danh sách miễn trừ của bộ quét ấy từng có năm tệp và đã được rút xuống một;
thêm vào đó hai tệp sinh SQL động nhiều nhất của lát này là đi ngược đúng việc
đó. Biểu thức Core khiến câu hỏi không còn phải đặt ra — và được mypy kiểm luôn.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import (
    Column,
    ColumnElement,
    FromClause,
    String,
    Table,
    Text,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.orm import DeclarativeBase

from ket.kernel.excel.descriptors import CellKind, ColumnDescriptor, TemplateDescriptor
from ket.kernel.excel.models import ImportStagingRow
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec

TRUE_WORDS: Final[tuple[str, ...]] = ("x", "co", "có", "true", "1", "yes", "y")
FALSE_WORDS: Final[tuple[str, ...]] = ("khong", "không", "false", "0", "no", "n")
"""Từ khóa boolean, cả tiếng Việt lẫn tiếng Anh, cả có dấu lẫn không dấu.

Tệp mẫu nói "đánh x", nhưng người dùng gõ thứ họ quen — từ chối `"Có"` là bắt họ
học một quy ước để phục vụ máy. So sánh chạy sau `lower()` + `btrim`.

Chuỗi rỗng **không** nằm trong `FALSE_WORDS`: "để trống" là một trạng thái riêng
mà mỗi cột diễn giải theo cách của nó — `is_group` trống là `false`, còn
`is_active` trống là `true` (xem `is_active_expression`)."""

COMMON_TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"code", "name", "name_en", "parent_code", "is_group", "is_active"}
)
"""Cột mà câu `INSERT` liệt kê tường minh, nên phần "cột riêng" phải trừ chúng ra."""

ALWAYS_CREATE_ONLY_FIELDS: Final[frozenset[str]] = frozenset({"is_group"})
"""Cột chốt một lần của **mọi** danh mục, không suy được từ registry (H88).

`is_group` vắng mặt trong `MasterDataBaseUpdateRequest` từ lát 3A nên không đường
API nào đổi được nó — nhưng sự vắng mặt ấy là tính chất của *một model Pydantic*,
không phải một luật mà chỗ khác đọc được. Nhập liệu là đường ghi thứ hai, và nó
phải tự biết.

Vì sao luật này đáng có: bật `is_group` trên một nút đang có bản ghi con biến một
nhóm thành nút hạch toán được **mà vẫn có nhánh con** — trạng thái không đường ghi
nào khác tạo ra nổi, và là đúng thứ `MasterDataService.ensure_postable` (posting
engine, phase 4) dựa vào để từ chối hạch toán vào nút gom."""


def cell(field: str) -> ColumnElement[str]:
    """Ô của bảng đệm dưới dạng `text`, đã cắt khoảng trắng hai đầu.

    `.astext` (toán tử `->>`) chứ không `[...]` trần (`->`): mọi phép kiểm làm
    việc trên chuỗi thô, và một giá trị `jsonb` mang theo dấu nháy kép của chính
    nó vào câu thông báo lỗi.

    Tên trường đi vào truy vấn như một **tham số ràng buộc** của toán tử `->>`,
    không phải một mảnh chuỗi ghép vào câu lệnh — đó là toàn bộ khác biệt so với
    bản đầu của tệp này.
    """
    return func.btrim(ImportStagingRow.cells[field].astext)


def cell_or_null(field: str) -> ColumnElement[str]:
    """Ô rỗng và ô chỉ có khoảng trắng đều là "không nhập"."""
    return func.nullif(cell(field), "")


def table_for(model: type[DeclarativeBase]) -> Table:
    """`Model.__table__` với kiểu **hẹp lại** thành `Table`.

    SQLAlchemy khai `__table__` là `FromClause` vì một model ánh xạ được vào
    cả `Join` hay `Subquery`. Mọi model của lát này ánh xạ vào một bảng thật,
    nhưng mypy không biết điều đó — và `insert()`/`update()` thì đòi `Table`.
    Kiểm ở đây một lần thay vì rải `cast()` khắp nơi: một `cast()` là lời hứa,
    còn `isinstance` là một phép kiểm sẽ nổ nếu lời hứa sai.
    """
    table = model.__table__
    if not isinstance(table, Table):
        raise TypeError(f"{model.__name__} không ánh xạ vào một bảng thật")
    return table


def table_of(slug: str | None) -> Table:
    """Bảng của một danh mục, tra từ registry.

    Trả về đối tượng `Table` chứ không tên: nó là thứ biểu thức Core nhận, nên
    không có chỗ nào trong lát này cầm một tên bảng dưới dạng chuỗi.

    Tra không ra là lỗi **lập trình** — một `CatalogReference` trỏ vào danh mục
    chưa đăng ký — nên nó ném `ValueError`, không phải lỗi nghiệp vụ.
    """
    spec = CATALOG_REGISTRY.get(slug or "")
    if spec is None:
        raise ValueError(f"CatalogReference trỏ tới danh mục chưa đăng ký: {slug!r}")
    return table_for(spec.model)


def visible_to(table: FromClause, branch_id: int | None) -> ColumnElement[bool]:
    """Điều kiện "chi nhánh này thấy dòng nào" của một bảng danh mục (FR-SYS-018).

    Cùng luật với `MasterDataService._visible_to`: dùng chung toàn công ty
    (`branch_id IS NULL`) cộng phần riêng của chi nhánh đang thao tác. Bảng danh
    mục cố ý không bật RLS (H39) nên đây là lớp lọc duy nhất, và một lượt nhập
    tra được bản ghi mà màn hình giấu đi sẽ gắn khóa ngoại vào một thứ người
    dùng không nhìn thấy.
    """
    shared = table.c.branch_id.is_(None)
    if branch_id is None:
        return shared
    return or_(shared, table.c.branch_id == branch_id)


def boolean_expression(field: str) -> ColumnElement[bool]:
    """Ô boolean → `boolean`. Trống = `false`."""
    return func.lower(func.coalesce(cell(field), "")).in_(TRUE_WORDS)


def is_active_expression() -> ColumnElement[bool]:
    """Cột "Còn theo dõi" — **để trống nghĩa là còn theo dõi**.

    Ngược chiều mọi cột boolean khác, và tệp mẫu ghi rõ điều đó. Dùng chung
    `boolean_expression` ở đây sẽ khiến mọi dòng bỏ trống cột ấy được tạo ra ở
    trạng thái đã ngừng theo dõi — tức một lượt nhập 10.000 dòng cho ra một danh
    mục rỗng trên mọi màn hình, và không có thông báo lỗi nào vì không có lỗi nào.
    """
    return func.lower(func.coalesce(cell("is_active"), "")).notin_(FALSE_WORDS)


def reference_id(column: ColumnDescriptor, branch_id: int | None) -> ColumnElement[int]:
    """Mã tra cứu → `id` của bản ghi nhìn thấy được từ chi nhánh đang thao tác.

    `ORDER BY branch_id NULLS LAST` là **cùng** thứ tự ưu tiên mà
    `MasterDataService.resolve_by_code` dùng ("riêng chi nhánh trước, dùng chung
    sau"). Hai chỗ phân xử "cùng mã, hai bản ghi" theo hai thứ tự khác nhau
    nghĩa là nhập liệu và ô tra cứu trên màn hình cho ra hai bản ghi khác nhau
    cho cùng một mã người dùng gõ — và không ai phát hiện cho tới lúc đối chiếu.
    """
    target = table_of(column.reference_slug)
    return (
        select(target.c.id)
        .where(target.c.code == cell(column.field))
        .where(visible_to(target, branch_id))
        .order_by(target.c.branch_id.desc().nulls_last())
        .limit(1)
        .scalar_subquery()
    )


def owned_by(table: FromClause, branch_id: int | None) -> ColumnElement[bool]:
    """Phạm vi **ghi** của một lượt nhập: đúng chi nhánh đang thao tác (H86).

    Khác hẳn `visible_to`, và khác biệt ấy là cả một lớp lỗi. `visible_to` trả
    lời "chi nhánh này **đọc** được dòng nào" nên nó gồm cả phần dùng chung toàn
    công ty (`branch_id IS NULL`). Dùng nó làm điều kiện ghi thì một lượt nhập
    của chi nhánh A sẽ sửa bản ghi dùng chung của cả công ty — và vì
    `master_data_table_args` **cố ý** cho phép mã `VT001` dùng chung tồn tại song
    song `VT001` riêng của A (hai chỉ mục duy nhất tách theo điều kiện), một ô mã
    khớp **hai** dòng và câu `UPDATE` sửa cả hai.

    `IS NOT DISTINCT FROM` chứ không `=`: người dùng không chọn chi nhánh nào thì
    `branch_id` là `NULL`, và phạm vi ghi của họ đúng là phần dùng chung — `NULL =
    NULL` cho ra `NULL`, tức không khớp gì.

    Hệ quả có chủ đích (FR-SYS-018): đứng ở chi nhánh A mà nhập một mã đã tồn tại
    ở phần dùng chung thì đó **không** phải "mã đã tồn tại" — nó tạo ra bản ghi
    riêng của A, đúng bằng thứ `POST /api/v1/master/{slug}` vẫn cho phép.
    """
    return table.c.branch_id.is_not_distinct_from(branch_id)


def code_matches(table: FromClause, branch_id: int | None) -> ColumnElement[bool]:
    """Điều kiện nối một dòng đệm với bản ghi đang có cùng mã **trong phạm vi ghi**.

    Một hàm chứ ba bản sao: phép so này quyết định "tạo mới hay cập nhật" ở bước
    đếm, ở phép kiểm trùng mã, ở câu `INSERT` và ở câu `UPDATE`. Bốn bản sao là
    bốn cơ hội để một bản lệch khỏi ba bản kia, và hậu quả
    là báo cáo hứa "cập nhật 9.960" rồi bước ghi tạo mới 9.960 bản ghi trùng mã.

    So **phân biệt hoa thường**, cùng luật với `MasterDataService.resolve_by_code`
    và với chính ràng buộc duy nhất của bảng (`uq_<bảng>_shared_code` trên `code`
    nguyên văn). Một phép so `lower(...) = lower(...)` ở đây sai theo hai đường
    cùng lúc: nó **khớp nhầm** khi danh mục có cả `VT001` lẫn `vt001` — hai bản
    ghi hợp lệ, khác nhau, mà DB cho phép tồn tại song song — và nó vô hiệu hóa
    chỉ mục trên `code`, biến mỗi phép kiểm thành một lượt quét toàn bảng. Với
    10.000 dòng nhân số bản ghi đang có, đó là khác biệt giữa vài giây và nhiều
    phút (FR-NFR-043).
    """
    return and_(
        table.c.code == cell("code"),
        owned_by(table, branch_id),
    )


def column_source(
    column: ColumnDescriptor, branch_id: int | None, target: Column[Any] | None = None
) -> ColumnElement[Any]:
    """Giá trị của một cột đích, lấy từ dòng đệm — dùng ở cả `INSERT` lẫn `UPDATE`.

    `target` là **cột thật** sẽ nhận giá trị. Có nó thì giá trị được ép về đúng
    kiểu của cột; thiếu nó thì mọi thứ không phải khóa ngoại hay boolean đi vào
    câu lệnh dưới dạng `text`.

    Đó không phải chi tiết làm đẹp: PostgreSQL **không** ép ngầm `text → integer`
    ở vị trí gán, nên một câu `INSERT ... SELECT` gán `text` vào cột `integer`
    hỏng **lúc lập kế hoạch** — không phụ thuộc dữ liệu, kể cả khi mọi ô của cột
    đó để trống. Bốn danh mục (`asset_types`, `partners`, `payment_terms`,
    `tool_types`) không nhập được dòng nào vì đúng điều này, trong khi bước kiểm
    vẫn báo "hợp lệ".
    """
    if column.kind is CellKind.CODE_REF:
        return reference_id(column, branch_id)
    if column.field == "is_active":
        return is_active_expression()
    if column.kind is CellKind.BOOLEAN:
        return boolean_expression(column.field)
    value = cell_or_null(column.field)
    if target is None:
        return value
    return _with_default(_typed(value, target), target)


def _typed(value: ColumnElement[Any], target: Column[Any]) -> ColumnElement[Any]:
    """Ép ô về kiểu cột — **trừ** kiểu chuỗi, và ngoại lệ đó là cả một lớp lỗi.

    `CAST(x AS varchar(n))` của PostgreSQL **cắt** chuỗi dài quá thay vì báo lỗi,
    trong khi *gán* một `text` vào cột `varchar(n)` thì nổ với "value too long".
    Ép kiểu ở đây vì thế biến một lỗi ồn thành một lỗi im: `swift_code` 20 ký tự
    lặng lẽ thành 11 ký tự đầu — và tệ hơn, nó **lọt** luôn ràng buộc
    `CHECK (length(swift_code) IN (8, 11))` vì sau khi cắt thì độ dài đúng bằng 11.

    Cột số và cột ngày thì ngược lại: không ép thì câu lệnh hỏng lúc lập kế hoạch
    (không có phép ép ngầm `text → integer` ở vị trí gán). Nên ép cho chúng, để
    nguyên cho chuỗi.
    """
    return value if isinstance(target.type, String) else cast(value, target.type)


def _with_default(value: ColumnElement[Any], target: Column[Any]) -> ColumnElement[Any]:
    """Ô để trống trên một cột `NOT NULL` thì rơi về **giá trị ngầm định của cột**.

    Câu `INSERT ... SELECT` liệt kê mọi cột tường minh, nên một ô trống ghi
    `NULL` **đè lên** cả `DEFAULT` của cột — từ khóa `DEFAULT` không dùng được ở
    vị trí một biểu thức trong `SELECT`. Với cột `NOT NULL` có ngầm định (ví dụ
    `payment_terms.discount_days`, khai `default=0, server_default="0"`), hệ quả
    là mọi lượt nhập bỏ trống cột đó đều đổ với `NotNullViolation` — người dùng
    thấy "hợp lệ" ở bước kiểm rồi job hỏng vì một cột họ cố ý không điền.

    Chỉ áp cho cột `NOT NULL`: cột cho phép rỗng thì `NULL` **là** câu trả lời
    đúng cho một ô trống, và ghép ngầm định vào đó sẽ biến "không nhập" thành
    một giá trị người dùng không gõ.
    """
    if target.nullable:
        return value
    fallback = getattr(target.default, "arg", None)
    if fallback is None or callable(fallback):
        # Không có ngầm định vô hướng để rơi về (hoặc nó là một hàm Python chỉ
        # chạy ở đường ORM). Để `NULL` đi tiếp: DB sẽ từ chối, và một thông báo
        # `NotNullViolation` nêu đúng tên cột vẫn tốt hơn một giá trị bịa ra.
        return value
    return func.coalesce(value, literal(fallback))


def keep_existing(
    column: ColumnDescriptor,
    branch_id: int | None,
    target: Column[Any],
) -> ColumnElement[Any]:
    """Giá trị mới ở đường **sửa** — ô để trống thì giữ nguyên giá trị đang có (H87).

    Ô trống nghĩa là "tôi không nói gì về cột này", không phải "hãy xóa nó". Đó là
    điều kiện để vòng làm việc thường gặp nhất an toàn: xuất danh mục ra, sửa hai
    cột, nhập lại — với cách hiểu ngược lại thì một lần dán hụt vài cột xóa trắng
    hàng nghìn bản ghi mà không có cảnh báo nào.

    Cái giá phải trả, có chủ đích: **không** xóa được một giá trị bằng nhập liệu.
    Người dùng xóa nó trên chính màn hình của bản ghi, nơi họ thấy mình đang xóa gì.

    **Không** đi qua `_with_default`, và đó là điểm dễ sai nhất của tệp này. Bản
    đầu bọc `coalesce(column_source(...), cột)` — mà `column_source` cho cột
    `NOT NULL` **đã** là `coalesce(ô, ngầm_định)` và vì thế không bao giờ trả
    `NULL`. Lớp ngoài thành mã chết, nên một ô để trống **đặt lại cột về ngầm
    định** thay vì giữ giá trị đang có: đúng ngược lại H87, và đúng ở những cột mà
    `_with_default` được viết ra để phục vụ (`payment_terms.due_days` 30 → 0).

    Ngầm định của cột là câu trả lời đúng cho một dòng **mới**; với dòng đã tồn
    tại thì câu trả lời đúng là giá trị nó đang mang.
    """
    if column.kind is CellKind.CODE_REF:
        return func.coalesce(reference_id(column, branch_id), target)
    if column.kind is CellKind.BOOLEAN or column.field == "is_active":
        # Ô boolean để trống cũng là "không nói gì" ở đường sửa (H87 mở rộng sau
        # review vòng 2): trước đó ô trống ở cột "Còn theo dõi" nghĩa là `true`,
        # nên một tệp dán hụt cột đó **bật lại** mọi bản ghi ai đó đã cho ngừng
        # theo dõi. Trên dòng **mới** thì "để trống" vẫn giữ nghĩa cũ — bản ghi
        # mới không có giá trị cũ nào để giữ.
        return case(
            (cell_or_null(column.field).is_(None), target),
            else_=column_source(column, branch_id, target),
        )
    return func.coalesce(_typed(cell_or_null(column.field), target), target)


def as_text(value: ColumnElement[Any]) -> ColumnElement[str]:
    """Ép một biểu thức về `text` để so sánh giữa hai kiểu khác nhau.

    Dùng ở đúng một chỗ — so trường chốt một lần trong tệp với giá trị đang có
    (H81), nơi một bên là chuỗi người dùng gõ và bên kia là cột thật (`integer`,
    `varchar`, `numeric`). So bằng `text` tránh phải biết kiểu của từng cột, và
    phép so ấy chỉ cần trả lời "hai giá trị có khác nhau không".
    """
    return cast(value, Text())


def create_only_columns(
    spec: CatalogSpec, descriptor: TemplateDescriptor
) -> tuple[ColumnDescriptor, ...]:
    """Cột có ở thân request tạo mới nhưng **không** có ở thân request sửa (H69).

    Suy ra từ hiệu của hai model chứ không từ một danh sách tên trường thứ hai:
    registry đã diễn đạt quan hệ ấy bằng kế thừa (`extra_fields` là lớp con của
    `extra_update_fields`), và một danh sách song song ở đây là chỗ để lát sau
    thêm một trường chốt một lần mà quên cập nhật — hậu quả im lặng, đúng loại
    mà H81 sinh ra để chặn.
    """
    found = [column for column in descriptor.columns if column.field in ALWAYS_CREATE_ONLY_FIELDS]
    if spec.extra_fields is None or spec.extra_update_fields is None:
        return tuple(found)
    editable = set(spec.extra_update_fields.model_fields)
    locked = [field for field in spec.extra_fields.model_fields if field not in editable]
    for field in locked:
        match = next(
            (
                item
                for item in descriptor.columns
                if item.field == field or item.target_field == field
            ),
            None,
        )
        if match is not None:
            found.append(match)
    return tuple(found)


def extra_columns(descriptor: TemplateDescriptor) -> tuple[ColumnDescriptor, ...]:
    """Cột riêng của danh mục — phần câu `INSERT` phải liệt kê thêm.

    `parent_code` không nằm ở đây vì nó được xử lý riêng bằng một `LEFT JOIN`
    trong câu lệnh: nó là cột duy nhất mà giá trị có thể trỏ tới **một dòng khác
    trong cùng tệp**, nên nó không tra được bằng một truy vấn con đơn giản.
    """
    return tuple(
        column for column in descriptor.columns if column.field not in COMMON_TARGET_FIELDS
    )


def editable_columns(
    spec: CatalogSpec, descriptor: TemplateDescriptor
) -> tuple[ColumnDescriptor, ...]:
    """Cột mà lượt nhập ở chế độ cập nhật được phép ghi đè.

    Trừ ra ba nhóm, mỗi nhóm một lý do khác nhau:

    * `code` — là khóa so khớp, sửa nó ở đây không có nghĩa gì.
    * `parent_code` — đổi cha là chuyển cả một nhánh cây, việc mà
      `MasterDataService.move` làm bằng một câu `UPDATE` riêng **có kiểm chu
      trình**. Nhập liệu không được là đường vòng qua phép kiểm ấy.
    * Trường chốt một lần — H81; phép kiểm đã biến chúng thành dòng lỗi, và bỏ
      chúng khỏi câu `UPDATE` là lớp bảo đảm thứ hai cho cùng bất biến.
    """
    locked = set(create_only_columns(spec, descriptor))
    return tuple(
        column
        for column in descriptor.columns
        if column.field not in {"code", "parent_code"} and column not in locked
    )


def first_occurrence_rank() -> ColumnElement[int]:
    """Thứ hạng của một dòng trong nhóm cùng mã, theo thứ tự xuất hiện trong tệp.

    `> 1` là dòng trùng. Dòng **đầu tiên** của mỗi nhóm cố ý không bị đánh dấu:
    nó là dòng hợp lệ, và báo lỗi cả nhóm sẽ khiến một mã lặp hai lần sinh ra
    hai lỗi cho một việc phải sửa.
    """
    return func.row_number().over(
        partition_by=cell_or_null("code"),
        order_by=ImportStagingRow.row_number,
    )


def counts_expression(matched: ColumnElement[Any]) -> tuple[ColumnElement[int], ...]:
    """`(sẽ tạo mới, sẽ cập nhật)` — đếm theo việc có khớp bản ghi đang có không."""
    return (
        func.count().filter(matched.is_(None)),
        func.count().filter(matched.is_not(None)),
    )


def truthy(condition: ColumnElement[bool]) -> ColumnElement[bool]:
    """`CASE WHEN … THEN true ELSE false END` — để `NULL` không thành "không lỗi".

    Dùng ở phép kiểm kiểu: `lower(ô) !~ mẫu` trả `NULL` khi ô rỗng, và `NULL`
    trong mệnh đề `WHERE` nghĩa là dòng **không** được chọn. Ở đây điều đó tình
    cờ đúng (ô rỗng không phải lỗi kiểu), nhưng dựa vào một sự tình cờ là cách
    phép kiểm tiếp theo sao chép nó vào chỗ mà nó sai.
    """
    return case((condition, True), else_=False)
