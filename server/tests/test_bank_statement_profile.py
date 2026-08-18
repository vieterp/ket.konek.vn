"""Nhập sao kê ngân hàng theo hồ sơ định dạng (lát 3C-2, RT-26, FR-BNK-032).

Khẳng định trung tâm: **hai định dạng khác hẳn nhau đi qua cùng một bộ đọc**, và
thứ khác nhau giữa chúng nằm trọn trong một dòng cấu hình. Đó là cả nội dung của
RT-26 — nếu phải sửa mã cho mỗi nhà băng thì bảng hồ sơ không giải quyết gì.

Hai định dạng được dựng để **khác nhau ở mọi trục** mà hồ sơ mô tả:

| | Dạng A (kiểu VCB) | Dạng B (kiểu ACB) |
| --- | --- | --- |
| Tệp | .xlsx | .csv có BOM |
| Tiêu đề | dòng 6 (5 dòng đầu là đầu thư) | dòng 1 |
| Số tiền | hai cột nợ/có | một cột có dấu |
| Ngày | `dd/mm/yyyy` dạng chuỗi | `yyyy-mm-dd` |
| Phân cách | `,` nghìn — `.` thập phân | `.` nghìn — `,` thập phân |
| Ngăn cột | — | `;` |

Nếu bộ đọc lén dựa vào một quy ước cố định nào (dòng tiêu đề là 1, dấu chấm là
dấu thập phân, số tiền là một cột), một trong hai dạng sẽ đỏ.

**Nợ chặn phase 6** (chốt với người dùng 2026-08-18): fixture ở đây do chúng ta
tự dựng theo hình dạng đã biết của sao kê Việt Nam. Trước khi module Bank của
phase 6 nhận sao kê thật, phải đối chiếu lại với **tệp xuất thật của VCB và
ACB** — một fixture tự dựng chứng minh bộ đọc *có thể* xử lý sự khác biệt, nó
không chứng minh chúng ta đã đoán đúng sự khác biệt.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import all_branch_codes, branch_ids, unique_code
from ket.kernel.bank_import.profile_models import (
    NAME_MAX_LENGTH,
    AmountSignRule,
    BankStatementProfile,
    StatementFileKind,
)
from ket.kernel.bank_import.profile_parser import (
    StatementIssue,
    _read_decimal,
    parse_statement,
)
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    BankStatementColumnMissingError,
    BankStatementFileUnreadableError,
    BankStatementFormatUnsupportedError,
)
from ket.kernel.master_data.merge_service import merge_records
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.registry import REGISTRY
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

FIXTURES = Path(__file__).parent / "fixtures" / "bank_statements"

VCB_HEADER_ROW = 6
"""Sao kê thật mở đầu bằng khối tên đơn vị, số tài khoản, kỳ sao kê. Đặt tiêu đề
ở dòng 6 để bộ đọc không thể lấy đúng bằng một hằng số."""


def _vcb_like_profile() -> BankStatementProfile:
    """Dạng A: .xlsx, tiêu đề dòng 6, hai cột nợ/có, `dd/mm/yyyy`, nghìn = `,`."""
    return BankStatementProfile(
        bank_id=1,
        name="Sao kê Internet Banking",
        file_kind=StatementFileKind.XLSX.value,
        header_row=VCB_HEADER_ROW,
        date_col="Ngày giao dịch",
        date_format="%d/%m/%Y",
        debit_col="Số tiền ghi nợ",
        credit_col="Số tiền ghi có",
        ref_col="Số tham chiếu",
        description_col="Diễn giải",
        balance_col="Số dư",
        decimal_sep=".",
        thousand_sep=",",
    )


def _acb_like_profile() -> BankStatementProfile:
    """Dạng B: .csv, tiêu đề dòng 1, một cột có dấu, `yyyy-mm-dd`, nghìn = `.`."""
    return BankStatementProfile(
        bank_id=1,
        name="Sổ phụ gửi qua email",
        file_kind=StatementFileKind.CSV.value,
        header_row=1,
        date_col="Ngày giao dịch",
        date_format="%Y-%m-%d",
        amount_col="Số tiền",
        sign_rule=AmountSignRule.SIGNED.value,
        ref_col="Số tham chiếu",
        description_col="Diễn giải",
        balance_col="Số dư",
        decimal_sep=",",
        thousand_sep=".",
        csv_delimiter=";",
    )


def _vcb_like_file(rows: list[list[object]] | None = None) -> BytesIO:
    """Tệp .xlsx dạng A, kèm khối đầu thư mà sao kê thật nào cũng có."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "sao_ke_202608"
    sheet.append(["NGÂN HÀNG TMCP NGOẠI THƯƠNG VIỆT NAM"])
    sheet.append(["Số tài khoản: 0011001234567"])
    sheet.append(["Chủ tài khoản: CÔNG TY TNHH KONEK"])
    sheet.append(["Kỳ sao kê: 01/08/2026 - 31/08/2026"])
    sheet.append([])
    sheet.append(
        [
            "Ngày giao dịch",
            "Số tham chiếu",
            "Diễn giải",
            "Số tiền ghi nợ",
            "Số tiền ghi có",
            "Số dư",
        ]
    )
    for row in rows if rows is not None else _VCB_ROWS:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


