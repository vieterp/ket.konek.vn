"""Hồ sơ nhà cung cấp hóa đơn điện tử + nới hai ràng buộc số hóa đơn — lát 7E-2.

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-08

Chạy **một lần cho mỗi schema dataset** như `0001`..`0033`, bằng `ket_owner`.

**Vì sao nới hai ràng buộc của `0032`.** Nhà cung cấp hóa đơn điện tử **cấp số
hóa đơn**, không phải phần mềm: số nằm trong câu trả lời của `issueInvoices`,
tức chỉ tới nơi sau khi tờ hóa đơn đã rời phần mềm và tới cơ quan thuế (quyết
định user 2026-09-08). Hai ràng buộc của `0032` viết dưới giả định phần mềm là
nơi duy nhất cấp số, nên chúng cấm đúng trạng thái mà luồng ấy phải đi qua.

* `only_draft_has_no_number` → `issued_invoice_has_a_number`: ngưỡng dời từ
  "chỉ bản nháp mới được thiếu số" sang "từ `DA_PHAT_HANH` trở lên thì phải có
  số". `DANG_PHAT_HANH` và `PHAT_HANH_LOI` nay là hai trạng thái hợp lệ mà chưa
  có số — "đang gửi, chưa nghe trả lời" và "gửi rồi, bị từ chối". Không bỏ hẳn
  ràng buộc: một tờ hóa đơn cơ quan thuế đã nhận mà phần mềm không biết số của
  nó là dữ liệu vô dụng ở đúng chỗ nó quan trọng nhất.
* `number_and_date_together` → `number_needs_a_date`: song điều kiện thành một
  chiều. Ngày hóa đơn là thứ **ta** chọn và gửi đi trong chính bản XML
  (`ArisingDate`), nên nó có trước số; khoảng giữa ấy là một trạng thái thật.

**Dãy số gap-free cục bộ không mất vai trò**, chỉ hẹp lại đúng chỗ nó là sự
thật: hóa đơn đặt in và tự in (`invoice_forms.kind` 1 và 2) không có bên thứ ba
nào cấp số, và `0032` đã cấm chúng mang `provider_code`
(`provider_only_for_electronic`). Nhà cung cấp `internal` cũng vậy. Điều kiện ở
`service.issue` vì thế đọc đúng một trường: ký hiệu có khai nhà cung cấp không.

Trigger `einvoices_immutable_after_issue` **giữ nguyên, không dựng lại**: nhánh
số của nó canh `OLD.invoice_no IS NOT NULL`, nên điền từ `NULL` vẫn qua còn đổi
một số đã cấp vẫn bị chặn — đúng hai chiều cần thiết cho luồng mới. Revision này
cũng **không thêm cột nào** vào `einvoices`, nên nhánh `status >= 3` của trigger
vẫn phủ đủ bộ cột như `0032` khai.

**Bảng mới `einvoice_provider_profiles`** giữ thông tin đăng nhập, một dòng mỗi
nhà cung cấp. Không có cột chi nhánh: tài khoản là của doanh nghiệp, mà một dữ
liệu kế toán là một doanh nghiệp — nên bảng này **không** vào danh sách RLS, và
cổng `test_rls_policy_coverage` chỉ đòi policy cho bảng *có* cột chi nhánh.
`password_enc` là `bytea` vì nó đi qua `SecretBox` (Fernet), cùng cơ chế và cùng
lập luận kiểu cột với bí mật TOTP.

Không đổi metadata builtin nào nên chuỗi không cần bước làm mới (doctrine 0025).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import set_search_path_statement

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_CODE_MAX_LENGTH = 50
_BASE_URL_MAX_LENGTH = 200
_USERNAME_MAX_LENGTH = 100
_TAX_CODE_MAX_LENGTH = 20

_PROFILE_TABLE = "einvoice_provider_profiles"
_EINVOICE_TABLE = "einvoices"

# 3 = `EInvoiceStatus.DA_PHAT_HANH`, 0 = `CHUA_PHAT_HANH` (số trần có chú thích,
# cùng lối `0022` và `0032`).
_ISSUED_FLOOR = 3


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
    _relax_number_constraints()
    _create_provider_profiles()
    _apply_security()


def _relax_number_constraints() -> None:
    """Xem docstring đầu tệp về vì sao hai ràng buộc này đổi."""
    op.drop_constraint("only_draft_has_no_number", _EINVOICE_TABLE, type_="check")
    op.create_check_constraint(
        "issued_invoice_has_a_number",
        _EINVOICE_TABLE,
        f"status < {_ISSUED_FLOOR} OR invoice_no IS NOT NULL",
    )
    op.drop_constraint("number_and_date_together", _EINVOICE_TABLE, type_="check")
    op.create_check_constraint(
        "number_needs_a_date",
        _EINVOICE_TABLE,
        "invoice_no IS NULL OR invoice_date IS NOT NULL",
    )


def _create_provider_profiles() -> None:
    op.create_table(
        _PROFILE_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider_code", sa.String(length=_PROVIDER_CODE_MAX_LENGTH), nullable=False),
        sa.Column("base_url", sa.String(length=_BASE_URL_MAX_LENGTH), nullable=False),
        sa.Column("username", sa.String(length=_USERNAME_MAX_LENGTH), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=False),
        sa.Column("tax_code", sa.String(length=_TAX_CODE_MAX_LENGTH), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_code", name="uq_einvoice_provider_profiles_code"),
        sa.CheckConstraint("base_url <> ''", name="base_url_not_blank"),
        sa.CheckConstraint("username <> ''", name="username_not_blank"),
        sa.CheckConstraint("tax_code <> ''", name="tax_code_not_blank"),
    )


def _apply_security() -> None:
    op.execute(set_search_path_statement(_target_schema()))
    grantee = _dataset_grantee()
    for statement in grant_read_write(
        _PROFILE_TABLE, grantee=grantee, sequence=serial_sequence_name(_PROFILE_TABLE)
    ):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table(_PROFILE_TABLE)
    op.drop_constraint("number_needs_a_date", _EINVOICE_TABLE, type_="check")
    op.create_check_constraint(
        "number_and_date_together",
        _EINVOICE_TABLE,
        "(invoice_no IS NULL) = (invoice_date IS NULL)",
    )
    op.drop_constraint("issued_invoice_has_a_number", _EINVOICE_TABLE, type_="check")
    op.create_check_constraint(
        "only_draft_has_no_number",
        _EINVOICE_TABLE,
        "status = 0 OR invoice_no IS NOT NULL",
    )
