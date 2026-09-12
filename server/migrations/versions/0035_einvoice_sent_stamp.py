"""Dấu "đã gửi" + bảng bản thể hiện + mẫu in bản thể hiện — lát 7E-3.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-09

Chạy **một lần cho mỗi schema dataset** như `0001`..`0034`, bằng `ket_owner`.

**Ba cột, một lời khai.** Lượt gửi bản thể hiện xảy ra **ngoài phần mềm** —
quyết định user 2026-09-08: kế toán gửi từ hộp thư của mình, hoặc chính nhà cung
cấp gửi theo cấu hình bên họ (bản tích hợp EasyInvoice đang chạy thật bỏ hẳn
`CusEmails` khỏi XML vì trường ấy làm họ trả `Code=127`, và tự lo phần thư từ).
Ba cột ở đây vì thế ghi một *lời khai về việc đã xảy ra*, không phải kết quả một
thao tác của máy; `sent_at` do người dùng khai nên nó nhận ngày lùi.

**Ràng buộc `sent_after_issue` một chiều, không phải song điều kiện.** Chiều
"chưa phát hành thì chưa gửi được" luôn đúng. Chiều ngược lại thì không:
`DA_THAY_THE`/`DA_DIEU_CHINH`/`DA_HUY` tới được thẳng từ `DA_PHAT_HANH` mà chưa
từng gửi ai, nên đòi "trạng thái cao ⇒ có dấu gửi" sẽ đỏ trên dữ liệu đúng —
đúng hình dạng lỗi mà `0034` phải sửa cho `number_and_date_together`.

**Trigger `einvoices_immutable_after_issue` KHÔNG dựng lại, và đó là một quyết
định chứ không phải một lượt bỏ sót.** Nhánh `status >= 3` của nó là **danh sách
cấm** — nó liệt kê đích danh chín cột nội dung không được đổi — chứ không phải
danh sách cho phép. Ba cột mới không nằm trong danh sách ấy, nên chúng ghi được
ở `DA_PHAT_HANH`, mà đó chính là điều kiện sống còn của cạnh `DA_PHAT_HANH →
DA_GUI`: dấu gửi luôn được đóng **sau** khi hóa đơn đã phát hành. Bài
`test_marking_sent_survives_the_immutability_trigger` ghim đúng điều này, vì
một lượt siết trigger thành danh sách cho phép ở lát sau sẽ làm cạnh `SEND` chết
im lặng.

**`_refresh_builtin_data` ĐÃ DỜI SANG `0039`** — doctrine 5B M-1: bước làm mới
metadata builtin luôn đậu ở **head** của chuỗi, nên mỗi lát gieo dữ liệu builtin
mới lại nhận nó. Revision này gieo mẫu in `HDDT` (bản thể hiện hóa đơn); mẫu ấy
vẫn tới mọi dataset vì `ensure_builtin_print_templates` idempotent theo từng dòng
`(document_type, code)` và `0039` gọi lại đúng hàm đó. Đừng thêm lượt gọi thứ hai
vào đây: hai chỗ gọi cùng một bước làm mới là hai chỗ để chúng lệch nhau khi lát
sau lại dời. Kiểm vị trí bằng
`grep -rn "_refresh_builtin_data" migrations/versions/`.

**Bảng `einvoice_representations`: vì sao KHÔNG dùng `attachments` chung.**
Bản đầu của lát này cất bản thể hiện vào bảng đính kèm dùng chung rồi nhận dạng
nó bằng `(entity_type='einvoices', media_type)`. Cửa `POST /api/v1/attachments`
cho phép đính **bất cứ tệp nào** vào **bất cứ `entity_id` nào** mà không hỏi bản
ghi chủ thuộc loại gì, nên một tệp PDF tùy ý đính vào id hóa đơn sẽ chiếm chỗ
vĩnh viễn: đường tải thấy "đã có" nên không bao giờ đi lấy bản thật về nữa — tức
đúng nghĩa vụ lưu trữ mười năm bị vô hiệu. Bảng riêng đóng bốn thứ cùng lúc:
không cửa HTTP nào ghi được vào nó; `UNIQUE (einvoice_id, kind)` **dựng thật**
bất biến "mỗi tờ một tệp mỗi loại"; nội dung hóa đơn không còn đọc được qua cửa
đính kèm chung bằng mã quyền của phân hệ khác; và `kind` là cột riêng nên phép
nhận dạng thôi phụ thuộc `media_type` (bảng đính kèm chống trùng theo
`content_hash` **không kể** kiểu nội dung — hai luật trên một khóa là chỗ một
lượt tải hợp lệ kẹt 409).

Tệp vẫn nằm ở **kho content-hash của phase 2**, dùng chung với đính kèm: cùng
nội dung thì cùng một tệp trên đĩa. `einvoices` vẫn không có cột `BYTEA` nào.

**`GRANT` cho ba cột mới thì không cần** — `GRANT` của PostgreSQL là theo bảng,
không theo cột, nên chúng đã nằm trong quyền `einvoices` cấp từ `0032` (tiền lệ
`0022`). Bảng mới thì cần đủ quyền + RLS như mọi bảng có `branch_id`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import enable_branch_rls_statements, set_search_path_statement

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EINVOICE_TABLE = "einvoices"
_REPRESENTATION_TABLE = "einvoice_representations"
_SENT_TO_MAX_LENGTH = 500
_MEDIA_TYPE_MAX_LENGTH = 150
_FILE_NAME_MAX_LENGTH = 255
_SHA256_HEX_LENGTH = 64

# 3 = `EInvoiceStatus.DA_PHAT_HANH` (số trần có chú thích, cùng lối `0022`,
# `0032` và `0034` — migration là ảnh chụp lịch sử, không import enum).
_ISSUED_FLOOR = 3


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    _add_sent_columns()
    _create_representations()
    _apply_security()


def _add_sent_columns() -> None:
    """Ba cột của dấu gửi, cộng ba ràng buộc — xem docstring đầu tệp."""
    op.add_column(_EINVOICE_TABLE, sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        _EINVOICE_TABLE, sa.Column("sent_to", sa.String(length=_SENT_TO_MAX_LENGTH), nullable=True)
    )
    op.add_column(_EINVOICE_TABLE, sa.Column("sent_by", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "sent_after_issue",
        _EINVOICE_TABLE,
        f"sent_at IS NULL OR status >= {_ISSUED_FLOOR}",
    )
    # Cùng lập luận `submitted_stamp_complete` của thông báo hủy (`0032`): "đã
    # gửi nhưng không biết gửi cho ai" là nửa dữ liệu chỉ lộ ra đúng lúc cần
    # giải trình.
    op.create_check_constraint(
        "sent_stamp_complete",
        _EINVOICE_TABLE,
        "(sent_at IS NULL) = (sent_to IS NULL) AND (sent_at IS NULL) = (sent_by IS NULL)",
    )
    op.create_check_constraint("sent_to_not_blank", _EINVOICE_TABLE, "sent_to <> ''")


def _dataset_grantee() -> str:
    return role_name_for_schema(_target_schema())


def _create_representations() -> None:
    """Bảng bản thể hiện đã lưu trữ — xem docstring đầu tệp về vì sao nó riêng."""
    table = _REPRESENTATION_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("einvoice_id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        # `pdf` / `xml` — giá trị của `RepresentationKind`; chuỗi trần vì
        # migration là ảnh chụp lịch sử, không import enum (luật của `0022`).
        sa.Column("kind", sa.String(length=3), nullable=False),
        sa.Column("content_hash", sa.String(length=_SHA256_HEX_LENGTH), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=_MEDIA_TYPE_MAX_LENGTH), nullable=False),
        sa.Column("file_name", sa.String(length=_FILE_NAME_MAX_LENGTH), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # Bất biến trung tâm: một tờ, một tệp mỗi loại. Toàn phần chứ không bán
        # phần — không trạng thái nào của hóa đơn làm bản lưu trữ cũ hết giá trị.
        sa.UniqueConstraint("einvoice_id", "kind", name="uq_einvoice_representations_kind"),
        sa.CheckConstraint("kind IN ('pdf', 'xml')", name="kind_known"),
        sa.CheckConstraint("byte_size > 0", name="byte_size_positive"),
        sa.CheckConstraint("file_name <> ''", name="file_name_not_blank"),
        sa.CheckConstraint(
            f"char_length(content_hash) = {_SHA256_HEX_LENGTH}", name="content_hash_is_sha256"
        ),
        # `CASCADE` ngược với `einvoices.source_voucher_id`: bản thể hiện là dẫn
        # xuất của tờ hóa đơn, không phải lời khai đứng riêng. Mà hóa đơn đã cấp
        # số thì `einvoices_numbered_rows_are_permanent` đã cấm xóa, nên đường
        # duy nhất chạm tới đây là xóa một bản nháp — thứ chưa từng có tệp nào.
        sa.ForeignKeyConstraint(
            ["einvoice_id", "branch_id"],
            [f"{_EINVOICE_TABLE}.id", f"{_EINVOICE_TABLE}.branch_id"],
            name="fk_einvoice_representations_einvoice",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name="fk_einvoice_representations_branch",
            ondelete="RESTRICT",
        ),
    )


def _apply_security() -> None:
    op.execute(set_search_path_statement(_target_schema()))
    grantee = _dataset_grantee()
    for statement in grant_read_write(
        _REPRESENTATION_TABLE,
        grantee=grantee,
        sequence=serial_sequence_name(_REPRESENTATION_TABLE),
    ):
        op.execute(statement)
    for statement in enable_branch_rls_statements(_REPRESENTATION_TABLE):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(_REPRESENTATION_TABLE)
    op.drop_constraint("sent_to_not_blank", _EINVOICE_TABLE, type_="check")
    op.drop_constraint("sent_stamp_complete", _EINVOICE_TABLE, type_="check")
    op.drop_constraint("sent_after_issue", _EINVOICE_TABLE, type_="check")
    op.drop_column(_EINVOICE_TABLE, "sent_by")
    op.drop_column(_EINVOICE_TABLE, "sent_to")
    op.drop_column(_EINVOICE_TABLE, "sent_at")
