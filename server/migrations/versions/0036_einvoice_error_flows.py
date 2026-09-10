"""Bảng quyết định xử lý sai sót + thông báo sai sót — lát 7F-1.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-10

Chạy **một lần cho mỗi schema dataset** như `0001`..`0035`, bằng `ket_owner`.

**Bảng quyết định là bảng, không phải tệp JSON.** Phác thảo phase-07 nói cả hai
(§Luồng hỏi–đáp nói `einvoice_error_flows`, bước 14 nói `error_flows.json`);
quyết định user 2026-09-10 chọn bảng. Lý do là FR-NFR-055: quy định về xử lý hóa
đơn sai sót đổi thì kế toán sửa dữ liệu, không chờ một bản phát hành mới. Một tệp
JSON nằm trong cây mã thì mọi lượt sửa đều là một lượt phát hành.

**Không bọc gói cấu hình** (khác `auto_posting_rules` của `0015`): gói cấu hình
tồn tại vì TT99 và TT133 gán **tài khoản khác nhau** cho cùng một nghiệp vụ. Cách
xử lý hóa đơn sai sót đến từ NĐ123/TT78 và áp như nhau cho cả hai chế độ kế toán,
nên phần phạm vi theo gói ở đây là máy móc không ai dùng — và máy móc không ai
dùng vẫn phải được bảo trì.

**`UNIQUE NULLS NOT DISTINCT` là chỗ dựa của phép tra.** `buyer_declared IS NULL`
nghĩa là "câu hỏi thứ hai không áp dụng ở nhánh này". `UNIQUE` mặc định của
PostgreSQL coi hai `NULL` là khác nhau, nên hai dòng `(KHONG_PHAT_SINH, NULL)`
nói ngược nhau vẫn lọt vào bảng và phép tra trả về dòng nào là tùy planner —
đúng loại lỗi không bao giờ đỏ trong CI mà sai trên máy khách. `NULLS NOT
DISTINCT` có từ PostgreSQL 15; bản cài đích là 16 (quyết định D4).

**Không có `branch_id`, và đó là quyết định.** Quy định của nhà nước không thuộc
về chi nhánh nào. Cùng hình dạng `item_discount_tiers`/`price_list_lines` của
`0027`: không cột thì cổng `test_rls_policy_coverage` không đòi policy, và cũng
**không** phải khai miễn trừ (cổng ấy quét theo cột `branch_id`, không quét
danh mục). Khác hẳn `opening_balance_invoices` mà `7A` phải đi vá: bảng ấy chứa
dữ liệu **của một chi nhánh** mà thiếu cột, còn bảng này thì không.

**Nới `einvoice_error_notices.kind` lên `0..2`** cho `THONG_BAO_SAI_SOT` (Mẫu
04/SS-HĐĐT). `0032` đã chừa chỗ cho lát này trong docstring của
`CANCELLATION_KINDS`, và loại thứ ba **không** vào bộ ấy: BR-EIV-04 vẫn đòi đúng
thông báo hủy + biên bản hủy, còn thông báo sai sót kèm **mọi** cách xử lý.
Bỏ rồi tạo lại vì Postgres không sửa được biểu thức của một `CHECK` tại chỗ.

**`_refresh_builtin_data` GIỮ Ở `0035`** — doctrine 5B M-1 không kích hoạt ở
revision này: nó không gieo báo cáo hay mẫu in builtin nào, chỉ gieo dữ liệu của
chính bảng mới (mà mọi dataset đều đi qua `upgrade` này nên đều thấy). Kiểm bằng
`grep -rn "_refresh_builtin_data" migrations/versions/` chứ đừng tin con số ở
đây — dự án đã sập bẫy vị trí này ba lần (6A, 6G-1, 7A).

**Năm dòng gieo, bốn cách xử lý.** Hai câu hỏi cho ba loại sai sót: hai loại đầu
hỏi tiếp "khách đã kê khai chưa" (2 × 2 = 4 dòng), loại thứ ba không hỏi
(1 dòng). Bốn *cách xử lý* phân biệt là thứ tiêu chí nghiệm thu đếm, không phải
số dòng.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ket.kernel.datasets.naming import role_name_for_schema
from ket.kernel.datasets.provisioning import ALEMBIC_SCHEMA_ATTRIBUTE
from ket.kernel.security.grants import grant_read_write, serial_sequence_name
from ket.kernel.security.rls import set_search_path_statement

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ERROR_FLOW_TABLE = "einvoice_error_flows"
_ERROR_NOTICE_TABLE = "einvoice_error_notices"

_FLOW_CODE_MAX_LENGTH = 50
_LEGAL_BASIS_MAX_LENGTH = 200

# Số trần có chú thích, cùng lối `0022`, `0032`, `0034` và `0035` — migration là
# ảnh chụp lịch sử, không import enum.
#
# `ErrorKind`:  0 = SAI_THONG_TIN · 1 = SAI_SO_TIEN · 2 = KHONG_PHAT_SINH
# `Remedy`:     0 = THAY_THE · 1 = DIEU_CHINH_THONG_TIN · 2 = DIEU_CHINH_TIEN · 3 = HUY
# `ErrorNoticeKind`: 0 = THONG_BAO_HUY · 1 = BIEN_BAN_HUY · 2 = THONG_BAO_SAI_SOT
_ERROR_KIND_MIN = 0
_ERROR_KIND_MAX = 2
_REMEDY_MIN = 0
_REMEDY_MAX = 3
_NOTICE_KIND_MIN = 0
_NOTICE_KIND_MAX = 2

_TT78 = "TT 78/2021/TT-BTC điều 7; NĐ 123/2020/NĐ-CP điều 19"
_ND123 = "NĐ 123/2020/NĐ-CP điều 19 khoản 1"

_SEED_FLOWS: tuple[tuple[str, int, bool | None, int, str], ...] = (
    ("WRONG_INFO_UNDECLARED", 0, False, 0, _TT78),
    ("WRONG_INFO_DECLARED", 0, True, 1, _TT78),
    ("WRONG_AMOUNT_UNDECLARED", 1, False, 0, _TT78),
    ("WRONG_AMOUNT_DECLARED", 1, True, 2, _TT78),
    ("NO_TRANSACTION", 2, None, 3, _ND123),
)


def _target_schema() -> str:
    schema = context.config.attributes.get(ALEMBIC_SCHEMA_ATTRIBUTE)
    if not isinstance(schema, str):
        raise RuntimeError(
            f"Không xác định được schema đích: `{ALEMBIC_SCHEMA_ATTRIBUTE}` chưa được "
            "`migrations/env.py` ghi vào Config.attributes"
        )
    return schema


def upgrade() -> None:
    _create_error_flows()
    _widen_notice_kinds()
    _apply_grants()
    _seed_error_flows()


def _create_error_flows() -> None:
    """Bảng quyết định — xem docstring đầu tệp về `NULLS NOT DISTINCT`."""
    table = _ERROR_FLOW_TABLE
    op.create_table(
        table,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=_FLOW_CODE_MAX_LENGTH), nullable=False),
        sa.Column("error_kind", sa.SmallInteger(), nullable=False),
        sa.Column("buyer_declared", sa.Boolean(), nullable=True),
        sa.Column("remedy", sa.SmallInteger(), nullable=False),
        sa.Column("legal_basis", sa.String(length=_LEGAL_BASIS_MAX_LENGTH), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("code <> ''", name="code_not_blank"),
        sa.CheckConstraint(
            f"error_kind BETWEEN {_ERROR_KIND_MIN} AND {_ERROR_KIND_MAX}",
            name="error_kind_known",
        ),
        sa.CheckConstraint(f"remedy BETWEEN {_REMEDY_MIN} AND {_REMEDY_MAX}", name="remedy_known"),
        sa.UniqueConstraint("code", name="uq_einvoice_error_flows_code"),
        sa.UniqueConstraint(
            "error_kind",
            "buyer_declared",
            name="uq_einvoice_error_flows_answers",
            postgresql_nulls_not_distinct=True,
        ),
    )


def _widen_notice_kinds() -> None:
    """`kind` nhận thêm `THONG_BAO_SAI_SOT` — xem docstring đầu tệp."""
    op.drop_constraint("kind_known", _ERROR_NOTICE_TABLE, type_="check")
    op.create_check_constraint(
        "kind_known",
        _ERROR_NOTICE_TABLE,
        f"kind BETWEEN {_NOTICE_KIND_MIN} AND {_NOTICE_KIND_MAX}",
    )


def _apply_grants() -> None:
    """Quyền cho vai trò runtime, kèm sequence của khóa `SERIAL`.

    `grant_read_write` chứ không `grant_read_only` dù đây là dữ liệu quy định:
    FR-NFR-055 đòi sửa được **lúc chạy** khi quy định đổi, và cửa sửa ấy đi qua
    vai trò runtime như mọi danh mục khác. Không có RLS vì bảng không mang
    `branch_id` — xem docstring đầu tệp.
    """
    op.execute(set_search_path_statement(_target_schema()))
    for statement in grant_read_write(
        _ERROR_FLOW_TABLE,
        grantee=role_name_for_schema(_target_schema()),
        sequence=serial_sequence_name(_ERROR_FLOW_TABLE),
    ):
        op.execute(statement)


def _seed_error_flows() -> None:
    """Năm nhánh của `docs/srs/07` §4.4.

    `INSERT` tĩnh, nên nó diễn đạt được thành SQL của `upgrade --sql` và không
    cần cửa `is_offline_mode` như `_refresh_builtin_data`.
    """
    table = sa.table(
        _ERROR_FLOW_TABLE,
        sa.column("code", sa.String),
        sa.column("error_kind", sa.SmallInteger),
        sa.column("buyer_declared", sa.Boolean),
        sa.column("remedy", sa.SmallInteger),
        sa.column("legal_basis", sa.String),
    )
    op.bulk_insert(
        table,
        [
            {
                "code": code,
                "error_kind": error_kind,
                "buyer_declared": buyer_declared,
                "remedy": remedy,
                "legal_basis": legal_basis,
            }
            for code, error_kind, buyer_declared, remedy, legal_basis in _SEED_FLOWS
        ],
    )


def downgrade() -> None:
    op.drop_constraint("kind_known", _ERROR_NOTICE_TABLE, type_="check")
    op.create_check_constraint(
        "kind_known",
        _ERROR_NOTICE_TABLE,
        f"kind BETWEEN {_NOTICE_KIND_MIN} AND 1",
    )
    op.drop_table(_ERROR_FLOW_TABLE)
