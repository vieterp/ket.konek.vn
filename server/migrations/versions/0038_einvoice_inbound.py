"""Sổ hóa đơn điện tử đầu vào — lát 7F-2b.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-12

Chạy **một lần cho mỗi schema dataset** như `0001`..`0037`, bằng `ket_owner`.

**Hai bảng, và bảng thứ nhất tồn tại vì một khóa.** Đường rẻ hơn cho FR-EIV-040
là phân giải tệp XML rồi trả ngay một thân chứng từ mua điền sẵn, không bảng
nào cả. Nó hỏng ở chỗ không sửa được về sau: không có dòng nào mang
`(MST người bán, mẫu số, ký hiệu, số)` thì cùng một tờ hóa đơn — thường đến hai
lần, bản gửi qua thư và bản tải từ cổng tra cứu — đi thẳng thành hai chứng từ
mua, tức **khấu trừ thuế GTGT đầu vào hai lần**. `uq_inbound_einvoices_identity`
là toàn bộ lý do bảng này là một bảng.

**Khóa ấy KHÔNG có `branch_id`**, có chủ đích: một tờ hóa đơn không được vào sổ
hai lần dù hai lượt nạp đứng ở hai chi nhánh khác nhau, vì sổ thì chỉ có một.
Hệ quả phải tính tới ở đường ghi: RLS che dòng của chi nhánh khác, nên phép tra
trước lượt ghi sẽ trượt và **ràng buộc này** mới là thứ chặn thật (xem
`inbound.record`).

**Ba cột của khóa vì thế `NOT NULL`.** PostgreSQL coi hai `NULL` là khác nhau,
nên một cột `NULL` được nằm trong khóa duy nhất là một khóa không chặn gì —
tờ hóa đơn thiếu mẫu số sẽ nạp lại được vô hạn lần. Đó là lý do bộ phân giải
đòi `KHMSHDon` và `KHHDon` chứ không cho chúng rỗng.

**Và khóa so trên dạng CHUẨN HÓA, không trên chuỗi thô** — phần quyết định xem
nó có chặn thật hay không. Hai bản của **cùng một tờ** hay khác nhau đúng ở cách
viết: số hóa đơn `00004994` với `4994` (số hóa đơn là một *số*, phần đệm chỉ để
in), ký hiệu hoa/thường tùy nhà cung cấp, mã số thuế có khoảng trắng. So chuỗi
thô thì cả hai lọt. Chuẩn hóa nằm trong **biểu thức của chỉ mục** chứ không ở
một cột thứ hai: cột thô giữ nguyên chữ người bán viết (thứ phải in ra và đối
chiếu với cơ quan thuế), và không có nguồn sự thật thứ hai nào để lệch.

**`nature` là cột chống một lỗi không số học nào bắt được.** Hóa đơn **điều
chỉnh giảm** của nhà cung cấp tự nó nhất quán từng đồng, nên nó lọt mọi phép
kiểm tổng rồi thành một chứng từ mua làm **tăng** chi phí và thuế đầu vào đúng
phần đáng lẽ phải giảm; hóa đơn **thay thế** mang số mới nên khóa nhận dạng
không thấy nó là lần thứ hai của cùng một khoản mua. Thứ sai nằm ở **dấu của
nghiệp vụ**, và chỉ `TCHDon` nói ra. Tờ hóa đơn vẫn được lưu (nghĩa vụ lưu trữ
không phân biệt loại); thứ bị chặn là lượt lập chứng từ.

**`ON DELETE SET NULL (voucher_id)`** — cú pháp PostgreSQL 15+, và cả ba chữ
đều cần thiết. `RESTRICT` biến mọi chứng từ mua lập từ hóa đơn đầu vào thành
chứng từ không xóa được (`REFERENCE_GUARDS` không chạy ở `VoucherService.delete`,
nên người dùng nhận một lỗi khóa ngoại trần), và gỡ nó đòi `einvoice` móc vào
vòng đời chứng từ của `purchase` — đúng thứ luật C3 cấm. `SET NULL` trơn thì
nhắm vào cả hai cột của khóa ghép, mà `branch_id` `NOT NULL`. Bản giới hạn một
cột trả tờ hóa đơn về đúng trạng thái đúng của nó — *chưa lập chứng từ*, vì
chứng từ không còn nữa — mà không đụng chi nhánh.

**Tệp XML không nằm trong cơ sở dữ liệu.** Nó đi vào kho định địa chỉ theo nội
dung của phase 2, cùng kho với bản thể hiện hóa đơn đầu ra (`0035`), nên
`pg_dump` hằng đêm không phải đọc lại chúng và hai tệp cùng nội dung là một tệp
trên đĩa. Bảng giữ hash + kích thước + tên hiển thị.

**`_refresh_builtin_data` GIỮ Ở `0035`** — doctrine 5B M-1 không kích hoạt ở
revision này: nó không gieo báo cáo hay mẫu in builtin nào. Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở
đây — dự án đã sập bẫy vị trí này ba lần (6A, 6G-1, 7A).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import enable_branch_rls_statements, set_search_path_statement

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INBOUND_TABLE = "inbound_einvoices"
_INBOUND_LINE_TABLE = "inbound_einvoice_lines"

_TAX_CODE_MAX_LENGTH = 20
_PARTY_NAME_MAX_LENGTH = 400
_PARTY_ADDRESS_MAX_LENGTH = 500
_INVOICE_FORM_MAX_LENGTH = 20
_INVOICE_SERIAL_MAX_LENGTH = 20
_INVOICE_NO_MAX_LENGTH = 50
_TAX_AUTHORITY_CODE_MAX_LENGTH = 100
_CURRENCY_CODE_LENGTH = 3
_FILE_NAME_MAX_LENGTH = 255
_SHA256_HEX_LENGTH = 64
_LINE_DESCRIPTION_MAX_LENGTH = 500
_LINE_UNIT_MAX_LENGTH = 50
_VAT_RATE_TEXT_MAX_LENGTH = 50

# Hình dạng số, cùng con số với `kernel/money.py` và `posting/engine/models.py`.
# Số trần chứ không import hằng: migration là ảnh chụp lịch sử (luật của `0022`).
_AMOUNT_PRECISION = 18
_AMOUNT_SCALE = 2
_RATE_PRECISION = 18
_RATE_SCALE = 6
_QUANTITY_PRECISION = 20
_QUANTITY_SCALE = 6
_UNIT_PRICE_PRECISION = 24
_UNIT_PRICE_SCALE = 6
_VAT_RATE_PRECISION = 5
_VAT_RATE_SCALE = 2


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def _dataset_grantee() -> str:
    return role_name_for_schema(_target_schema())


def upgrade() -> None:
    _create_inbound()
    _create_inbound_lines()
    _apply_security()


def _create_inbound() -> None:
    """Sổ hóa đơn đầu vào — xem docstring đầu tệp về khóa nhận dạng."""
    table = _INBOUND_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("seller_tax_code", sa.String(length=_TAX_CODE_MAX_LENGTH), nullable=False),
        sa.Column("seller_name", sa.String(length=_PARTY_NAME_MAX_LENGTH), nullable=False),
        sa.Column("seller_address", sa.String(length=_PARTY_ADDRESS_MAX_LENGTH), nullable=True),
        sa.Column("buyer_tax_code", sa.String(length=_TAX_CODE_MAX_LENGTH), nullable=False),
        sa.Column("buyer_name", sa.String(length=_PARTY_NAME_MAX_LENGTH), nullable=True),
        sa.Column("buyer_address", sa.String(length=_PARTY_ADDRESS_MAX_LENGTH), nullable=True),
        sa.Column("invoice_form", sa.String(length=_INVOICE_FORM_MAX_LENGTH), nullable=False),
        sa.Column("invoice_serial", sa.String(length=_INVOICE_SERIAL_MAX_LENGTH), nullable=False),
        sa.Column("invoice_no", sa.String(length=_INVOICE_NO_MAX_LENGTH), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column(
            "tax_authority_code",
            sa.String(length=_TAX_AUTHORITY_CODE_MAX_LENGTH),
            nullable=True,
        ),
        sa.Column("currency_code", sa.String(length=_CURRENCY_CODE_LENGTH), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(_RATE_PRECISION, _RATE_SCALE), nullable=False),
        sa.Column("total_before_tax", sa.Numeric(_AMOUNT_PRECISION, _AMOUNT_SCALE), nullable=False),
        sa.Column("total_vat", sa.Numeric(_AMOUNT_PRECISION, _AMOUNT_SCALE), nullable=False),
        sa.Column("total_amount", sa.Numeric(_AMOUNT_PRECISION, _AMOUNT_SCALE), nullable=False),
        # `InvoiceNature`: 1 gốc · 2 thay thế · 3 điều chỉnh · 9 liên quan tới tờ
        # khác nhưng không khai kiểu. Số trần có chú thích, cùng lối `0022`,
        # `0032`, `0037` — migration là ảnh chụp lịch sử, không import enum.
        sa.Column("nature", sa.SmallInteger(), nullable=False),
        sa.Column("related_form", sa.String(length=_INVOICE_FORM_MAX_LENGTH), nullable=True),
        sa.Column("related_serial", sa.String(length=_INVOICE_SERIAL_MAX_LENGTH), nullable=True),
        sa.Column("related_no", sa.String(length=_INVOICE_NO_MAX_LENGTH), nullable=True),
        sa.Column("related_date", sa.Date(), nullable=True),
        sa.Column("voucher_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=_SHA256_HEX_LENGTH), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("file_name", sa.String(length=_FILE_NAME_MAX_LENGTH), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.CheckConstraint("seller_tax_code <> ''", name="seller_tax_code_not_blank"),
        sa.CheckConstraint("buyer_tax_code <> ''", name="buyer_tax_code_not_blank"),
        sa.CheckConstraint("seller_name <> ''", name="seller_name_not_blank"),
        sa.CheckConstraint("invoice_form <> ''", name="invoice_form_not_blank"),
        sa.CheckConstraint("invoice_serial <> ''", name="invoice_serial_not_blank"),
        sa.CheckConstraint("invoice_no <> ''", name="invoice_no_not_blank"),
        sa.CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        sa.CheckConstraint(
            "total_before_tax >= 0 AND total_vat >= 0 AND total_amount >= 0",
            name="totals_not_negative",
        ),
        sa.CheckConstraint("byte_size > 0", name="byte_size_positive"),
        sa.CheckConstraint("file_name <> ''", name="file_name_not_blank"),
        sa.CheckConstraint(
            f"char_length(content_hash) = {_SHA256_HEX_LENGTH}", name="content_hash_is_sha256"
        ),
        sa.CheckConstraint("nature IN (1, 2, 3, 9)", name="nature_known"),
        # Hai vế của cùng một sự thật: tờ hóa đơn gốc không có gì để trỏ ngược,
        # và một tờ mang đường trỏ ngược thì không phải tờ gốc — nên `nature = 1`
        # mang nghĩa "không liên quan tới tờ nào" thay vì "chưa ai điền".
        sa.CheckConstraint(
            "nature = 1 OR (related_serial IS NOT NULL AND related_no IS NOT NULL) OR nature = 9",
            name="related_only_when_not_original",
        ),
        sa.CheckConstraint(
            "nature <> 1 OR (related_form IS NULL AND related_serial IS NULL "
            "AND related_no IS NULL AND related_date IS NULL)",
            name="original_has_no_related_invoice",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f(f"fk_{table}_branch_id"),
            ondelete="RESTRICT",
        ),
        # Khóa ghép → `vouchers (id, branch_id)`, dùng lại ràng buộc duy nhất
        # `uq_vouchers_id_branch` mà `0032` đã dựng cho `einvoices`.
        sa.ForeignKeyConstraint(
            ["voucher_id", "branch_id"],
            ["vouchers.id", "vouchers.branch_id"],
            name="fk_inbound_einvoices_voucher",
            ondelete="SET NULL (voucher_id)",
        ),
    )
    # Khóa nhận dạng: chỉ mục duy nhất trên **biểu thức chuẩn hóa**, không phải
    # một `UNIQUE` trên cột thô — xem docstring đầu tệp.
    op.execute(
        "CREATE UNIQUE INDEX uq_inbound_einvoices_identity ON inbound_einvoices ("
        "upper(btrim(seller_tax_code)), "
        "upper(btrim(invoice_form)), "
        "upper(btrim(invoice_serial)), "
        "coalesce(nullif(ltrim(btrim(invoice_no), '0'), ''), '0'))"
    )
    op.create_index(f"ix_{table}_branch_seller", table, ["branch_id", "seller_tax_code"])
    # "Còn tờ nào chưa vào sổ" — câu hỏi làm nên màn hình này. Chỉ mục **bán
    # phần**: những tờ đã lập chứng từ không bao giờ là câu trả lời, nên giữ
    # chúng trong chỉ mục chỉ làm nó lớn dần theo lịch sử mà không ai đọc.
    op.create_index(
        f"ix_{table}_pending",
        table,
        ["branch_id", "invoice_date"],
        postgresql_where=sa.text("voucher_id IS NULL"),
    )


def _create_inbound_lines() -> None:
    """Dòng hàng của tờ hóa đơn đầu vào.

    `vat_amount` là cột **được tính** lúc nạp: tờ hóa đơn TCT cộng thuế theo
    nhóm thuế suất chứ không theo dòng, nên tiền thuế từng dòng là kết quả một
    phép chia ngược. Chia **một lần** lúc nạp chứ không mỗi lần dựng chứng từ —
    hai đường sinh ra cùng một con số sẽ lệch nhau đúng vào lần ai đó sửa luật
    làm tròn ở một đường.

    Không có `branch_id`, nên không có policy RLS: dòng chỉ đọc được qua tờ hóa
    đơn cha, mà tờ ấy thì RLS đã lọc. Cùng hình dạng `purchase_invoice_lines`.
    """
    table = _INBOUND_LINE_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("line_no", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=_LINE_DESCRIPTION_MAX_LENGTH), nullable=False),
        sa.Column("unit", sa.String(length=_LINE_UNIT_MAX_LENGTH), nullable=True),
        sa.Column("quantity", sa.Numeric(_QUANTITY_PRECISION, _QUANTITY_SCALE), nullable=True),
        sa.Column(
            "unit_price", sa.Numeric(_UNIT_PRICE_PRECISION, _UNIT_PRICE_SCALE), nullable=True
        ),
        sa.Column("amount", sa.Numeric(_AMOUNT_PRECISION, _AMOUNT_SCALE), nullable=False),
        sa.Column("vat_rate", sa.Numeric(_VAT_RATE_PRECISION, _VAT_RATE_SCALE), nullable=True),
        sa.Column("vat_rate_text", sa.String(length=_VAT_RATE_TEXT_MAX_LENGTH), nullable=True),
        sa.Column("vat_amount", sa.Numeric(_AMOUNT_PRECISION, _AMOUNT_SCALE), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
        sa.UniqueConstraint("invoice_id", "line_no", name="uq_inbound_einvoice_lines_no"),
        sa.CheckConstraint("line_no > 0", name="line_no_positive"),
        sa.CheckConstraint("description <> ''", name="description_not_blank"),
        sa.CheckConstraint("amount >= 0 AND vat_amount >= 0", name="amounts_not_negative"),
        sa.CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        sa.CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="unit_price_not_negative"),
        sa.CheckConstraint("vat_rate IS NULL OR vat_rate >= 0", name="vat_rate_not_negative"),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            [f"{_INBOUND_TABLE}.id"],
            name=op.f(f"fk_{table}_invoice_id"),
            ondelete="CASCADE",
        ),
    )


def _apply_security() -> None:
    op.execute(set_search_path_statement(_target_schema()))
    grantee = _dataset_grantee()
    for statement in grant_read_write(_INBOUND_TABLE, grantee=grantee, sequence=None):
        op.execute(statement)
    for statement in grant_read_write(
        _INBOUND_LINE_TABLE,
        grantee=grantee,
        sequence=serial_sequence_name(_INBOUND_LINE_TABLE),
    ):
        op.execute(statement)
    # Chỉ bảng cha bật RLS — bảng dòng không có `branch_id` để canh theo, và
    # nó chỉ đọc được qua bảng cha. Cùng hình dạng `purchase_invoice_lines`.
    for statement in enable_branch_rls_statements(_INBOUND_TABLE):
        op.execute(statement)


def downgrade() -> None:
    """Đường lui. Một migration không có nó là một `head` không quay lại được —
    và cổng `test_dataset_migration_downgrade` cùng hai bài nâng cấp dataset cũ
    (chúng dựng trạng thái xuất phát bằng cách hạ cấp) bắt đúng điều đó."""
    # Tên bảng viết thẳng, không nội suy: `test_no_sql_string_interpolation` cấm
    # mọi `op.execute` nhận f-string, và cấm đúng — migration là ảnh chụp lịch
    # sử nên tên bảng ở đây cố định theo định nghĩa, không theo một hằng số về
    # sau đổi được. Cùng lối `0032.downgrade`.
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON inbound_einvoices")
    op.execute("ALTER TABLE inbound_einvoices DISABLE ROW LEVEL SECURITY")
    op.drop_table(_INBOUND_LINE_TABLE)
    op.drop_table(_INBOUND_TABLE)
