"""Luật liên-trường ở bước kiểm nhập liệu (lát 3C-2, nợ H3 của 3C-1).

Nợ H3 nói: bước kiểm không biết ràng buộc `CHECK` liên-trường của DB, nên vẫn có
ca "hợp lệ" rồi job hỏng ở bước ghi với một lỗi DB thô. Tệp này đóng nó, và đóng
theo hai chiều:

* **Cổng chống trôi** (không cần DB) — mọi `CHECK` của một bảng danh mục phải
  hoặc có một `RowRule` mang đúng tên nó, hoặc nằm trong danh sách "đã được canh
  ở chỗ khác" **kèm lý do**. Một ràng buộc thêm ở phase 5 làm bộ test đỏ, nên nợ
  H3 không quay lại được trong im lặng.
* **Hành vi** (cần DB) — luật thật sự thành dòng lỗi có số dòng và tên ô, và
  quan trọng không kém: nó **không** báo lỗi cho dòng hợp lệ, kể cả dòng cập
  nhật chỉ nhắc lại một phần các cột.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import unique_code
from import_support import import_source, minimal_row, spec_of, workbook_bytes
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.report import ImportMode, ImportReport
from ket.kernel.excel.sql import table_for
from ket.kernel.excel.staging import _StagedValues
from ket.kernel.master_data.registry import REGISTRY, CatalogSpec
from ket.kernel.persistence.unit_of_work import RequestScope

# ------------------------------------------------------- cổng chống trôi


COVERED_ELSEWHERE: dict[str, str] = {
    "code_not_blank": (
        "Ô trống thành `NULL` ở `sql.cell_or_null`, và cột Mã là cột bắt buộc — "
        "chuỗi rỗng không có đường nào tới cột này từ một tệp nhập liệu."
    ),
    "path_is_dotted_ids": (
        "`path` do bước ghi tính (`sql.py` dựng lại `tree_path.child_path`), không "
        "có cột nào trong tệp mẫu — xem `_COMMON_COLUMNS`."
    ),
    "level_at_least_root": "Như `path`: bước ghi tính, người dùng không gõ được.",
    "nature_known": (
        "`staging._allowed_value_errors` đã kiểm bằng tập giá trị đọc từ kiểu cột "
        "thật (review vòng 2, R2-1). Khai thêm một luật là hai câu lỗi cho một ô sai."
    ),
    "short_name_not_blank": "Ô trống thành `NULL`, không thành chuỗi rỗng.",
    "id_number_not_blank": "Ô trống thành `NULL`, không thành chuỗi rỗng.",
    "tax_code_not_blank": "Ô trống thành `NULL`, không thành chuỗi rỗng.",
}
"""Ràng buộc **không** cần `RowRule`, kèm lý do vì sao.