_VCB_ROWS: list[list[object]] = [
    [
        "01/08/2026",
        "FT26214001",
        "Thu tien hang KH Minh Anh",
        None,
        "12,500,000.50",
        "312,500,000.50",
    ],
    ["02/08/2026", "FT26214002", "Thanh toan NCC Hoa Phat", "4,250,000.00", None, "308,250,000.50"],
    ["03/08/2026", "FT26214003", "Phi quan ly tai khoan", "55,000.00", None, "308,195,000.50"],
    [],
    ["Tổng phát sinh", None, None, "4,305,000.00", "12,500,000.50", None],
]
"""Ba dòng giao dịch, một dòng trống, rồi khối chân bảng — đúng hình dạng tệp thật.

Dòng chân bảng **không** bị bỏ qua bởi luật "dòng trống": nó có nội dung. Nó trở
thành một dòng lỗi, và đó là hành vi đúng — người dùng phải thấy rằng bộ đọc
không hiểu nó, chứ không để nó lặng lẽ thành một giao dịch."""


# ------------------------------------------------- hai định dạng, một bộ đọc


def test_the_two_column_xlsx_format_is_read_with_the_right_signs() -> None:
    """Dạng A: tiền vào dương, tiền ra âm — quy ước dấu của module.

    Đảo dấu là lỗi nghiệp vụ đắt nhất mà bộ đọc này có thể gây ra: một khoản chi
    ghi thành khoản thu làm sai số dư, sai đối chiếu, và sai cả báo cáo lưu
    chuyển tiền tệ ở phase 10.
    """
    result = parse_statement(_vcb_like_file(), _vcb_like_profile())

    assert [line.amount for line in result.lines] == [
        Decimal("12500000.50"),
        Decimal("-4250000.00"),
        Decimal("-55000.00"),
    ]
    assert [line.booked_on for line in result.lines] == [
        date(2026, 8, 1),
        date(2026, 8, 2),
        date(2026, 8, 3),
    ]
    assert result.lines[0].reference == "FT26214001"
    assert result.lines[0].description == "Thu tien hang KH Minh Anh"
    assert result.lines[0].balance == Decimal("312500000.50")


def test_the_single_column_csv_format_reads_the_same_shape() -> None:
    """Dạng B — tệp thật trên đĩa, có BOM và dấu phân cách ngược hẳn dạng A.

    Đọc từ tệp đã commit chứ không dựng trong bộ nhớ: BOM và xuống dòng CRLF là
    hai thứ chỉ tồn tại trong byte thật, và cả hai đều đủ sức làm hỏng phép tra
    cột đầu tiên.
    """
    with (FIXTURES / "acb-like-signed-amount.csv").open("rb") as source:
        result = parse_statement(source, _acb_like_profile())

    assert [line.amount for line in result.lines] == [
        Decimal("12500000.50"),
        Decimal("-4250000.00"),
        Decimal("-55000.00"),
    ]
    assert result.lines[0].booked_on == date(2026, 8, 1)
    assert result.lines[0].reference == "FT26214001"


