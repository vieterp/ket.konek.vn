"""Mọi bảng có `branch_id` phải có policy RLS — kiểm bằng metadata, không bằng danh sách tay.

Đây là bộ test giữ cho tiêu chí "cô lập chi nhánh" còn đúng **qua các phase
sau**. Bộ test hành vi ở `test_rls_branch_isolation.py` chỉ chứng minh cơ chế
chạy trên những bảng đang có; nó sẽ tiếp tục xanh khi phase 4 thêm
`gl_postings` và ai đó quên gọi `enable_branch_rls_statements`.

Test này lấy danh sách bảng từ **metadata của model**, nên bảng mới tự động
nằm trong phạm vi kiểm ngay khi được khai báo. Muốn miễn trừ thì phải sửa
`_EXEMPT` ở đây kèm lý do — tức là một quyết định có ý thức, hiện ra trong diff.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from konek.kernel.datasets.provisioning import DatasetRef
from konek.kernel.security.rls import POLICY_NAME
from konek.model_registry import DatasetBase

pytestmark = pytest.mark.db

_EXEMPT: dict[str, str] = {
    # Danh mục chi nhánh, không phải dữ liệu phát sinh. Policy `WITH CHECK` trên
    # chính nó khiến không ai tạo được chi nhánh mới (id do sequence cấp lúc
    # INSERT). Quyền xem/sửa danh mục thuộc RBAC — xem migration 0001.
    "branches": "danh mục, không phải dữ liệu phát sinh",
    # Nguồn dựng nên chính phạm vi RLS — bật lên là vòng lặp.
    "user_branches": "nguồn của phạm vi RLS",
}


def _tables_with_branch_column() -> set[str]:
    return {
        name
        for name, table in DatasetBase.metadata.tables.items()
        if "branch_id" in table.columns or name == "branches"
    }


def test_every_branch_scoped_table_has_a_policy(
    app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    with app_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT tablename FROM pg_policies "
                "WHERE schemaname = :schema AND policyname = :policy"
            ),
            {"schema": dataset_alpha.schema_name, "policy": POLICY_NAME},
        ).scalars()
        with_policy = set(rows)

    expected = _tables_with_branch_column() - set(_EXEMPT)
    missing = expected - with_policy
    assert not missing, (
        f"Bảng có `branch_id` nhưng thiếu policy {POLICY_NAME}: {sorted(missing)}. "
        "Thêm `enable_branch_rls_statements(<bảng>)` vào migration tạo bảng đó, "
        "hoặc khai miễn trừ kèm lý do trong _EXEMPT."
    )


def test_policy_bearing_tables_have_rls_enabled(
    app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Có policy mà quên `ENABLE ROW LEVEL SECURITY` = policy nằm im, không chặn gì."""
    with app_engine.connect() as connection:
        not_enabled = connection.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relrowsecurity = false "
                "  AND c.relname IN (SELECT tablename FROM pg_policies WHERE schemaname = :schema)"
            ),
            {"schema": dataset_alpha.schema_name},
        ).scalars()
        assert not list(not_enabled)


def test_exemptions_are_still_accurate() -> None:
    """Miễn trừ phải trỏ tới bảng có thật — tên cũ còn sót lại sẽ âm thầm nới phạm vi kiểm."""
    unknown = set(_EXEMPT) - set(DatasetBase.metadata.tables)
    assert not unknown, f"_EXEMPT nhắc tới bảng không tồn tại: {sorted(unknown)}"