Danh sách có lý do chứ không chỉ có tên: một dòng ở đây là một lời khẳng định
rằng đường nhập liệu **không chạm tới được** ràng buộc ấy, và lời khẳng định thì
phải đọc lại được khi ai đó đổi `cell_or_null` hay thêm một cột vào tệp mẫu."""


def _constraint_suffix(spec: CatalogSpec, name: str) -> str:
    """`ck_items_nature_known` → `nature_known`."""
    prefix = f"ck_{spec.model.__tablename__}_"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _declared_checks(spec: CatalogSpec) -> set[str]:
    table = table_for(spec.model)
    return {
        _constraint_suffix(spec, str(item.name))
        for item in table.constraints
        if isinstance(item, CheckConstraint) and item.name
    }


@pytest.mark.parametrize("slug", [spec.slug for spec in REGISTRY.specs()])
def test_every_check_constraint_is_either_a_row_rule_or_explained(slug: str) -> None:
    """Không ràng buộc nào được im lặng vắng mặt khỏi bước kiểm.

    Đây là cổng khiến nợ H3 không tái phát: phase 5 thêm hệ thống tài khoản,
    phase 9 thêm biểu thuế, và mỗi `CHECK` mới của chúng buộc người viết phải trả
    lời một câu — "người dùng có gõ sai được điều này từ Excel không?". Trả lời
    "có" thì viết một `RowRule`; trả lời "không" thì viết lý do vào
    `COVERED_ELSEWHERE`. Không có đường thứ ba.
    """
    spec = spec_of(slug)
    declared = _declared_checks(spec)
    ruled = {rule.constraint for rule in spec.row_rules}
    unexplained = declared - ruled - set(COVERED_ELSEWHERE)
    assert not unexplained, (
        f"Danh mục {slug!r} có ràng buộc CHECK không ai canh ở bước kiểm: "
        f"{sorted(unexplained)}. Viết một `RowRule` cạnh model, hoặc thêm vào "
        f"`COVERED_ELSEWHERE` kèm lý do."
    )


@pytest.mark.parametrize("slug", [spec.slug for spec in REGISTRY.specs()])
def test_every_row_rule_points_at_a_real_constraint(slug: str) -> None:
    """Chiều ngược lại: một luật trỏ vào ràng buộc không còn tồn tại là luật chết.

    Nó vẫn chạy và vẫn báo lỗi, nhưng nó không còn phản chiếu điều gì dưới DB —
    tức bước kiểm từ chối một dòng mà DB sẵn sàng nhận.
    """
    spec = spec_of(slug)
    declared = _declared_checks(spec)
    for rule in spec.row_rules:
        assert rule.constraint in declared, (
            f"Luật của {slug!r} trỏ tới ràng buộc {rule.constraint!r} không có "
            f"trên bảng. Ràng buộc hiện có: {sorted(declared)}"
        )


@pytest.mark.parametrize("slug", [spec.slug for spec in REGISTRY.specs()])
def test_every_column_a_rule_reads_exists_in_the_template(slug: str) -> None:
    """Kể cả cột đọc **bên trong** `violated`, không chỉ cột `rule.field`.

    Bài ngay dưới chỉ soi `rule.field`, và đó là một điểm mù thật: một luật khai
    `field="direction"` (có trong tệp mẫu) nhưng đọc `row.value("partner_id")` ở
    thân lambda đi lọt cả hai cổng không-DB và chỉ nổ ở bước kiểm thật — với một
    `ValueError` từ sâu trong `staging`, cách xa dòng đã viết sai. Lát 7C-1 mắc
    đúng lỗi ấy: tệp mẫu cho người điền gõ **mã**, không gõ khóa ngoại (H79), nên
    cột `partner_id` không tồn tại ở bước kiểm — tên đúng là `partner_code`.

    Cách đóng: dựng chính khung nhìn mà `staging` truyền vào luật rồi **gọi**
    `violated`. Không cần PostgreSQL — dựng một biểu thức SQLAlchemy là việc thuần
    Python, và phép chiếu cột là chỗ duy nhất có thể ném.
    """
    spec = spec_of(slug)
    if not spec.row_rules:
        return
    values = _StagedValues(
        descriptor=template_for(spec),
        table=table_for(spec.model),
        branch_id=None,
        autocreate=frozenset(),
        updating=False,
    )
    for rule in spec.row_rules:
        try:
            rule.violated(values)
        except ValueError as error:  # pragma: no cover - chỉ chạy khi luật sai
            pytest.fail(f"Luật {rule.constraint!r} của {slug!r}: {error}")


@pytest.mark.parametrize("slug", [spec.slug for spec in REGISTRY.specs()])
def test_every_row_rule_names_a_column_of_the_template(slug: str) -> None:
    """Ô mà câu báo lỗi trỏ tới phải có thật trong tệp — nếu không người dùng không tìm ra."""
    spec = spec_of(slug)
    descriptor = template_for(spec)
    fields = {column.field for column in descriptor.columns}
    for rule in spec.row_rules:
        assert rule.field in fields, (
            f"Luật {rule.constraint!r} của {slug!r} trỏ tới cột {rule.field!r} "
            f"không có trong tệp mẫu"
        )


# --------------------------------------------------------------- hành vi


ITEMS = "items"
PAYMENT_TERMS = "payment_terms"
BANKS = "banks"


@pytest.fixture
def scope(dataset_alpha: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())


def _run(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
    slug: str,
    rows: list[list[object]],
    *,
    mode: ImportMode = ImportMode.CREATE_ONLY,
    commit: bool = False,
) -> ImportReport:
    return import_source(
        session_factory,
        dataset,
        scope,
        tmp_path,
        slug,
        workbook_bytes(slug, rows),
        mode=mode,
        commit=commit,
    )


def _row_with(slug: str, code: str, values: dict[str, object]) -> list[object]:
    spec = spec_of(slug)
    descriptor = template_for(spec)
    row = minimal_row(spec, code, f"Bản ghi {slug}")
    for index, column in enumerate(descriptor.columns):
        if column.field in values:
            row[index] = values[column.field]
    return row


@pytest.mark.db
def test_a_cross_field_violation_becomes_a_row_error_not_a_raw_database_failure(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Điều khoản `2/40 net 30`: mỗi cột hợp lệ, cặp thì không (H3).

    Trước lát này, đúng dòng đó đi qua bước kiểm sạch sẽ rồi làm job ghi đổ với
    `IntegrityError: ck_payment_terms_discount_window_within_due` — người dùng
    đọc "tệp hợp lệ" rồi nhận tên một ràng buộc nội bộ.
    """
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        PAYMENT_TERMS,
        [_row_with(PAYMENT_TERMS, unique_code("DK_H3"), {"due_days": "30", "discount_days": "40"})],
        commit=True,
    )
    assert not report.is_valid
    assert not report.committed, "không được ghi gì khi còn dòng lỗi"
    (error,) = report.errors
    assert error.row == 2
    assert error.code == "import.row_rule_violated"
    assert "ngày được nợ" in error.message
    assert "ck_" not in error.message, "thông điệp không được nêu tên ràng buộc nội bộ"