def test_both_formats_produce_the_same_normalised_lines() -> None:
    """Cùng ba giao dịch, hai tệp không có gì chung — đầu ra phải trùng khớp.

    Đây là khẳng định mà RT-26 tồn tại vì nó: phase 6 nhận `StatementLine` và
    không bao giờ phải biết tệp đến từ nhà băng nào.
    """
    xlsx = parse_statement(_vcb_like_file(), _vcb_like_profile())
    with (FIXTURES / "acb-like-signed-amount.csv").open("rb") as source:
        csv_result = parse_statement(source, _acb_like_profile())

    def comparable(result: object) -> list[tuple[date, Decimal, str | None]]:
        assert hasattr(result, "lines")
        return [(line.booked_on, line.amount, line.reference) for line in result.lines]

    assert comparable(xlsx) == comparable(csv_result)


# ------------------------------------------------------------ lỗi cấu trúc


def test_a_column_the_profile_names_but_the_file_lacks_stops_the_whole_run() -> None:
    """Hồ sơ không khớp tệp = **một** việc phải sửa, nên một lỗi chứ không N lỗi."""
    profile = _vcb_like_profile()
    profile.credit_col = "Ghi có"
    with pytest.raises(BankStatementColumnMissingError) as error:
        parse_statement(_vcb_like_file(), profile)
    assert "Ghi có" in str(error.value)


def test_a_header_row_past_the_end_of_the_file_is_refused() -> None:
    profile = _vcb_like_profile()
    profile.header_row = 500
    with pytest.raises(BankStatementColumnMissingError):
        parse_statement(_vcb_like_file(), profile)


def test_a_file_that_is_not_a_workbook_is_refused() -> None:
    with pytest.raises(BankStatementFileUnreadableError):
        parse_statement(BytesIO(b"khong phai xlsx"), _vcb_like_profile())


def test_mt940_says_plainly_that_this_build_cannot_read_it() -> None:
    """Không giả vờ đọc được: `file_kind` khai được nhưng bộ đọc thuộc phase 6."""
    profile = _vcb_like_profile()
    profile.file_kind = StatementFileKind.MT940.value
    with pytest.raises(BankStatementFormatUnsupportedError):
        parse_statement(BytesIO(b":20:STATEMENT"), profile)


# --------------------------------------------------------- lỗi từng dòng


def test_the_footer_block_becomes_a_row_error_not_a_transaction() -> None:
    """Khối chân bảng có nội dung nên nó **không** được im lặng bỏ qua.

    Bỏ qua mọi dòng đọc không nổi là cách một tệp cắt cụt giữa chừng vẫn báo
    "nhập thành công": người dùng đối chiếu số dư mới phát hiện thiếu giao dịch,
    thường là vài tuần sau.
    """
    result = parse_statement(_vcb_like_file(), _vcb_like_profile())
    assert len(result.lines) == 3
    assert not result.is_clean
    footer = [item for item in result.issues if item.row_number == 11]
    assert footer, "dòng chân bảng phải thành một dòng lỗi có số dòng thật"
    assert {item.code for item in footer} <= {
        "bank_statement.date_invalid",
        "bank_statement.both_sides_filled",
    }


def test_a_row_with_money_in_both_columns_is_refused_not_netted() -> None:
    """Cộng bù hai cột sẽ ra một con số hợp lệ cho một dòng vô nghĩa — lỗi im.

    Dòng có tiền ở cả hai cột nghĩa là tệp bị dán lệch cột hoặc hồ sơ khai nhầm.
    Cả hai đều phải dừng lại và hỏi người dùng.
    """
    rows: list[list[object]] = [
        ["01/08/2026", "FT1", "Dán lệch cột", "1,000.00", "2,000.00", "5,000.00"]
    ]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert not result.lines
    assert [item.code for item in result.issues] == ["bank_statement.both_sides_filled"]


def test_a_row_with_no_money_at_all_is_reported_once() -> None:
    rows: list[list[object]] = [["01/08/2026", "FT1", "Không có số tiền", None, None, None]]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert [item.code for item in result.issues] == ["bank_statement.amount_missing"]


