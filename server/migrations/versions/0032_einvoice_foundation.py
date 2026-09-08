"""Nền hóa đơn điện tử: danh mục ký hiệu, hóa đơn, văn bản hủy, hồ sơ đăng ký.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-07

Chạy **một lần cho mỗi schema dataset** như `0001`..`0031`, bằng `ket_owner`.

**Bồi cột vào danh mục đã có, không thêm danh mục.** `invoice_forms` đăng ký từ
phase 1 với đúng bộ cột chung, và docstring của chính nó viết sẵn rằng mẫu số,
ký hiệu và dải số "thuộc phase 7". Lát này trả món nợ ấy: `form_no`, `kind`,
`provider_code`. `code` của danh mục mang **ký hiệu** hóa đơn — thứ duy nhất
trong doanh nghiệp và in trên tờ hóa đơn — nên hai chỉ mục duy nhất sẵn có của
`master_data_table_args` canh đúng thứ cần canh, không phải thêm ràng buộc nào.

**Ba bảng mới:**

* `einvoices` — hóa đơn, có `branch_id` + RLS. Khóa ngoại **ghép**
  `(source_voucher_id, branch_id)` → `vouchers (id, branch_id)` giữ hai vế
  không lệch được; nó đòi `vouchers` có một `UNIQUE (id, branch_id)`, thêm ở
  đây (rẻ: `id` đã là khóa chính nên cặp ấy vốn duy nhất, chỉ là chưa ai khai
  ra để PostgreSQL cho tham chiếu).
* `einvoice_error_notices` — thông báo hủy + biên bản hủy, `UNIQUE (einvoice_id,
  kind)`. Trần `kind` là `0..1`; 7F nới lên khi thêm thông báo sai sót, cùng
  khuôn nới trần `target_kind` của `0030`/`0031`.
* `invoice_registrations` — hồ sơ đăng ký / thông báo phát hành, `branch_id` +
  RLS.

**Một trigger** `einvoices_immutable_after_issue` — BR-EIV-01 và nửa BR-EIV-02:

* nội dung hóa đơn đứng yên khi `status >= 3`; đường duy nhất còn mở là các cột
  trạng thái (`status`, ba cột `tax_authority_*`, `lookup_code`), vì chính chúng
  chở tin từ cơ quan thuế về sau khi phát hành;
* **số hóa đơn không đổi kể từ lúc có giá trị**, ở mọi trạng thái. Đây mới là
  chỗ dãy gap-free thủng: phát hành lỗi rồi phát hành lại phải dùng lại số cũ
  (ADR-013), và một đường ghi cấp số lần hai để lại một số không thuộc hóa đơn
  nào. `service.issue` đã canh; trigger là lớp chót cho đường ghi nào lách qua.

**Hai hàm chạy ngoài RLS** — lần thứ **năm** của mẫu "phép kiểm chạy dưới phạm
vi nào" (6C H-1, 3B-2 B-8, 6G-2 H-3, 7B `partner_open_debt`), cùng lập luận an
toàn: `SECURITY DEFINER`, đầu vào là giá trị vô hướng, không nhận điều kiện lọc
tự do, không ghi, `SET search_path FROM CURRENT` sau khi revision **tự đặt**
search_path.

* `invoice_form_branch_owner(p_form_id, p_branch_id)` — **một ký hiệu thuộc
  đúng một chi nhánh**. Dãy số phân theo (mẫu số, ký hiệu) đúng chữ BR-EIV-02,
  còn dải số đã thông báo phân theo chi nhánh (FR-INV-008); hai trục khác nhau
  cho cùng một con số nghĩa là chi nhánh B phát hành ra số thuộc dải của chi
  nhánh A. Buộc một ký hiệu về một chi nhánh làm hai trục ấy trùng nhau. Chủ cũ
  của ký hiệu nằm ở chi nhánh người gọi không nhìn thấy — đúng lý do hàm này
  phải đứng ngoài RLS.
* `invoice_range_conflicts(p_form_id, p_range_from, p_range_to, p_exclude_id)`
  — chồng lấn dải trong cùng một ký hiệu. Sau luật trên nó chỉ còn so trong một
  chi nhánh, nhưng giữ `SECURITY DEFINER` làm lớp thứ hai: nới luật kia ở lát
  sau mà phép kiểm này lặng lẽ mù đi là hai tờ hóa đơn cùng số.

Hồ sơ **giữ chỗ** là hồ sơ đã nộp hoặc đang hiệu lực: bản nháp chưa xin gì, bản
đã ngừng đã trả lại.

Không đổi metadata builtin nào nên chuỗi không cần bước làm mới (doctrine 0025).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write
from ket.kernel.security.rls import enable_branch_rls_statements, set_search_path_statement

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FORM_NO_MAX_LENGTH = 20
_PROVIDER_CODE_MAX_LENGTH = 50
_INVOICE_NO_MAX_LENGTH = 50
_TAX_AUTHORITY_CODE_MAX_LENGTH = 100
_LOOKUP_CODE_MAX_LENGTH = 100
_NOTICE_NO_MAX_LENGTH = 50
_REASON_CODE_MAX_LENGTH = 50

_EINVOICE_TABLE = "einvoices"
_NOTICE_TABLE = "einvoice_error_notices"
_REGISTRATION_TABLE = "invoice_registrations"

_RLS_TABLES = (_EINVOICE_TABLE, _REGISTRATION_TABLE)
_ALL_TABLES = (_EINVOICE_TABLE, _NOTICE_TABLE, _REGISTRATION_TABLE)

_VOUCHER_BRANCH_UNIQUE = "uq_vouchers_id_branch"


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
    _extend_invoice_forms()
    _add_voucher_branch_unique()
    _create_einvoices()
    _create_error_notices()
    _create_registrations()
    _create_immutability_trigger()
    _create_range_conflict_function()
    _apply_security()


def _extend_invoice_forms() -> None:
    table = "invoice_forms"
    op.add_column(table, sa.Column("form_no", sa.String(length=_FORM_NO_MAX_LENGTH), nullable=True))
    # `InvoiceFormKind`: 0 điện tử, 1 đặt in, 2 tự in.
    op.add_column(table, sa.Column("kind", sa.SmallInteger(), nullable=True))
    op.add_column(
        table,
        sa.Column("provider_code", sa.String(length=_PROVIDER_CODE_MAX_LENGTH), nullable=True),
    )
    op.create_check_constraint("kind_is_known", table, "kind IS NULL OR kind IN (0, 1, 2)")
    op.create_check_constraint("form_no_not_blank", table, "form_no IS NULL OR form_no <> ''")
    _refuse_incomplete_invoice_forms()
    op.create_check_constraint(
        "form_set_unless_group",
        table,
        "is_group OR (form_no IS NOT NULL AND kind IS NOT NULL)",
    )
    op.create_check_constraint(
        "group_has_no_invoice_fields",
        table,
        "NOT is_group OR (form_no IS NULL AND kind IS NULL AND provider_code IS NULL)",
    )
    op.create_check_constraint(
        "provider_only_for_electronic", table, "provider_code IS NULL OR kind = 0"
    )


def _refuse_incomplete_invoice_forms() -> None:
    """Dừng chuỗi nếu danh mục đã có ký hiệu cũ chưa khai mẫu số và hình thức.

    `form_set_unless_group` ngay dưới là ràng buộc **kiểm ngay lúc thêm**, nên
    một dòng ký hiệu người dùng đã tạo qua API danh mục chung trước lát này (mã
    và tên, không có gì khác) sẽ làm cả migration đổ với một thông điệp của
    PostgreSQL không nói được dòng nào.

    Không backfill, vì **không có giá trị nào suy ra được**: mẫu số và hình thức
    hóa đơn là thứ doanh nghiệp đã đăng ký với cơ quan thuế, và đoán `kind = 0`
    cho một ký hiệu hóa đơn đặt in là ghi một lời khai sai vào chính chỗ
    BR-EIV-03 sẽ đọc. Không nới ràng buộc thành `NOT VALID`, vì một ràng buộc
    bỏ qua dữ liệu cũ thì đúng những dòng đáng ngờ nhất là những dòng nó không
    canh. Đường còn lại là **dừng và nói rõ**: nêu đích danh các mã phải điền,
    rollback sạch (Alembic chạy trong transaction), người vận hành điền xong
    chạy lại.

    Viết bằng `DO` tĩnh chứ không `SELECT` ở Python để chuỗi vẫn sinh được bằng
    `upgrade --sql` (cùng lý do `0026` ghi cho bước sửa dữ liệu của nó).
    """
    op.execute(
        "DO $$"
        " DECLARE offending text;"
        " BEGIN"
        "   SELECT string_agg(code, ', ' ORDER BY code) INTO offending"
        "   FROM invoice_forms"
        "   WHERE NOT is_group AND (form_no IS NULL OR kind IS NULL);"
        "   IF offending IS NOT NULL THEN"
        "     RAISE EXCEPTION 'Ky hieu hoa don chua khai mau so va hinh thuc: %."
        " Dien hai truong nay cho tung ky hieu roi chay lai migration.', offending"
        "       USING ERRCODE = 'raise_exception';"
        "   END IF;"
        " END $$"
    )


def _add_voucher_branch_unique() -> None:
    """Đích cho khóa ngoại ghép của `einvoices` — xem docstring đầu tệp.

    Không tốn thêm bảo đảm nào: `id` đã là khóa chính nên cặp `(id, branch_id)`
    vốn duy nhất; ràng buộc chỉ nói ra điều đó để PostgreSQL chấp nhận một
    `REFERENCES vouchers (id, branch_id)`.
    """
    op.create_unique_constraint(_VOUCHER_BRANCH_UNIQUE, "vouchers", ["id", "branch_id"])


def _create_einvoices() -> None:
    table = _EINVOICE_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("source_voucher_id", sa.Uuid(), nullable=False),
        sa.Column("invoice_form_id", sa.Integer(), nullable=False),
        sa.Column("invoice_no", sa.String(length=_INVOICE_NO_MAX_LENGTH), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        # `EInvoiceStatus`: 0 chưa PH, 1 đang PH, 2 PH lỗi, 3 đã PH, 4 đã gửi,
        # 5 đã thay thế, 6 đã điều chỉnh, 7 đã hủy.
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("tax_authority_status", sa.SmallInteger(), nullable=True),
        sa.Column(
            "tax_authority_code",
            sa.String(length=_TAX_AUTHORITY_CODE_MAX_LENGTH),
            nullable=True,
        ),
        sa.Column("tax_authority_message", sa.Text(), nullable=True),
        sa.Column("lookup_code", sa.String(length=_LOOKUP_CODE_MAX_LENGTH), nullable=True),
        sa.Column("replaces_invoice_id", sa.Uuid(), nullable=True),
        sa.Column("adjusts_invoice_id", sa.Uuid(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status BETWEEN 0 AND 7", name=op.f(f"ck_{table}_status_known")),
        sa.CheckConstraint(
            "tax_authority_status IS NULL OR tax_authority_status BETWEEN 0 AND 3",
            name=op.f(f"ck_{table}_tax_authority_status_known"),
        ),
        sa.CheckConstraint(
            "(invoice_no IS NULL) = (invoice_date IS NULL)",
            name=op.f(f"ck_{table}_number_and_date_together"),
        ),
        sa.CheckConstraint(
            "status = 0 OR invoice_no IS NOT NULL",
            name=op.f(f"ck_{table}_only_draft_has_no_number"),
        ),
        sa.CheckConstraint(
            "replaces_invoice_id <> id", name=op.f(f"ck_{table}_does_not_replace_itself")
        ),
        sa.CheckConstraint(
            "adjusts_invoice_id <> id", name=op.f(f"ck_{table}_does_not_adjust_itself")
        ),
        sa.UniqueConstraint("invoice_form_id", "invoice_no", name="uq_einvoices_form_number"),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f(f"fk_{table}_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_voucher_id", "branch_id"],
            ["vouchers.id", "vouchers.branch_id"],
            name="fk_einvoices_source_voucher",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_form_id"],
            ["invoice_forms.id"],
            name=op.f(f"fk_{table}_invoice_form_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["replaces_invoice_id"],
            [f"{table}.id"],
            name=op.f(f"fk_{table}_replaces_invoice_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["adjusts_invoice_id"],
            [f"{table}.id"],
            name=op.f(f"fk_{table}_adjusts_invoice_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_branch_status", table, ["branch_id", "status"])
    # **Một chứng từ, một hóa đơn còn hiệu lực** (review 7D H-1). Không có ràng
    # buộc này thì cùng một chứng từ bán phát hành được N tờ hóa đơn, mỗi tờ
    # tiêu một số của dãy — và vì hóa đơn cố ý không mang số tiền (nó đọc từ
    # chứng từ gốc), N tờ ấy đều "khớp chứng từ gốc" từng đồng. Lập luận
    # "BR-EIV-07 đúng theo cấu trúc" đóng chiều LỆCH; nó không đóng chiều NHÂN
    # ĐÔI, và chiều thứ hai mới là chiều làm doanh thu khai gấp N lần.
    #
    # Riêng phần theo ba trạng thái cuối: một hóa đơn thay thế (7F) trỏ về cùng
    # chứng từ trong khi tờ cũ chuyển sang `DA_THAY_THE` — đó là hình dạng
    # ĐÚNG, và một ràng buộc toàn phần sẽ chặn đúng đường sửa sai sót ấy.
    op.create_index(
        "uq_einvoices_live_source_voucher",
        table,
        ["source_voucher_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN (5, 6, 7)"),
    )
    # Guard FR-EIV-035 chạy ở MỌI lượt sửa, xóa và bỏ ghi sổ chứng từ của mọi
    # phân hệ — kể cả bản cài không dùng hóa đơn điện tử. Không có chỉ mục này
    # thì mỗi lượt ấy quét toàn bảng hóa đơn.
    op.create_index(f"ix_{table}_source_voucher", table, ["source_voucher_id"])


def _create_error_notices() -> None:
    table = _NOTICE_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("einvoice_id", sa.Uuid(), nullable=False),
        # `ErrorNoticeKind`: 0 thông báo hủy (CQT), 1 biên bản hủy (người mua).
        # Trần nới ở 7F khi thêm thông báo sai sót (FR-EIV-030/033/034).
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("notice_no", sa.String(length=_NOTICE_NO_MAX_LENGTH), nullable=False),
        sa.Column("notice_date", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.String(length=_REASON_CODE_MAX_LENGTH), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        # `NoticeStatus`: 0 nháp, 1 đã nộp.
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind BETWEEN 0 AND 1", name=op.f(f"ck_{table}_kind_known")),
        sa.CheckConstraint("status BETWEEN 0 AND 1", name=op.f(f"ck_{table}_status_known")),
        sa.CheckConstraint("notice_no <> ''", name=op.f(f"ck_{table}_notice_no_not_blank")),
        sa.CheckConstraint(
            "(status = 1) = (submitted_at IS NOT NULL)",
            name=op.f(f"ck_{table}_submitted_stamp_complete"),
        ),
        sa.UniqueConstraint("einvoice_id", "kind", name="uq_einvoice_error_notices_kind"),
        sa.ForeignKeyConstraint(
            ["einvoice_id"],
            [f"{_EINVOICE_TABLE}.id"],
            name=op.f(f"fk_{table}_einvoice_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )


def _create_registrations() -> None:
    table = _REGISTRATION_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=False),
        sa.Column("invoice_form_id", sa.Integer(), nullable=False),
        sa.Column("notice_no", sa.String(length=_NOTICE_NO_MAX_LENGTH), nullable=True),
        sa.Column("notice_date", sa.Date(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("range_from", sa.BigInteger(), nullable=True),
        sa.Column("range_to", sa.BigInteger(), nullable=True),
        # `RegistrationStatus`: 0 nháp, 1 đã nộp, 2 hiệu lực, 3 ngừng.
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("status BETWEEN 0 AND 3", name=op.f(f"ck_{table}_status_known")),
        sa.CheckConstraint(
            "notice_no IS NULL OR notice_no <> ''", name=op.f(f"ck_{table}_notice_no_not_blank")
        ),
        sa.CheckConstraint(
            "(range_from IS NULL) = (range_to IS NULL)",
            name=op.f(f"ck_{table}_range_bounds_together"),
        ),
        sa.CheckConstraint(
            "range_from IS NULL OR range_to >= range_from",
            name=op.f(f"ck_{table}_range_is_ordered"),
        ),
        sa.CheckConstraint(
            "range_from IS NULL OR range_from >= 1", name=op.f(f"ck_{table}_range_starts_at_one")
        ),
        sa.ForeignKeyConstraint(
            ["branch_id"],
            ["branches.id"],
            name=op.f(f"fk_{table}_branch_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_form_id"],
            ["invoice_forms.id"],
            name=op.f(f"fk_{table}_invoice_form_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}")),
    )
    op.create_index(f"ix_{table}_form", table, ["invoice_form_id", "branch_id"])


def _create_immutability_trigger() -> None:
    """BR-EIV-01 + nửa BR-EIV-02 — xem docstring đầu tệp.

    Câu lệnh viết **thẳng, không ghép chuỗi**: `test_no_sql_string_interpolation`
    cấm mọi `op.execute` nhận chuỗi dựng động, kể cả một danh sách cột `join`
    lại — và cấm đúng. Cái giá là danh sách cột phải gõ ra ở đây; cái được là
    thân trigger trong tệp đúng bằng thân trigger trong cơ sở dữ liệu, đọc một
    chỗ là biết nó canh gì.

    **Ba tầng, ba câu hỏi khác nhau** (review 7D M-2 nới hai tầng đầu):

    1. `invoice_no` và `invoice_form_id` bất biến **kể từ lúc số CÓ giá trị**,
       ở mọi trạng thái. Không chờ ngưỡng đã phát hành, vì số đã tiêu từ
       `DANG_PHAT_HANH` — bản đầu chỉ đóng băng `invoice_form_id` ở `>= 3`, tức
       một hóa đơn đang phát hành hoặc phát hành lỗi vẫn đổi được ký hiệu và
       mang theo con số của dãy CŨ sang dãy KHÁC.
    2. Mọi cột nội dung bất biến khi `status >= 3`. Cột duy nhất còn mở là năm
       cột chở tin từ cơ quan thuế về (`status`, ba cột `tax_authority_*`,
       `lookup_code`) — chúng chỉ có giá trị sau khi hóa đơn đã đi.
    3. **`DELETE` bị chặn khi hóa đơn đã tiêu số.** Máy trạng thái đã cấm, nhưng
       máy trạng thái sống ở Python; một `DELETE` thẳng bằng SQL bỏ tờ hóa đơn
       đi và để lại một dòng `allocated_numbers` trỏ vào hư không — dãy thủng ở
       đúng chỗ khó phát hiện nhất.

    Một cột thêm ở lát sau (7E `sent_at`, 7F chuỗi sai sót) **không** tự động
    được canh, và đó là chỗ nguy hiểm. `test_einvoice_flow` đóng lại: nó duyệt
    cột thật của bảng theo ORM và đòi mỗi cột ngoài năm cột trên phải bị trigger
    từ chối — thêm cột mà quên dựng lại trigger thì CI đỏ.
    """
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "CREATE OR REPLACE FUNCTION einvoices_refuse_content_change()"
        " RETURNS trigger"
        " LANGUAGE plpgsql"
        " AS $$"
        " BEGIN"
        "   IF OLD.invoice_no IS NOT NULL AND ("
        "        NEW.invoice_no IS DISTINCT FROM OLD.invoice_no"
        "     OR NEW.invoice_form_id IS DISTINCT FROM OLD.invoice_form_id"
        "   ) THEN"
        "     RAISE EXCEPTION 'So hoa don da cap khong doi duoc (BR-EIV-02)'"
        "       USING ERRCODE = 'raise_exception';"
        "   END IF;"
        # 3 = `EInvoiceStatus.DA_PHAT_HANH` (số trần có chú thích, 0022).
        "   IF OLD.status >= 3 AND ("
        "        NEW.id IS DISTINCT FROM OLD.id"
        "     OR NEW.branch_id IS DISTINCT FROM OLD.branch_id"
        "     OR NEW.source_voucher_id IS DISTINCT FROM OLD.source_voucher_id"
        "     OR NEW.invoice_form_id IS DISTINCT FROM OLD.invoice_form_id"
        "     OR NEW.invoice_no IS DISTINCT FROM OLD.invoice_no"
        "     OR NEW.invoice_date IS DISTINCT FROM OLD.invoice_date"
        "     OR NEW.replaces_invoice_id IS DISTINCT FROM OLD.replaces_invoice_id"
        "     OR NEW.adjusts_invoice_id IS DISTINCT FROM OLD.adjusts_invoice_id"
        "     OR NEW.issued_at IS DISTINCT FROM OLD.issued_at"
        "   ) THEN"
        "     RAISE EXCEPTION 'Hoa don da phat hanh la bat bien (BR-EIV-01)'"
        "       USING ERRCODE = 'raise_exception';"
        "   END IF;"
        "   RETURN NEW;"
        " END;"
        " $$"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION einvoices_refuse_numbered_delete()"
        " RETURNS trigger"
        " LANGUAGE plpgsql"
        " AS $$"
        " BEGIN"
        "   IF OLD.invoice_no IS NOT NULL THEN"
        "     RAISE EXCEPTION 'Hoa don da cap so khong xoa duoc (BR-EIV-02)'"
        "       USING ERRCODE = 'raise_exception';"
        "   END IF;"
        "   RETURN OLD;"
        " END;"
        " $$"
    )
    op.execute(
        "CREATE TRIGGER einvoices_immutable_after_issue"
        " BEFORE UPDATE ON einvoices"
        " FOR EACH ROW EXECUTE FUNCTION einvoices_refuse_content_change()"
    )
    op.execute(
        "CREATE TRIGGER einvoices_numbered_rows_are_permanent"
        " BEFORE DELETE ON einvoices"
        " FOR EACH ROW EXECUTE FUNCTION einvoices_refuse_numbered_delete()"
    )


def _create_range_conflict_function() -> None:
    """FR-INV-008 — xem docstring đầu tệp; khuôn và lý do từng mệnh đề ở `0026`."""
    op.execute(set_search_path_statement(_target_schema()))
    op.execute(
        "CREATE OR REPLACE FUNCTION invoice_range_conflicts("
        "p_form_id integer, p_range_from bigint, p_range_to bigint, p_exclude_id uuid)"
        " RETURNS TABLE(notice_no text, range_from bigint, range_to bigint)"
        " LANGUAGE sql"
        " STABLE"
        " SECURITY DEFINER"
        " SET search_path FROM CURRENT"
        " AS $$"
        "     SELECT r.notice_no::text, r.range_from, r.range_to"
        "     FROM invoice_registrations r"
        "     WHERE r.invoice_form_id = p_form_id"
        # 1 đã nộp, 2 hiệu lực — hồ sơ đã ra khỏi tay người lập thì dải của nó
        # đã đứng tên ai đó. Nháp (0) chưa xin gì, ngừng (3) đã trả lại.
        "       AND r.status IN (1, 2)"
        "       AND r.range_from IS NOT NULL"
        "       AND r.range_from <= p_range_to"
        "       AND r.range_to >= p_range_from"
        "       AND (p_exclude_id IS NULL OR r.id <> p_exclude_id)"
        " $$"
    )
    op.execute(
        "CREATE OR REPLACE FUNCTION invoice_form_branch_owner("
        "p_form_id integer, p_branch_id integer)"
        " RETURNS TABLE(branch_id integer)"
        " LANGUAGE sql"
        " STABLE"
        " SECURITY DEFINER"
        " SET search_path FROM CURRENT"
        " AS $$"
        "     SELECT DISTINCT r.branch_id"
        "     FROM invoice_registrations r"
        "     WHERE r.invoice_form_id = p_form_id"
        "       AND r.status IN (1, 2)"
        "       AND r.branch_id <> p_branch_id"
        " $$"
    )


def _apply_security() -> None:
    grantee = _dataset_grantee()
    for table in _ALL_TABLES:
        for statement in grant_read_write(table, grantee=grantee, sequence=None):
            op.execute(statement)
    for table in _RLS_TABLES:
        for statement in enable_branch_rls_statements(table):
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS invoice_range_conflicts(integer, bigint, bigint, uuid)")
    op.execute("DROP FUNCTION IF EXISTS invoice_form_branch_owner(integer, integer)")
    op.execute("DROP TRIGGER IF EXISTS einvoices_immutable_after_issue ON einvoices")
    op.execute("DROP TRIGGER IF EXISTS einvoices_numbered_rows_are_permanent ON einvoices")
    op.execute("DROP FUNCTION IF EXISTS einvoices_refuse_content_change()")
    op.execute("DROP FUNCTION IF EXISTS einvoices_refuse_numbered_delete()")
    # Dòng `number_sequences` mang khóa `EIV|…` **sống sót** qua lượt hạ cấp:
    # chúng thuộc bảng của phase 2, và xóa chúng ở đây là xóa bằng chứng duy
    # nhất còn lại về những số đã cấp ra đời thật. Lượt nâng cấp lại vì thế
    # tiếp tục từ đúng chỗ dãy dừng, không cấp lại số cũ — đó là hành vi đúng,
    # và `deployment-guide` §hạ cấp ghi nó ra để người vận hành không ngạc nhiên.
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON einvoices")
    op.execute("ALTER TABLE einvoices DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS p_branch_scope ON invoice_registrations")
    op.execute("ALTER TABLE invoice_registrations DISABLE ROW LEVEL SECURITY")
    op.drop_table(_REGISTRATION_TABLE)
    op.drop_table(_NOTICE_TABLE)
    op.drop_table(_EINVOICE_TABLE)
    op.drop_constraint(_VOUCHER_BRANCH_UNIQUE, "vouchers")
    table = "invoice_forms"
    for constraint in (
        "ck_invoice_forms_provider_only_for_electronic",
        "ck_invoice_forms_group_has_no_invoice_fields",
        "ck_invoice_forms_form_set_unless_group",
        "ck_invoice_forms_form_no_not_blank",
        "ck_invoice_forms_kind_is_known",
    ):
        # Tên ĐẦY ĐỦ và không truyền `type_` — lý do ở `0030`, cùng chỗ đã mất
        # một vòng chạy ở 7C-3.
        op.drop_constraint(constraint, table)
    op.drop_column(table, "provider_code")
    op.drop_column(table, "kind")
    op.drop_column(table, "form_no")