@pytest.mark.db
def test_a_stock_item_without_a_base_unit_is_caught_at_validation(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """H76 ở đường ghi **thứ ba**: hàng hóa phải có đơn vị chính."""
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        ITEMS,
        [_row_with(ITEMS, unique_code("VT_H3"), {"nature": "goods", "base_unit_code": None})],
    )
    assert not report.is_valid
    assert [error.code for error in report.errors] == ["import.row_rule_violated"]
    assert report.errors[0].column is not None
    assert report.errors[0].column.startswith("Mã đơn vị chính")


@pytest.mark.db
def test_a_swift_code_of_a_legal_length_but_illegal_shape_is_caught(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Chín ký tự: qua được trần độ dài (11), trượt ISO 9362 (8 hoặc 11).

    Ca này là lý do luật độ dài không thay được luật liên-trường — trần đọc từ
    cột chỉ biết "không quá 11".
    """
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        BANKS,
        [_row_with(BANKS, unique_code("NH_H3"), {"swift_code": "ABCDEFGHI"})],
    )
    assert [error.code for error in report.errors] == ["import.row_rule_violated"]
    assert "8 hoặc 11" in report.errors[0].message


@pytest.mark.db
def test_a_valid_row_is_not_flagged_by_any_rule(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Đối chứng: mười bốn luật không được chặn cả dòng đúng.

    Không có phép đối chứng này thì một luật viết ngược chiều vẫn cho mọi test
    trên xanh — nó bắt mọi thứ, kể cả thứ phải cho qua.
    """
    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        PAYMENT_TERMS,
        [_row_with(PAYMENT_TERMS, unique_code("DK_OK"), {"due_days": "30", "discount_days": "10"})],
        commit=True,
    )
    assert report.is_valid, report.errors
    assert report.committed


