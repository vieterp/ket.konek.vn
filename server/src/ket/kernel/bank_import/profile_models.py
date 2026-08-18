"""Hồ sơ định dạng sao kê của một ngân hàng (RT-26, FR-BNK-032).

Một dòng ở đây mô tả **cách đọc** tệp sao kê của một ngân hàng: cột nào là ngày,
ngày viết theo khuôn nào, số tiền nằm ở một cột có dấu hay hai cột nợ/có, dấu
phân cách phần nghìn và phần thập phân là gì.

**Vì sao là dữ liệu chứ không mã.** Viết một bộ đọc cho mỗi ngân hàng nghĩa là
mỗi lần khách hàng mở tài khoản ở nhà băng thứ mười một thì phải phát hành lại
phần mềm — trong khi thứ khác nhau giữa mười nhà băng ấy chỉ là mười cấu hình.
Đây cũng đúng mục tiêu chất lượng #2 của plan.md: *cấu hình thay vì sửa code*.

**Cột định vị theo TÊN tiêu đề, không theo chỉ số.** Một hồ sơ khai
`date_col = "Ngày giao dịch"` chứ không `date_col = 3`. Ngân hàng chèn thêm một
cột vào giữa bảng là chuyện xảy ra thật, và với chỉ số thì hậu quả im lặng: cột
"số tiền" trở thành cột "số dư", mọi dòng vẫn đọc được, mọi con số đều sai.
Với tên thì lượt nhập dừng ngay và nói tên cột nó không tìm thấy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.auditing.listener import Audited
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

PROFILE_TABLE_NAME: Final[str] = "bank_statement_profiles"

NAME_MAX_LENGTH: Final[int] = 100
COLUMN_NAME_MAX_LENGTH: Final[int] = 100
"""Trần cho một tên cột lấy từ tệp ngân hàng. Rộng vì tiêu đề của sao kê hay là
cả một câu ("Số tiền ghi có (VND)"), và trần này chỉ để chặn dán nhầm cả một ô
dữ liệu vào ô cấu hình."""

DATE_FORMAT_MAX_LENGTH: Final[int] = 40


class StatementFileKind(StrEnum):
    """Dạng tệp mà ngân hàng phát ra."""

    XLSX = "xlsx"
    CSV = "csv"
    MT940 = "mt940"
    """Chuẩn điện văn SWIFT. Có trong danh sách vì hồ sơ khai được ngay từ bây
    giờ, nhưng **bộ đọc thuộc phase 6**: MT940 không phải một bảng có cột mà là
    một định dạng bản ghi thẻ (`:61:`, `:86:`), tức một bộ đọc thứ hai chứ không
    một nhánh `if`. Dựng nó khi chưa có một tệp MT940 thật để đối chiếu là dựng
    theo trí tưởng tượng."""


class AmountSignRule(StrEnum):
    """Cách hiểu **dấu** của một cột số tiền gộp.

    Chỉ có nghĩa khi hồ sơ dùng **một** cột số tiền. Sao kê hai cột (nợ/có) tự
    nó đã nói chiều tiền, nên `sign_rule` phải để trống ở đó — có `CHECK` canh.
    """

    SIGNED = "signed"
    """Số đã mang dấu; **dương = tiền vào**. Dạng phổ biến của tệp CSV."""

    DEBIT_POSITIVE = "debit_positive"
    """Số luôn dương, và nó là tiền **ra**. Gặp ở sao kê chỉ liệt kê khoản chi."""


CSV_DELIMITER_LENGTH: Final[int] = 1

_FILE_KINDS: Final[str] = ", ".join(f"'{item.value}'" for item in StatementFileKind)
_SIGN_RULES: Final[str] = ", ".join(f"'{item.value}'" for item in AmountSignRule)


def _profile_table_args() -> tuple[SchemaItem, ...]:
    """Ràng buộc của bảng — mỗi cái chặn một hồ sơ **đọc được nhưng sai**.

    Khai `CHECK` tường minh chứ không qua `Enum(create_constraint=True)`, cùng lý
    do H90: mọi enum của dự án dùng `create_constraint=False`, và bật riêng một
    cái tạo ra ràng buộc mà `compare_metadata` của Alembic không đối chiếu được.

    Vì sao ràng buộc ở **DB** chứ chỉ ở tầng API: gói cấu hình của phase 5 chạy
    SQL thẳng (đường ghi không qua Pydantic), và một hồ sơ sai khai qua đường ấy
    sẽ hỏng ở lúc người dùng nhập sao kê — cách chỗ sai một quãng rất xa.
    """
    return (
        UniqueConstraint("bank_id", "name", name=f"uq_{PROFILE_TABLE_NAME}_bank_name"),
        CheckConstraint(f"file_kind IN ({_FILE_KINDS})", name="file_kind_known"),
        CheckConstraint(
            f"sign_rule IS NULL OR sign_rule IN ({_SIGN_RULES})", name="sign_rule_known"
        ),
        CheckConstraint("header_row >= 1", name="header_row_positive"),
        # Một cột gộp **hoặc** cặp nợ/có, không lẫn lộn, và `sign_rule` chỉ đi
        # với cột gộp. Không có ràng buộc này thì một hồ sơ khai cả ba cột sẽ
        # chạy — bộ đọc buộc phải chọn một, và lựa chọn ấy là một quy ước ngầm
        # không ai đọc được từ cấu hình.
        CheckConstraint(
            "(amount_col IS NOT NULL AND sign_rule IS NOT NULL "
            " AND debit_col IS NULL AND credit_col IS NULL)"
            " OR (amount_col IS NULL AND sign_rule IS NULL"
            "     AND (debit_col IS NOT NULL OR credit_col IS NOT NULL))",
            name="amount_shape_is_one_of_two",
        ),
        # Ba ký tự phải khác nhau đôi một, nếu không câu `1.234,50` không phân
        # tích được: dấu phần nghìn trùng dấu thập phân biến `1.234` thành một
        # con số mơ hồ, còn dấu ngăn cột trùng một trong hai thì mỗi số bị cắt
        # thành hai ô — và cả hai hỏng **im lặng**, ra một con số hợp lệ khác.
        CheckConstraint("decimal_sep <> thousand_sep", name="separators_differ"),
        CheckConstraint(
            "csv_delimiter IS NULL"
            " OR (csv_delimiter <> decimal_sep"
            "     AND (thousand_sep IS NULL OR csv_delimiter <> thousand_sep))",
            name="csv_delimiter_distinct",
        ),
    )


class BankStatementProfile(Audited, RowVersioned, DatasetBase):
    """Cách đọc tệp sao kê của **một** ngân hàng.

    `Audited` + `RowVersioned` vì đây là cấu hình **người sửa**, và sửa sai một
    ô ở đây làm lệch mọi số tiền của mọi lượt nhập sau đó — "ai đổi dấu thập
    phân, lúc nào" là câu người ta sẽ hỏi.

    Không có `branch_id`: định dạng tệp là tính chất của **ngân hàng**, không của
    chi nhánh doanh nghiệp. Thêm cột ấy sẽ kéo bảng vào tầm cổng
    `test_rls_policy_coverage` và bắt phải nghĩ ra một policy cho một câu hỏi
    không tồn tại.
    """

    __tablename__ = PROFILE_TABLE_NAME
    __table_args__ = _profile_table_args()

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    bank_id: Mapped[int] = mapped_column(
        Integer,
        # `RESTRICT` theo quy ước của danh mục: xóa một ngân hàng còn hồ sơ sẽ
        # bỏ lại một cấu hình mồ côi mà không màn hình nào hiện ra.
        ForeignKey("banks.id", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    """Nhãn người dùng đọc ("Sao kê Internet Banking", "Sổ phụ gửi qua email").

    Duy nhất **trong một ngân hàng**, không toàn hệ thống: một nhà băng phát ra
    nhiều định dạng khác nhau tùy kênh, và đó chính là ca hồ sơ này phục vụ."""

    file_kind: Mapped[str] = mapped_column(String(10), nullable=False)

    header_row: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    """Dòng chứa tiêu đề cột, đánh số **như trong Excel** (dòng đầu là 1).

    Là cấu hình chứ không hằng số 1 như tệp mẫu nhập liệu (`reader.HEADER_ROW`):
    sao kê ngân hàng thường mở đầu bằng vài dòng tiêu đề đơn vị, số tài khoản,
    kỳ sao kê — và số dòng ấy khác nhau giữa các nhà băng."""

    date_col: Mapped[str] = mapped_column(String(COLUMN_NAME_MAX_LENGTH), nullable=False)
    date_format: Mapped[str] = mapped_column(String(DATE_FORMAT_MAX_LENGTH), nullable=False)
    """Khuôn ngày theo cú pháp `strptime` (`%d/%m/%Y`).

    Chỉ dùng khi ô là **chuỗi**. Tệp .xlsx thường lưu ngày dưới dạng ngày thật,
    và khi đó bộ đọc lấy thẳng giá trị ấy — ép một ngày đã đúng đi qua một khuôn
    chuỗi là tạo thêm một chỗ để sai."""

    debit_col: Mapped[str | None] = mapped_column(String(COLUMN_NAME_MAX_LENGTH), nullable=True)
    """Cột tiền **ra**. Đi cùng `credit_col` ở dạng sao kê hai cột."""

    credit_col: Mapped[str | None] = mapped_column(String(COLUMN_NAME_MAX_LENGTH), nullable=True)
    """Cột tiền **vào**."""

    amount_col: Mapped[str | None] = mapped_column(String(COLUMN_NAME_MAX_LENGTH), nullable=True)
    """Cột số tiền gộp, đi cùng `sign_rule`."""

    sign_rule: Mapped[str | None] = mapped_column(String(20), nullable=True)

    ref_col: Mapped[str | None] = mapped_column(String(COLUMN_NAME_MAX_LENGTH), nullable=True)
    """Số tham chiếu / số chứng từ của ngân hàng — chìa khóa đối chiếu ở phase 6."""

    description_col: Mapped[str | None] = mapped_column(
        String(COLUMN_NAME_MAX_LENGTH), nullable=True
    )
    balance_col: Mapped[str | None] = mapped_column(String(COLUMN_NAME_MAX_LENGTH), nullable=True)
    """Số dư sau giao dịch. Không bắt buộc, nhưng có thì phase 6 kiểm tra chéo
    được: số dư cuối kỳ trước cộng phát sinh phải bằng số dư dòng cuối."""

    decimal_sep: Mapped[str] = mapped_column(
        String(1), nullable=False, default=".", server_default="."
    )
    thousand_sep: Mapped[str | None] = mapped_column(String(1), nullable=True)
    """`NULL` = tệp không dùng dấu phần nghìn. Khác chuỗi rỗng ở chỗ `CHECK
    (decimal_sep <> thousand_sep)` so được — một chuỗi rỗng thì luôn khác mọi
    dấu, nên nó lọt qua ràng buộc mà không mang thêm thông tin nào."""

    csv_delimiter: Mapped[str | None] = mapped_column(String(CSV_DELIMITER_LENGTH), nullable=True)
    """Dấu ngăn cột của tệp CSV. `NULL` cho tệp .xlsx.

    **Lệch khỏi bảng phác trong plan.md, có chủ đích.** Bảng ở plan không có cột
    này, nhưng thiếu nó thì bộ đọc phải *đoán*: một tệp Việt Nam dùng dấu phẩy
    làm dấu thập phân thì dấu ngăn cột của nó không thể là dấu phẩy, nên bộ đọc
    sẽ phải suy ra `;`. Suy đoán ấy đúng phần lớn thời gian — và ca nó sai là ca
    mỗi con số bị cắt làm đôi, im lặng. Một ô cấu hình rẻ hơn một suy đoán."""
