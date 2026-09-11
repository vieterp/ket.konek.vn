"""Hai loại chứng từ mang phần chênh của lượt điều chỉnh hóa đơn — lát 7F-2a.

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-11

Chạy **một lần cho mỗi schema dataset** như `0001`..`0036`, bằng `ket_owner`.

**Vì sao chứng từ điều chỉnh phải là chứng từ riêng.** `einvoices` cố ý không
mang cột tiền — nó đọc tổng từ chứng từ gốc, và đó chính là cách BR-EIV-07 thành
đúng theo **cấu trúc** chứ không theo một phép kiểm (`0032`, lát 7D). Một tờ hóa
đơn điều chỉnh tăng hoặc giảm chỉ khai **phần chênh**, nên nó không trỏ được vào
chứng từ gốc mang số tiền đầy đủ mà vẫn giữ bất biến ấy. Lát 7F-1 dừng ở đúng
đây và trả `409`; lát này mở đường bằng cách cho `sales` hai `kind` mới.

**Chiều nằm ở `kind`, không ở dấu của số tiền.** `totals_not_negative` cấm số âm
trên thân hóa đơn bán, và cấm đúng: một cột tiền nhận cả hai dấu buộc mọi phép
cộng — doanh thu, tuổi nợ, bảng kê bán ra — phải nhớ kiểm dấu trước khi cộng.
Hai `kind` thay vì một `kind` kèm cột chiều: với hai, `ADJUSTMENT_DECREASE` rơi
thẳng vào `REVERSING_KINDS` đã có từ 7C-2 và `ADJUSTMENT_INCREASE` rơi vào nhánh
hóa đơn thường, nên bộ dựng bút toán, sổ phụ và đối trừ **không mọc thêm nhánh
nào**. Một cột chiều thì mỗi nơi ấy phải đọc lại nó và tự suy ra — bảng quyết
định chép bốn lần vào mã.

**Hai nghiệp vụ định khoản chèn TẠI ĐÂY, khác `0028`.** Doctrine của
`seed._ensure_auto_posting_backfilled` là "chỗ trống tính theo từng
`document_type`", và nó đúng cho những lát mở một loại chứng từ **mới**: `0028`
để bộ nghiệp vụ `SAL` cho hàm ấy lấp, vì lúc đó `SAL` chưa có dòng nào. Lát này
thêm nghiệp vụ vào một `document_type` **đã có dòng**, tức đúng ca mà phép kiểm
theo loại chứng từ bỏ qua — để nguyên thì hai nghiệp vụ này chỉ tới được dataset
cấp mới sau hôm nay, và bản cài cũ với bản cài mới lặng lẽ khác nhau.

Lượt chèn vì thế khóa theo **`operation_code`**, không theo `document_type`, và
`WHERE NOT EXISTS` giữ nó an toàn khi chạy lại. Doctrine "không gieo lại thứ
người dùng đã xóa" không bị vi phạm: hai mã này chưa từng tồn tại ở dataset nào,
nên chưa ai xóa được chúng. `default_accounts` không phải chèn gì — cả hai dùng
lại `ar_trade`, `revenue_goods` và `sales_deduction` mà `0028` đã gieo.

**`_refresh_builtin_data` GIỮ Ở `0035`** — doctrine 5B M-1 không kích hoạt ở
revision này: nó không gieo báo cáo hay mẫu in builtin nào. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở
đây — dự án đã sập bẫy vị trí này ba lần (6A, 6G-1, 7A).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SALES_TABLE = "sales_invoices"
_SALES_DOCUMENT_TYPE = "SAL"

# Số trần có chú thích, cùng lối `0022`, `0032`, `0034`..`0036` — migration là
# ảnh chụp lịch sử, không import enum.
#
# `SalesInvoiceKind`: 0 = GOODS · 1 = SERVICE · 2 = RETURN · 3 = ALLOWANCE
#                     4 = AGENCY · 5 = ADJUSTMENT_INCREASE · 6 = ADJUSTMENT_DECREASE
# `PartnerKind`:      0 = CUSTOMER
_KIND_MIN = 0
_ADJUSTMENT_INCREASE = 5
_KIND_MAX = 6
_CUSTOMER = 0

_NEW_OPERATIONS: tuple[tuple[str, str, str, str, int], ...] = (
    (
        "dieu-chinh-tang-hoa-don",
        "Điều chỉnh tăng hóa đơn đã phát hành",
        "ar_trade",
        "revenue_goods",
        7,
    ),
    (
        "dieu-chinh-giam-hoa-don",
        "Điều chỉnh giảm hóa đơn đã phát hành",
        "sales_deduction",
        "ar_trade",
        8,
    ),
)
"""Cùng nội dung với `auto_posting_rules.csv` của cả hai gói dựng sẵn.