@pytest.mark.db
def test_an_update_row_that_omits_a_column_is_judged_on_the_stored_value(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Điểm dễ sai nhất của cả lát: luật phải đọc **giá trị hiệu lực** (H87/H91).

    Vòng làm việc thường gặp nhất là xuất ra, sửa một cột, nhập lại. Một tệp chỉ
    mang cột Mã và Tên của một mã hàng đã có đơn vị chính **không** vi phạm
    `stock_item_needs_base_unit` — ô trống nghĩa là "giữ nguyên", nên bản ghi vẫn
    có đơn vị chính sau khi ghi.

    Đánh giá luật trên ô thô sẽ báo lỗi cho một mã hàng hoàn toàn hợp lệ, và báo
    cho **mọi** dòng của mọi tệp sửa từng phần — tức tính năng cập nhật hàng loạt
    coi như hỏng.
    """
    unit_code = unique_code("DVT_H3")
    item_code = unique_code("VT_KEEP")
    assert _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        "units_of_measure",
        [minimal_row(spec_of("units_of_measure"), unit_code, "Cái")],
        commit=True,
    ).committed
    created = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        ITEMS,
        [_row_with(ITEMS, item_code, {"nature": "goods", "base_unit_code": unit_code})],
        commit=True,
    )
    assert created.is_valid and created.committed, created.errors

    # Tệp sửa **chỉ** tên: không nhắc tính chất, không nhắc đơn vị chính.
    spec = spec_of(ITEMS)
    descriptor = template_for(spec)
    sparse: list[object] = [None] * len(descriptor.columns)
    for index, column in enumerate(descriptor.columns):
        if column.field == "code":
            sparse[index] = item_code
        elif column.field == "name":
            sparse[index] = "Tên đã sửa"

    report = _run(
        session_factory,
        dataset_alpha,
        scope,
        tmp_path,
        ITEMS,
        [sparse],
        mode=ImportMode.CREATE_AND_UPDATE,
        commit=True,
    )
    assert report.is_valid, report.errors
    assert report.committed and report.update_rows == 1


@pytest.mark.db
@pytest.mark.parametrize(
    ("field", "cell", "expected"),
    [
        pytest.param("due_days", "1.000", "import.not_integer", id="dấu-phần-nghìn-kiểu-VN"),
        pytest.param("due_days", "ba mươi", "import.not_integer", id="chữ-thay-vì-số"),
        pytest.param("due_days", "N/A", "import.not_integer", id="ô-N/A"),
        pytest.param("due_days", "9999999999999", "import.out_of_range", id="vượt-tầm-cột"),
        # Ca của review vòng 3 (R3-C1): phần nguyên **đúng trần** (3 chữ số cho
        # `Numeric(7, 4)`) nhưng `CAST` làm tròn `999.99999` thành `1000.0000` —
        # tám chữ số, quá `precision` 7. Bản đầu đếm chữ số *trước* khi ép nên nó
        # lọt qua, rồi làm đổ cả lượt kiểm bằng `NumericValueOutOfRange`.
        pytest.param(
            "discount_percent", "999.99999", "import.out_of_range", id="tràn-sau-khi-làm-tròn"
        ),
    ],
)
def test_a_bad_number_is_a_row_error_not_a_crashed_validation(
    field: str,
    cell: str,
    expected: str,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Review vòng 2, C1 — luật liên-trường **nuốt cả báo cáo lỗi**.

    Truy vấn của luật `CAST(ô AS integer)` trên **mọi** dòng đệm, kể cả dòng mà
    phép kiểm kiểu vừa báo là sai. Một ô `"1.000"` (cách gõ số phổ biến nhất ở
    Việt Nam) làm PostgreSQL ném `InvalidTextRepresentation` → transaction hỏng
    → `finally: staging.clear()` ném tiếp `InFailedSqlTransaction` **đè lên**
    nguyên nhân gốc → người dùng nhận `"lỗi không mong muốn: InternalError"` và
    mất toàn bộ danh sách lỗi đã tính đúng.

    Ca cuối là vế thứ hai: `"9999999999999"` **qua** được phép kiểm hình thức
    (nó đúng là một số nguyên) rồi mới nổ ở `CAST` với `NumericValueOutOfRange`
    — nên lọc dòng đã lỗi một mình không đóng được nó.
    """
    row = _row_with(PAYMENT_TERMS, unique_code("DK_BAD"), {field: cell})
    report = _run(
        session_factory, dataset_alpha, scope, tmp_path, PAYMENT_TERMS, [row], commit=True
    )
    assert not report.is_valid
    assert not report.committed
    codes = {error.code for error in report.errors}
    assert expected in codes, f"nhận {codes}"
    assert all(error.row == 2 for error in report.errors)


@pytest.mark.db
def test_a_bad_number_does_not_hide_the_other_errors_of_the_file(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Một dòng hỏng không được xóa mất lỗi của những dòng còn lại.

    Đây là phần đắt nhất của C1: bản đầu không chỉ bỏ sót một lỗi, nó làm **cả
    lượt kiểm** đổ — nên chín dòng lỗi khác, đã được tính đúng kèm số dòng và tên
    ô, không bao giờ tới được người dùng.
    """
    rows = [
        _row_with(PAYMENT_TERMS, unique_code("DK_OK1"), {"due_days": "30"}),
        _row_with(PAYMENT_TERMS, unique_code("DK_BAD2"), {"due_days": "ba mươi"}),
        _row_with(PAYMENT_TERMS, unique_code("DK_BAD3"), {"due_days": "10", "discount_days": "40"}),
    ]
    report = _run(session_factory, dataset_alpha, scope, tmp_path, PAYMENT_TERMS, rows)
    assert not report.is_valid
    by_row = {error.row: error.code for error in report.errors}
    assert by_row.get(3) == "import.not_integer"
    assert by_row.get(4) == "import.row_rule_violated", "lỗi của dòng khác đã bị nuốt mất"


@pytest.mark.db
def test_a_value_that_rounds_over_the_column_precision_is_caught(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """R3-C1: `CAST(… AS numeric(p, s))` **làm tròn theo `s` rồi mới kiểm `p`**.

    `partners.credit_limit` là `Numeric(20, 6)` — mười bốn chữ số phần nguyên.
    `"99999999999999.9999999"` có đúng mười bốn, nên phép đếm chữ số của bản đầu
    cho qua; `CAST` làm tròn thành `100000000000000.000000` (mười lăm) và tràn.

    Đây là vế thứ hai của C1, và nó dẫn tới **đúng** hậu quả cũ: transaction
    hỏng, `staging.clear()` ném chồng lên, người dùng mất cả báo cáo lỗi.
    """
    row = _row_with("partners", unique_code("DT_ROUND"), {"credit_limit": "99999999999999.9999999"})
    report = _run(session_factory, dataset_alpha, scope, tmp_path, "partners", [row], commit=True)
    assert not report.is_valid
    assert not report.committed
    assert "import.out_of_range" in {error.code for error in report.errors}


@pytest.mark.db
def test_a_value_that_fits_exactly_is_not_flagged(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    scope: RequestScope,
    tmp_path: Path,
) -> None:
    """Đối chứng: phép kiểm miền không được chặn giá trị **vừa khít**.

    Không có nó, một bản sửa quá tay (trừ hao rộng, hoặc đếm nhầm chữ số) vẫn
    cho mọi test trên xanh — nó chặn tất cả, kể cả thứ phải cho qua.
    """
    row = _row_with("partners", unique_code("DT_FIT"), {"credit_limit": "99999999999999.999999"})
    report = _run(session_factory, dataset_alpha, scope, tmp_path, "partners", [row], commit=True)
    assert report.is_valid, report.errors
    assert report.committed