def test_an_unparseable_amount_is_reported_once_with_the_value() -> None:
    """Một ô hỏng = **một** lỗi. Bản đầu báo "không phải số" rồi "thiếu số tiền"."""
    rows: list[list[object]] = [["01/08/2026", "FT1", "Số hỏng", "1.2.3.4x", None, None]]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert [item.code for item in result.issues] == ["bank_statement.amount_invalid"]
    assert result.issues[0].value == "1.2.3.4x"


def test_a_bad_date_names_the_expected_format() -> None:
    rows: list[list[object]] = [["2026-08-01", "FT1", "Sai khuôn ngày", None, "1,000.00", None]]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert [item.code for item in result.issues] == ["bank_statement.date_invalid"]
    assert "%d/%m/%Y" in result.issues[0].message


def test_a_real_date_cell_is_used_as_is_instead_of_reparsed() -> None:
    """Excel lưu ngày dưới dạng **ngày**; ép nó qua `date_format` là tự tạo lỗi.

    Đây là ca xảy ra thường xuyên nhất trong thực tế: người dùng mở tệp ngân
    hàng ra rồi lưu lại bằng Excel, và Excel đổi cột ngày sang kiểu ngày thật.
    Với bản đọc chỉ biết chuỗi thì mọi dòng của tệp ấy thành lỗi khuôn ngày.
    """
    rows: list[list[object]] = [[date(2026, 8, 9), "FT1", "Ngày thật", None, "1,000.00", None]]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert result.is_clean, result.issues
    assert result.lines[0].booked_on == date(2026, 8, 9)


def test_a_debit_positive_profile_turns_positive_numbers_into_money_out() -> None:
    """`sign_rule` là thứ duy nhất phân biệt hai cách đọc cùng một cột số."""
    profile = _acb_like_profile()
    profile.sign_rule = AmountSignRule.DEBIT_POSITIVE.value
    with (FIXTURES / "acb-like-signed-amount.csv").open("rb") as source:
        result = parse_statement(source, profile)
    assert all(line.amount <= 0 for line in result.lines)


# ------------------------------------------------ ràng buộc của bảng hồ sơ


@pytest.fixture
def scope(dataset_alpha: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())


def _a_bank(session_factory: sessionmaker[Session], scope: RequestScope) -> int:
    spec = REGISTRY.get("banks")
    assert spec is not None
    with unit_of_work(session_factory, scope) as session:
        service: MasterDataService[Bank] = MasterDataService(session, Bank)
        record = service.create(code=unique_code("NH_SK"), name="Ngân hàng thử")
        session.flush()
        return record.id


@pytest.mark.db
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        pytest.param(
            {"amount_col": "Số tiền", "sign_rule": "signed", "debit_col": "Nợ"},
            "cột gộp lẫn với cột nợ/có",
            id="lẫn-hai-hình-dạng",
        ),
        pytest.param(
            {"amount_col": "Số tiền", "sign_rule": None},
            "cột gộp mà không khai cách hiểu dấu",
            id="thiếu-sign-rule",
        ),
        pytest.param(
            {"debit_col": "Nợ", "credit_col": "Có", "sign_rule": "signed"},
            "sign_rule vô nghĩa với sao kê hai cột",
            id="thừa-sign-rule",
        ),
        pytest.param(
            {"debit_col": "Nợ", "decimal_sep": ",", "thousand_sep": ","},
            "hai dấu trùng nhau thì `1,234` là số mơ hồ",
            id="trùng-dấu-phân-cách",
        ),
        pytest.param(
            {"debit_col": "Nợ", "decimal_sep": ",", "csv_delimiter": ","},
            "dấu ngăn cột trùng dấu thập phân thì mỗi số bị cắt làm đôi",
            id="ngăn-cột-trùng-thập-phân",
        ),
        pytest.param(
            {"debit_col": "Nợ", "header_row": 0},
            "dòng tiêu đề đánh số từ 1",
            id="header-row-không-hợp-lệ",
        ),
        pytest.param(
            {"debit_col": "Nợ", "file_kind": "pdf"},
            "dạng tệp không có bộ đọc",
            id="file-kind-lạ",
        ),
    ],
)
def test_the_database_refuses_a_profile_that_would_read_numbers_wrong(
    changes: dict[str, object],
    reason: str,
    session_factory: sessionmaker[Session],
    scope: RequestScope,
) -> None:
    """Ràng buộc ở **DB**, không chỉ ở tầng API (cùng lý do H90).

    Gói cấu hình của phase 5 chạy SQL thẳng — một đường ghi không qua Pydantic —
    nên một hồ sơ sai khai qua đường ấy sẽ chỉ lộ ra lúc người dùng nhập sao kê,
    cách chỗ sai rất xa. Mỗi tham số ở đây là một hồ sơ **đọc được nhưng sai**.
    """
    bank_id = _a_bank(session_factory, scope)
    values: dict[str, object] = {
        "bank_id": bank_id,
        "name": unique_code("HS"),
        "file_kind": "xlsx",
        "header_row": 1,
        "date_col": "Ngày",
        "date_format": "%d/%m/%Y",
        "decimal_sep": ".",
        **changes,
    }
    with pytest.raises(IntegrityError), unit_of_work(session_factory, scope) as session:
        session.add(BankStatementProfile(**values))
        session.flush()


