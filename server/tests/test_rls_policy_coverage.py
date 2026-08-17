"""Mọi bảng có `branch_id` phải có policy RLS — kiểm bằng metadata, không bằng danh sách tay.

Đây là bộ test giữ cho tiêu chí "cô lập chi nhánh" còn đúng **qua các phase
sau**. Bộ test hành vi ở `test_rls_branch_isolation.py` chỉ chứng minh cơ chế
chạy trên những bảng đang có; nó sẽ tiếp tục xanh khi phase 4 thêm
`gl_postings` và ai đó quên gọi `enable_branch_rls_statements`.

Test này lấy danh sách bảng từ **metadata của model**, nên bảng mới tự động
nằm trong phạm vi kiểm ngay khi được khai báo. Muốn miễn trừ thì phải thêm một
dòng vào `_EXEMPT` kèm lý do — tức một quyết định có ý thức, hiện ra trong diff.

**Đã thử miễn trừ "theo loại" ở lát 3B-1 rồi bỏ.** Ý tưởng là: mọi lớp con
`MasterDataRow` tự động được miễn, vì lý do (`NULL` = dùng chung toàn công ty) là
sự thật của cả loại chứ không của từng bảng — hai mươi dòng mang cùng một lý do
làm chìm mất hai miễn trừ thật sự đáng chú ý. Review thù địch chứng minh nó
**hỏng từ tiền đề**: `MasterDataRow.branch_id` khai `nullable=True`, nên đối
trọng "phải có `branch_id` cho `NULL`" là **hằng đúng** — mọi lớp con thỏa mãn
tự động. Một `class GlPostingsLikeFactTable(MasterDataRow)` ở phase 4 sẽ vào
thẳng diện miễn trừ với cả hai test vẫn xanh, tức là cô lập chi nhánh tắt đi mà
không dòng diff nào nói ra.

Bài học giữ lại: một cổng bảo mật đổi từ "liệt kê tường minh" sang "suy theo
tính chất" chỉ an toàn khi tính chất ấy **có thể sai** — nếu không, đối trọng
chỉ là một câu khẳng định lại chính định nghĩa. Hai mươi dòng lặp lại là cái giá
rẻ hơn nhiều so với một suy luận không thể đỏ.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.security.rls import POLICY_NAME
from ket.model_registry import DatasetBase

pytestmark = pytest.mark.db

_EXEMPT: dict[str, str] = {
    # Danh mục chi nhánh, không phải dữ liệu phát sinh. Policy `WITH CHECK` trên
    # chính nó khiến không ai tạo được chi nhánh mới (id do sequence cấp lúc
    # INSERT). Quyền xem/sửa danh mục thuộc RBAC — xem migration 0001.
    "branches": "danh mục, không phải dữ liệu phát sinh",
    # Nguồn dựng nên chính phạm vi RLS — bật lên là vòng lặp.
    "user_branches": "nguồn của phạm vi RLS",
    # ---------------------------------------------------------------- danh mục
    # Từ phase 3. Lý do **giống nhau** cho cả nhóm: `branch_id IS NULL` nghĩa là
    # dùng chung toàn công ty (FR-SYS-018), và policy chi nhánh sẽ giấu đúng
    # những dòng đó khỏi mọi người. Cùng lập luận đã áp cho `branches` — ai được
    # xem/sửa danh mục là câu hỏi của RBAC, không phải của cô lập dòng; lớp lọc
    # theo chi nhánh nằm ở `MasterDataService._visible_to` và ở tầng HTTP.
    #
    # Liệt kê từng bảng dù lý do trùng nhau: xem docstring module — bản "suy theo
    # lớp gốc" đã thử và bỏ vì đối trọng của nó là hằng đúng. Một dòng ở đây là
    # cái giá để `class X(MasterDataRow)` ở phase 4 **không** tự tắt được RLS.
    "cost_objects": "danh mục, NULL = dùng chung toàn công ty",
    "expense_items": "danh mục, NULL = dùng chung toàn công ty",
    "projects": "danh mục, NULL = dùng chung toàn công ty",
    "project_types": "danh mục, NULL = dùng chung toàn công ty",
    "contracts": "danh mục, NULL = dùng chung toàn công ty",
    "warehouses": "danh mục, NULL = dùng chung toàn công ty",
    "units_of_measure": "danh mục, NULL = dùng chung toàn công ty",
    "asset_types": "danh mục, NULL = dùng chung toàn công ty",
    "tool_types": "danh mục, NULL = dùng chung toàn công ty",
    "payment_terms": "danh mục, NULL = dùng chung toàn công ty",
    "banks": "danh mục, NULL = dùng chung toàn công ty",
    "timekeeping_symbols": "danh mục, NULL = dùng chung toàn công ty",
    "document_types": "danh mục, NULL = dùng chung toàn công ty",
    "invoice_forms": "danh mục, NULL = dùng chung toàn công ty",
    "pit_tables": "danh mục, NULL = dùng chung toàn công ty",
    "excise_tax_tables": "danh mục, NULL = dùng chung toàn công ty",
    "resource_tax_tables": "danh mục, NULL = dùng chung toàn công ty",
    "partners": "danh mục, NULL = dùng chung toàn công ty",
    "employees": "danh mục, NULL = dùng chung toàn công ty",
}


def _tables_with_branch_column() -> set[str]:
    return {
        name
        for name, table in DatasetBase.metadata.tables.items()
        if "branch_id" in table.columns or name == "branches"
    }


def test_every_master_data_table_is_listed_explicitly() -> None:
    """Danh mục mới phải **thêm một dòng** vào `_EXEMPT`, không được miễn tự động.

    Đây là chỗ cái giá của việc liệt kê tay được thu lại: người thêm một danh mục
    ở lát 3B-2 hay phase 5 sẽ thấy test này đỏ kèm đúng tên bảng còn thiếu, thay
    vì phải nhớ rằng có một tệp test cần cập nhật.

    Chiều ngược lại — một bảng **phát sinh** kế thừa `MasterDataRow` — vẫn phải
    qua cùng cửa: nó sẽ đỏ ở đây, và người sửa buộc phải viết ra lý do miễn trừ
    trong một dòng mà người review nhìn thấy. Đó chính là thứ bản "suy theo lớp
    gốc" không làm được.
    """
    catalog_tables = {
        str(mapper.class_.__tablename__)
        for mapper in DatasetBase.registry.mappers
        if issubclass(mapper.class_, MasterDataRow)
    }
    missing = catalog_tables - set(_EXEMPT)

    assert not missing, (
        f"Bảng danh mục chưa khai miễn trừ RLS: {sorted(missing)}. "
        "Thêm một dòng vào `_EXEMPT` kèm lý do — hoặc, nếu đây là bảng phát sinh, "
        "gọi `enable_branch_rls_statements(<bảng>)` trong migration tạo nó."
    )


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