Chép ở hai chỗ có chủ đích và chỉ ở lát này: tệp CSV phục vụ dataset cấp mới,
lượt chèn dưới đây phục vụ dataset đã có. Hai đường vẫn gieo **một** dữ liệu vì
`WHERE NOT EXISTS` khóa theo `(package_id, document_type, operation_code)` —
đúng khóa của `uq_auto_posting_rules_package_operation` — nên dataset cấp mới
sau khi CSV đổi sẽ đi qua đây mà không chèn gì.
"""


_INSERT_OPERATION = """
INSERT INTO auto_posting_rules (
    package_id, document_type, operation_code, operation_name,
    debit_purpose, credit_purpose, requires_partner, partner_kind, display_order
)
SELECT p.id, :document_type, :code, :name,
       :debit, :credit, true, :partner_kind, :display_order
  FROM config_packages AS p
 WHERE NOT EXISTS (
           SELECT 1
             FROM auto_posting_rules AS r
            WHERE r.package_id = p.id
              AND r.document_type = :document_type
              AND r.operation_code = :code
       )
"""
"""Tên bảng viết thẳng, không nội suy: `test_no_sql_string_interpolation` cấm
mọi `text()` nhận chuỗi dựng động, và cấm đúng — đó là đường một biến của người
dùng đi lọt vào SQL. Migration là ảnh chụp lịch sử nên tên bảng ở đây cố định
theo định nghĩa, chứ không phải một hằng số nào đó về sau đổi được."""


_DELETE_OPERATIONS = """
DELETE FROM auto_posting_rules
 WHERE document_type = 'SAL'
   AND operation_code IN ('dieu-chinh-tang-hoa-don', 'dieu-chinh-giam-hoa-don')
