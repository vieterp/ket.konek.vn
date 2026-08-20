"""Gieo gói cấu hình dựng sẵn (TT99, TT133) lúc cấp dữ liệu kế toán mới.

Cùng khuôn với `kernel/dimensions/seed.py`: nhận `Connection` (không `Session`)
vì nó chạy trong cùng mạch `owner_engine.begin()` với `ensure_admin_role` và
`ensure_builtin_dimensions` lúc `provision_dataset` — một dữ liệu kế toán có
vai trò, có chiều "Mã thống kê" nhưng thiếu hệ thống tài khoản là trạng thái mà
không lệnh nào tự sửa được. Core `insert` vì lý do y hệt: `ConfigPackage`/
`ChartOfAccount` là bảng `Audited`, mà lúc cấp dữ liệu kế toán chưa có người
thực hiện nào để khai `AuditContext` — ghi qua ORM `Session` sẽ đâm vào
`AuditContextMissingError` đúng như thiết kế fail-closed của listener.

**Idempotent theo `code` gói**, không theo nội dung: gói `code` đã tồn tại thì
bỏ qua toàn bộ (không sửa, không thêm dòng) — kể cả khi `version` trong
`package.json` đã tăng. Nâng cấp nội dung một gói **đã kích hoạt** là một thao
tác có kiểm soát riêng (gói mới = `code` mới, xem `activator.py`), không phải
việc của đường gieo mầm chạy mỗi lần cấp dữ liệu kế toán.

**Gói dựng sẵn kích hoạt ngay lúc gieo** (`activated_at = now()`) — khác gói
nhập qua `.zip` (`importer.py`), vốn nằm im (`activated_at = NULL`) cho tới khi
có người bấm "kích hoạt". Một dữ liệu kế toán mới cấp phải **dùng được ngay**
(`resolve_package` chỉ đọc gói đã kích hoạt — sửa sau review C1), và không có
ai để bấm nút kích hoạt vào đúng lúc `provision_dataset` đang chạy. `activated_by`
để `NULL`: cột cho phép (`nullable=True`), và đây là gieo mầm hệ thống, không
phải một quyết định của một người dùng cụ thể — cùng lý do `dimensions/seed.py`
không gán "người tạo" cho chiều dựng sẵn.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy import Connection, insert, select, update

from ket.kernel.config.accounts_models import ChartOfAccount, ClosingAccountPair, DefaultAccount
from ket.kernel.config.accounts_models import ConfigPackage as ConfigPackageModel
from ket.kernel.config.packages.loader import LoadedPackage, load_builtin_package
from ket.kernel.config.statements.models import StatementLayout, StatementRow
from ket.kernel.master_data.tree_path import ROOT_LEVEL, child_path, level_of
from ket.kernel.persistence.seeding import bind_seed_schema

logger = structlog.get_logger(__name__)

BUILTIN_PACKAGE_SLUGS: tuple[str, ...] = ("tt99", "tt133")
"""Thư mục con của `data/` được gieo tự động lúc cấp dữ liệu kế toán.