@pytest.mark.db
def test_a_valid_profile_of_each_shape_is_accepted(
    session_factory: sessionmaker[Session], scope: RequestScope
) -> None:
    """Đối chứng: bảy ràng buộc trên không được chặt tới mức chặn cả hồ sơ đúng.

    Không có phép đối chứng này thì một `CHECK` viết sai chiều vẫn cho bộ test
    trên xanh — nó chặn mọi thứ, kể cả thứ phải cho qua.
    """
    bank_id = _a_bank(session_factory, scope)
    with unit_of_work(session_factory, scope) as session:
        for profile in (_vcb_like_profile(), _acb_like_profile()):
            profile.bank_id = bank_id
            profile.name = unique_code("HS_OK")
            session.add(profile)
        session.flush()


def test_a_real_number_cell_keeps_its_decimal_part() -> None:
    """Ô số **thực** trong .xlsx: dấu phân cách của hồ sơ không được đụng vào nó.

    Review vòng 2, C2 — sai đúng mười lần, im lặng. openpyxl trả kiểu dấu phẩy
    động cho mọi ô không nguyên, mà bản đầu chỉ nhận `int`/`Decimal`; số thực vì
    thế rơi xuống nhánh **chuỗi**, thành `"12500000.5"`, rồi `thousand_sep = "."`
    của hồ sơ xóa nốt dấu chấm → `125000005`.

    Fixture .xlsx của tệp này viết số dưới dạng **chuỗi** (đúng như nhiều sao kê
    thật), nên toàn bộ nhánh ấy chưa từng chạy. Ca số nguyên thì tình cờ đúng —
    đó là lý do lỗi chỉ nổ ở cột số dư và tài khoản ngoại tệ.
    """
    rows: list[list[object]] = [
        ["01/08/2026", "FT1", "Ô số thực", None, 12500000.50, 312500000.50],
        ["02/08/2026", "FT2", "Ô số nguyên", None, 4250000, 316750000.50],
    ]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())

    assert result.is_clean, result.issues
    assert [line.amount for line in result.lines] == [
        Decimal("12500000.50"),
        Decimal("4250000"),
    ]
    assert result.lines[0].balance == Decimal("312500000.50")


