"""Ngữ cảnh của một transaction không được sống sót sang người dùng kế tiếp.

`bind_transaction_scope` đặt ba thứ mỗi transaction: vai trò dataset, `search_path`,
và GUC phạm vi chi nhánh. Cả ba đều phải là `LOCAL` — hết transaction là hết hiệu
lực. Đó chính là điều kiện khiến nhiều người dùng khác nhau dùng chung một
connection pool mà không kế thừa quyền của nhau. Bỏ chữ `LOCAL` đi thì thay đổi
**tồn tại tới hết phiên**, và request kế tiếp nhặt đúng connection đó sẽ chạy
dưới dataset của người trước.

Vì sao cần một tệp riêng với engine riêng: mọi engine trong `conftest.py` dùng
`NullPool`, tức mỗi lần dùng là một connection mới. Không có "quay lại pool" thì
không có cách nào quan sát rò trạng thái — đổi `SET LOCAL ROLE` thành `SET ROLE`
vẫn xanh toàn bộ bộ test. Đây là lỗ do kiểm đột biến chỉ ra, không phải giả định.

`SET` là lệnh có tính transaction trong PostgreSQL: một `SET` không-LOCAL bị
transaction rollback hủy, nhưng **commit thì giữ lại**. Nên test phải chạy một
transaction **có commit** (`unit_of_work`) mới tái hiện được, chứ không phải
`dataset_session` (không commit).
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.grants import APP_ROLE
from ket.kernel.security.models import Branch

pytestmark = pytest.mark.db


@pytest.fixture
def pooled_factory(pooled_app_engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(pooled_app_engine)


def test_next_transaction_on_the_same_connection_starts_clean(
    pooled_app_engine: Engine,
    pooled_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
) -> None:
    """Sau một transaction đã commit, connection quay lại pool phải sạch cả ba thứ."""
    # `branch_ids` phải KHÁC RỖNG. Với tuple rỗng, `set_branch_scope` đặt GUC
    # bằng chuỗi rỗng, nên `assert not branch_scope` đúng bất kể `set_config` có
    # `local=true` hay không — khẳng định rỗng nghĩa. Đã đo: đổi `local` thành
    # `false` mà toàn bộ bộ test vẫn xanh.
    with unit_of_work(
        pooled_factory,
        RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=()),
    ) as session:
        branch = Branch(code="POOL-LEAK-BRANCH", name="Chi nhánh cho phép dò GUC")
        session.add(branch)
        session.flush()
        branch_id = branch.id

    scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=1,
        branch_ids=(branch_id,),
    )
    with unit_of_work(pooled_factory, scope) as session:
        session.add(Branch(code="POOL-LEAK-PROBE", name="Dò rò trạng thái qua pool"))

    # Pool chỉ có một connection nên đây chắc chắn là chính connection vừa trả về.
    with pooled_app_engine.connect() as connection:
        current_user = connection.execute(text("SELECT current_user")).scalar_one()
        branch_scope = connection.execute(
            text("SELECT current_setting('ket.branch_ids', true)")
        ).scalar_one_or_none()
        current_schema = connection.execute(text("SELECT current_schema()")).scalar_one()

    assert current_user == APP_ROLE, (
        f"vai trò rò sang transaction sau: {current_user} — `SET LOCAL ROLE` đã mất chữ LOCAL"
    )
    assert not branch_scope, (
        f"phạm vi chi nhánh rò sang transaction sau: {branch_scope!r} — "
        "`set_config(..., local)` đã mất tham số local"
    )
    assert current_schema == "public", f"search_path rò sang transaction sau: {current_schema}"


def test_a_leaked_role_would_actually_expose_the_other_dataset(
    pooled_app_engine: Engine,
    pooled_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    dataset_beta: DatasetRef,
) -> None:
    """Đối chứng: chứng minh test trên canh một hậu quả thật, không phải hình thức.

    Nếu vai trò rò lại, connection kế tiếp — thuộc về một người dùng khác, có thể
    của doanh nghiệp khác — sẽ chạy dưới vai trò của dataset trước và đọc được
    bảng của nó mà không cần bind gì. Ở trạng thái đúng, chính câu truy vấn đó
    phải bị từ chối vì `ket_app` trần không có quyền trên schema nào.
    """
    scope = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=1, branch_ids=())
    with unit_of_work(pooled_factory, scope) as session:
        session.add(Branch(code="POOL-LEAK-PROBE-2", name="Dò rò lần hai"))

    from sqlalchemy.exc import ProgrammingError

    with pooled_app_engine.connect() as connection:
        with pytest.raises(ProgrammingError) as error:
            connection.execute(text(f'SELECT count(*) FROM "{dataset_alpha.schema_name}".branches'))
    assert getattr(error.value.orig, "sqlstate", None) == "42501"