Danh sách đóng, khai tường minh: một gói dựng sẵn thứ ba (TT200 cho sổ cũ, theo
đúng cơ chế bổ sung FR-NFR-055 mà top phase-05 ghi) là quyết định sản phẩm
riêng, không phải một thư mục cứ thêm vào `data/` là tự động chạy."""

_PLACEHOLDER_PATH = "0."
"""Giá trị tạm cho `chart_of_accounts.path` giữa lúc `INSERT` và `UPDATE` —
xem docstring `_insert_accounts`."""


def _insert_package(connection: Connection, loaded: LoadedPackage) -> int:
    manifest = loaded.manifest
    package_id = connection.execute(
        insert(ConfigPackageModel)
        .values(
            code=manifest.code,
            name=manifest.name,
            description=manifest.description,
            scheme=manifest.scheme,
            legal_reference=manifest.legal_reference,
            effective_from=manifest.effective_from,
            effective_to=manifest.effective_to,
            version=manifest.version,
            is_builtin=True,
            # Gói dựng sẵn kích hoạt ngay — xem docstring đầu tệp.
            activated_at=datetime.now(UTC),
            activated_by=None,
        )
        # `returning` thay vì `lastrowid`: đích psycopg + PostgreSQL, và cột
        # khóa chính là `SERIAL`, không phải thứ driver trả lại qua đường
        # `cursor.lastrowid` (chỉ MySQL/SQLite có).
        .returning(ConfigPackageModel.id)
    ).scalar_one()
    return package_id


def _insert_accounts(
    connection: Connection, package_id: int, loaded: LoadedPackage
) -> dict[str, int]:
    """Ghi từng TK theo đúng thứ tự tệp (cha trước con) — path/level suy từ `id`
    vừa được DB cấp, cùng luật `move_subtree_sql` dùng cho mọi danh mục dạng cây.

    `path` chưa biết trước khi `INSERT` (nó phụ thuộc `id` mà DB cấp), nên ghi
    xuống một giá trị tạm hợp lệ với `CHECK ck_chart_of_accounts_path_is_dotted_ids`
    rồi `UPDATE` lại ngay bằng đúng `id` vừa có — hai câu lệnh, không phải một
    khoảng hở quan sát được: cả hai chạy trong cùng transaction gieo mầm
    (`bind_seed_schema` giữ khóa cố vấn tới hết transaction).
    """
    id_by_code: dict[str, int] = {}
    path_by_code: dict[str, str] = {}
    for row in loaded.accounts:
        parent_id = id_by_code.get(row.parent_code) if row.parent_code else None
        parent_path = path_by_code.get(row.parent_code) if row.parent_code else None
        account_id = connection.execute(
            insert(ChartOfAccount)
            .values(
                package_id=package_id,
                code=row.code,
                name=row.name,
                name_en=row.name_en,
                parent_id=parent_id,
                path=_PLACEHOLDER_PATH,
                level=ROOT_LEVEL,
                balance_nature=row.balance_nature,
                is_summary=row.is_summary,
                is_foreign_currency=row.is_foreign_currency,
                detail_tracking=list(row.detail_tracking) or None,
                is_locked=row.is_locked,
            )
            .returning(ChartOfAccount.id)
        ).scalar_one()
        path = child_path(parent_path, account_id)
        connection.execute(
            update(ChartOfAccount)
            .where(ChartOfAccount.id == account_id)
            .values(path=path, level=level_of(path))
        )
        id_by_code[row.code] = account_id
        path_by_code[row.code] = path
    return id_by_code


def _insert_default_accounts(
    connection: Connection, package_id: int, loaded: LoadedPackage
) -> None:
    if not loaded.default_accounts:
        return
    connection.execute(
        insert(DefaultAccount),
        [
            {
                "package_id": package_id,
                "document_type": row.document_type,
                "purpose": row.purpose,
                "account_code": row.account_code,
            }
            for row in loaded.default_accounts
        ],
    )


def _insert_closing_pairs(connection: Connection, package_id: int, loaded: LoadedPackage) -> None:
    if not loaded.closing_pairs:
        return
    connection.execute(
        insert(ClosingAccountPair),
        [
            {
                "package_id": package_id,
                "source_account": row.source_account,
                "target_account": row.target_account,
                "sequence": row.sequence,
                "description": row.description,
            }
            for row in loaded.closing_pairs
        ],
    )


def _insert_statements(connection: Connection, package_id: int, loaded: LoadedPackage) -> None:
    for layout in loaded.statements:
        layout_id = connection.execute(
            insert(StatementLayout)
            .values(
                package_id=package_id,
                code=layout.code,
                name=layout.name,
                name_en=layout.name_en,
                statement_kind=layout.statement_kind,
            )
            .returning(StatementLayout.id)
        ).scalar_one()
        connection.execute(
            insert(StatementRow),
            [
                {
                    "layout_id": layout_id,
                    "row_code": row.row_code,
                    "label": row.label,
                    "label_en": row.label_en,
                    "note_ref": row.note_ref,
                    "formula": row.formula,
                    "indent_level": row.indent_level,
                    "display_order": row.display_order,
                    "is_bold": row.is_bold,
                    "hide_when_zero": row.hide_when_zero,
                    "note": row.note,
                }
                for row in layout.rows
            ],
        )


def _ensure_statements_backfilled(
    connection: Connection, package_id: int, loaded: LoadedPackage
) -> bool:
    """Gieo layout BCTC cho gói builtin **đã tồn tại từ trước lát 5B** nhưng
    chưa có layout nào. Không mâu thuẫn với luật "gói `code` đã tồn tại thì bỏ
    qua toàn bộ": luật đó chặn **ghi đè** nội dung một gói có thể đã kích hoạt;
    ở đây chỉ **thêm vào chỗ trống** — một dataset cấp giữa migration 0010 và
    0011 có gói TT99 nhưng bảng `statement_layouts` rỗng, và không thao tác
    người dùng nào tự lấp được chỗ trống đó. Gói đã có ≥1 layout thì không đụng
    (nâng cấp layout thật sự = gói mới, đúng doctrine)."""
    has_layouts = connection.scalar(
        select(StatementLayout.id).where(StatementLayout.package_id == package_id).limit(1)
    )
    if has_layouts is not None or not loaded.statements:
        return False
    _insert_statements(connection, package_id, loaded)
    logger.info(
        "config_package.seed_statements_backfilled",
        package_code=loaded.manifest.code,
        layouts=len(loaded.statements),
    )
    return True


def _seed_one(connection: Connection, slug: str) -> bool:
    """Gieo một gói dựng sẵn. Trả `True` nếu vừa thêm mới."""
    loaded = load_builtin_package(slug)
    manifest = loaded.manifest

    existing = connection.execute(
        select(ConfigPackageModel.id, ConfigPackageModel.version).where(
            ConfigPackageModel.code == manifest.code
        )
    ).first()
    if existing is not None:
        existing_id, existing_version = existing
        if existing_version != manifest.version:
            # Chỉ ghi log — xem docstring đầu tệp về lý do đường gieo mầm không
            # tự nâng cấp nội dung một gói `code` đã tồn tại. KHÔNG backfill
            # layout khi version lệch (review 5B, M-1): công thức trong
            # `statements.json` được loader kiểm dải TK theo accounts.csv
            # CÙNG version trên đĩa — gắn chúng vào hệ TK version cũ trong DB
            # là đi vòng qua chính lượt kiểm đó.
            logger.warning(
                "config_package.seed_version_mismatch",
                package_code=manifest.code,
                seeded_version=existing_version,
                data_version=manifest.version,
            )
            return False
        _ensure_statements_backfilled(connection, existing_id, loaded)
        return False

    package_id = _insert_package(connection, loaded)
    _insert_accounts(connection, package_id, loaded)
    _insert_default_accounts(connection, package_id, loaded)
    _insert_closing_pairs(connection, package_id, loaded)
    _insert_statements(connection, package_id, loaded)
    return True


def ensure_builtin_packages(connection: Connection, schema: str) -> int:
    """Gieo mọi gói dựng sẵn còn thiếu vào một schema dataset. Trả số gói thêm mới.

    Absence của thư mục `data/<slug>` là **lỗi cấu hình của bản cài**, không
    phải trạng thái hợp lệ để bỏ qua: `load_builtin_package` ném
    `ConfigPackageDataInvalidError` (thiếu tệp) và lỗi đó nổi lên nguyên vẹn —
    cấp dữ liệu kế toán thất bại rõ ràng thay vì thành công với một hệ thống TK
    rỗng mà không ai phát hiện ra cho tới lần ghi sổ đầu tiên.
    """
    bind_seed_schema(connection, schema)
    added = 0
    for slug in BUILTIN_PACKAGE_SLUGS:
        if _seed_one(connection, slug):
            added += 1
    return added