@pytest.mark.parametrize("hostile", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_amount_is_refused(hostile: str) -> None:
    """`Decimal("NaN")` hợp lệ về cú pháp, và PostgreSQL `numeric` **nhận** nó.

    Một dòng như vậy vào sổ tiền gửi ở phase 6 thì mọi phép cộng sau đó ra `NaN`
    và mọi phép so `> 0` trả `false` — một dòng sao kê làm hỏng số dư cả kỳ, và
    không có gì trong báo cáo chỉ ra dòng nào gây ra.
    """
    rows: list[list[object]] = [["01/08/2026", "FT1", "Số vô định", None, hostile, None]]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert [item.code for item in result.issues] == ["bank_statement.amount_invalid"]


def _bank_merge_hooks() -> tuple[object, ...]:
    """Hook của danh mục ngân hàng, lấy **từ registry** chứ không dựng tay.

    Dựng tay một `BankStatementProfileMergeHook()` ở đây sẽ đo một đường mã mà
    người dùng thật không đi qua: `routers/master_data.py` truyền
    `spec.merge_hooks`, nên thứ cần canh là danh mục **có khai** hook ấy.
    """
    spec = REGISTRY.get("banks")
    assert spec is not None
    return tuple(spec.merge_hooks)


@pytest.mark.db
def test_merging_two_banks_keeps_both_profiles_with_distinct_names(
    session_factory: sessionmaker[Session], scope: RequestScope, dataset_alpha: DatasetRef
) -> None:
    """Gộp hai ngân hàng cùng có hồ sơ trùng tên (FR-SYS-016).

    Review vòng 2, M2: `BankStatementProfileMergeHook` không có phép kiểm hành vi
    nào — hai đột biến (`before_move` thành no-op, `_free_name` bỏ cắt theo trần
    cột) đều sống sót. Cổng `test_master_data_merge` chỉ đối chiếu **tập** danh
    mục có khai hook với tập **cần** hook; nó không gọi hook.

    Ca được đo là ca thường gặp nhất của chính yêu cầu gộp: hai nhà băng trùng
    thì hai hồ sơ thường **cùng tên** ("Sao kê Internet Banking"). Không có hook,
    lượt gộp đổ với `UniqueViolation` nêu tên một chỉ mục nội bộ.

    Đổi tên chứ không xóa, và đó là điểm khác `PartnerBankAccountMergeHook`: tên
    chỉ là nhãn, còn nội dung là mười mấy ô cấu hình có thể khác nhau hoàn toàn —
    xóa một trong hai là vứt đi cách đọc một định dạng tệp.
    """
    long_name = "S" * NAME_MAX_LENGTH
    with unit_of_work(session_factory, scope) as session:
        service: MasterDataService[Bank] = MasterDataService(session, Bank)
        source = service.create(code=unique_code("NH_NGUON"), name="Ngân hàng nguồn")
        target = service.create(code=unique_code("NH_DICH"), name="Ngân hàng đích")
        session.flush()
        source_id, target_id = source.id, target.id
        for bank_id in (source_id, target_id):
            for name in ("Sao kê Internet Banking", long_name):
                session.add(
                    BankStatementProfile(
                        bank_id=bank_id,
                        name=name,
                        file_kind="xlsx",
                        header_row=1,
                        date_col="Ngày",
                        date_format="%d/%m/%Y",
                        debit_col="Nợ",
                        decimal_sep=".",
                    )
                )
        session.flush()

    with unit_of_work(session_factory, scope) as session:
        merge_records(
            session,
            Bank,
            source_id=source_id,
            target_id=target_id,
            # Gộp là thao tác toàn công ty (quyết định của lát 3B-2): người thực
            # hiện phải được gán **mọi** chi nhánh. Đọc danh sách lúc chạy chứ
            # không viết cứng — dataset dùng chung giữa mọi tệp test, nên một tập
            # rỗng đúng hôm nay sẽ sai ngay khi tệp test khác tạo chi nhánh đầu
            # tiên, và test sẽ đỏ vì một lý do không liên quan gì tới nó.
            actor_branch_ids=frozenset(
                branch_ids(
                    session_factory,
                    dataset_alpha,
                    all_branch_codes(session_factory, dataset_alpha),
                ).values()
            ),
            hooks=_bank_merge_hooks(),
        )

    with unit_of_work(session_factory, scope) as session:
        names = sorted(
            session.scalars(
                select(BankStatementProfile.name).where(BankStatementProfile.bank_id == target_id)
            ).all()
        )
    assert len(names) == 4, "cả bốn hồ sơ phải sống sót — nội dung là dữ liệu, không phải nhãn"
    assert len(set(names)) == 4, "tên phải khác nhau đôi một"
    assert all(len(name) <= NAME_MAX_LENGTH for name in names), "tên mới vượt trần cột"


def test_a_negative_amount_where_the_profile_says_always_positive_is_reported() -> None:
    """Review vòng 2, H5: bộ đọc **ép dấu** thay vì hỏi.

    Ô `-1.000.000` ở cột ghi **nợ** đi qua `-abs(...)` và vẫn thành tiền ra. Gốc
    của ca ấy là bút toán **hoàn/điều chỉnh**: ngân hàng ghi số âm ở cột chi để
    nói "trả lại tiền vào tài khoản". Sau khi ép dấu, một khoản thu 5 triệu vào
    sổ thành một khoản chi 5 triệu — sai 10 triệu ở đối chiếu và ở báo cáo lưu
    chuyển tiền tệ phase 10.

    Quyết định (người dùng chốt 2026-08-18): **báo lỗi, không ép**. Im lặng sửa
    dữ liệu người dùng là thứ cả module này được viết ra để không làm.
    """
    rows: list[list[object]] = [["01/08/2026", "FT1", "Bút toán hoàn", "-1,000,000.00", None, None]]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert not result.lines
    assert [item.code for item in result.issues] == ["bank_statement.sign_conflicts_with_profile"]


def test_a_negative_amount_under_debit_positive_is_reported() -> None:
    """Cùng luật, ở dạng một cột gộp: `sign_rule` khai "luôn dương" thì âm là mâu thuẫn."""
    profile = _acb_like_profile()
    profile.sign_rule = AmountSignRule.DEBIT_POSITIVE.value
    with (FIXTURES / "acb-like-signed-amount.csv").open("rb") as source:
        result = parse_statement(source, profile)
    # Hai dòng âm của fixture thành mâu thuẫn dấu; dòng chân bảng vẫn là lỗi ngày
    # như mọi khi (xem `_VCB_ROWS`), nên khẳng định theo **sự có mặt**.
    assert "bank_statement.sign_conflicts_with_profile" in {item.code for item in result.issues}
    # Dòng dương vẫn đọc được và thành tiền **ra** — luật `debit_positive` còn nguyên.
    assert [line.amount for line in result.lines] == [Decimal("-12500000.50")]


def test_a_decimal_cell_is_read_without_going_through_repr() -> None:
    """R3-H2 — hồi quy do chính bản sửa C2 tạo ra.

    Bản sửa gộp `Real | Decimal` rồi gọi `Decimal(repr(value))`, mà
    `repr(Decimal("12.5"))` là chuỗi `"Decimal('12.5')"` → `InvalidOperation`
    ném **ngoài** khối `try` bên dưới. Hôm nay openpyxl không trả `Decimal`, nên
    lỗi này chờ nguồn dữ liệu thứ hai của phase 6 mới nổ.
    """
    rows: list[list[object]] = [
        ["01/08/2026", "FT1", "Ô Decimal", None, Decimal("12500000.50"), None]
    ]
    result = parse_statement(_vcb_like_file(rows), _vcb_like_profile())
    assert result.is_clean, result.issues
    assert result.lines[0].amount == Decimal("12500000.50")


def test_an_infinite_number_from_a_cell_is_refused() -> None:
    """R3-H3 — nợ M6 chưa đóng: nhánh `Real` mới **đi vòng qua** `is_finite()`.

    Trước bản sửa, một ô số cho ra `Infinity` thành `StatementLine(amount=Infinity)`
    với `issues` **rỗng**. PostgreSQL `numeric` nhận cả `NaN` lẫn `±Infinity`, nên
    phase 6 ghi được nó vào sổ tiền gửi và mọi phép cộng sau đó ra `Infinity` —
    một dòng sao kê làm hỏng số dư cả kỳ.

    Gọi thẳng `_read_decimal` thay vì đi qua `parse_statement`: openpyxl **không**
    ghi được `inf`/`nan` vào .xlsx (ô đọc lại thành rỗng), nên qua đường công khai
    thì nhánh `Real` không tới được hôm nay. Nó tới được từ phase 6, khi dòng sao
    kê nạp từ nguồn thứ hai — và đó chính là lúc phép chặn này phải đã có sẵn.
    """
    issues: list[StatementIssue] = []
    for value in (Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN")):
        issues.clear()
        parsed = _read_decimal(value, 2, "Số tiền ghi có", _vcb_like_profile(), issues)
        assert parsed is None, f"{value} lọt qua"
        assert [item.code for item in issues] == ["bank_statement.amount_invalid"]