"""
"""Đường hạ cấp của `_INSERT_OPERATION`. Literal chứ không bindparam — xem
`downgrade`; và hai mã ở đây phải khớp `_NEW_OPERATIONS`, thứ mà
`test_the_adjustment_operations_agree_between_the_packages_and_the_migration`
canh cùng lượt với tệp CSV của gói."""


def upgrade() -> None:
    _widen_sales_kinds()
    _add_adjustment_link()
    _add_adjustment_operations()


def _add_adjustment_link() -> None:
    """`adjusts_voucher_id` — đường trỏ về chứng từ được điều chỉnh (FR-EIV-033/036).

    **Ràng buộc mới là phần đáng nói, không phải cột.** `adjustment_link_matches_kind`
    buộc đường này có mặt **đúng** trên hai `kind` điều chỉnh và vắng mặt ở năm
    loại còn lại. Đó là thứ làm cho `adjusts_voucher_id IS NULL` mang nghĩa
    "không phải chứng từ điều chỉnh" thay vì "chưa ai điền" — và phân hệ hóa đơn
    điện tử **tin vào nghĩa ấy** để phân biệt một chứng từ chênh lệch với một
    hóa đơn bán thường (C3 cấm nó đọc `kind` của phân hệ khác). Một luật chỉ
    viết ở service thì đường ghi thứ hai nào đó sẽ làm phép phân biệt ấy nói dối.

    Cột `NULL` được nên dữ liệu cũ đi qua không cần vá: mọi hóa đơn đã có đều
    mang `kind` ngoài hai loại mới, tức đúng vế "phải rỗng" của ràng buộc.

    `RESTRICT` chứ không `CASCADE`: chứng từ gốc phải sống chừng nào còn một
    chứng từ điều chỉnh nói về nó — cùng lập luận `fk_einvoices_source_voucher`
    của `0032`.
    """
    op.add_column(_SALES_TABLE, sa.Column("adjusts_voucher_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_sales_invoices_adjusts_voucher_id",
        _SALES_TABLE,
        _SALES_TABLE,
        ["adjusts_voucher_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "adjustment_link_matches_kind",
        _SALES_TABLE,
        f"(adjusts_voucher_id IS NOT NULL) = (kind IN ({_ADJUSTMENT_INCREASE}, {_KIND_MAX}))",
    )
    op.create_check_constraint("does_not_adjust_itself", _SALES_TABLE, "adjusts_voucher_id <> id")


def _widen_sales_kinds() -> None:
    """`kind` nhận thêm hai loại điều chỉnh — xem docstring đầu tệp.

    Bỏ rồi tạo lại vì Postgres không sửa được biểu thức của một `CHECK` tại chỗ
    (cùng lối `0036._widen_notice_kinds`).
    """
    op.drop_constraint("kind_known", _SALES_TABLE, type_="check")
    op.create_check_constraint(
        "kind_known", _SALES_TABLE, f"kind BETWEEN {_KIND_MIN} AND {_KIND_MAX}"
    )


def _add_adjustment_operations() -> None:
    """Chèn hai nghiệp vụ vào **mọi** gói cấu hình đã gieo ở dataset này.

    `INSERT … SELECT … WHERE NOT EXISTS` chứ không `bulk_insert`: `package_id`
    chỉ biết được lúc chạy, và một dataset có thể mang nhiều gói (TT99 và TT133
    cùng tồn tại, mỗi năm tài chính chọn một). Câu lệnh vẫn **tĩnh**, nên nó
    diễn đạt được thành SQL của `upgrade --sql`.
    """
    for code, name, debit, credit, order in _NEW_OPERATIONS:
        op.execute(
            sa.text(_INSERT_OPERATION).bindparams(
                document_type=_SALES_DOCUMENT_TYPE,
                code=code,
                name=name,
                debit=debit,
                credit=credit,
                partner_kind=_CUSTOMER,
                display_order=order,
            )
        )


def downgrade() -> None:
    """Gỡ hai nghiệp vụ rồi thắt `kind` về `0..4`.

    Cả hai bước nằm trong **một** transaction (DDL của PostgreSQL có
    transaction), nên thứ tự ở đây không đổi kết quả: dataset đang có một chứng
    từ điều chỉnh thì lượt thắt `CHECK` hỏng và phần `DELETE` cuốn theo — đúng
    như nó nên hỏng, vì hạ cấp mà bỏ lại chứng từ mang `kind` vừa bị cấm là để
    lại một bảng tự mâu thuẫn.

    Hai mã viết thẳng trong câu lệnh, không qua bindparam: `env.py` sinh SQL với
    `literal_binds=True` cho `downgrade --sql`, và một bindparam **danh sách**
    mang `NullType` nên không có bộ dựng literal nào — câu lệnh đổ
    `CompileError` đúng lúc người vận hành cần bản SQL để rà trước khi chạy. Mọi
    tiền lệ `DELETE` ở đường hạ cấp (`0010`, `0031`) cũng viết literal.
    """
    op.execute(sa.text(_DELETE_OPERATIONS))
    op.drop_constraint("does_not_adjust_itself", _SALES_TABLE, type_="check")
    op.drop_constraint("adjustment_link_matches_kind", _SALES_TABLE, type_="check")
    op.drop_constraint("fk_sales_invoices_adjusts_voucher_id", _SALES_TABLE, type_="foreignkey")
    op.drop_column(_SALES_TABLE, "adjusts_voucher_id")
    op.drop_constraint("kind_known", _SALES_TABLE, type_="check")
    op.create_check_constraint("kind_known", _SALES_TABLE, f"kind BETWEEN {_KIND_MIN} AND 4")
