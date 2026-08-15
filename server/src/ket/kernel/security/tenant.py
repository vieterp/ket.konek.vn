"""Đặt phạm vi chi nhánh (GUC) cho transaction hiện hành.

Cặp đôi với `rls.py`: ở đó định nghĩa policy đọc GUC, ở đây đặt GUC. Hai nửa
phải luôn đi cùng nhau — đặt GUC mà quên policy thì không cô lập gì; có policy
mà quên đặt GUC thì fail-closed (không thấy dòng nào), tức là hỏng **rõ ràng**
chứ không rò dữ liệu. Thứ tự hỏng đó là có chủ đích.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from ket.kernel.security.rls import BRANCH_SCOPE_GUC

# Tên GUC ràng buộc như **tham số**, không ghép chuỗi: `set_config` và
# `current_setting` nhận nó như đối số hàm chứ không như identifier, nên không
# có lý do gì phải nối chuỗi ở đây. Ngoài việc bỏ được một ngoại lệ khỏi
# `tests/test_no_sql_string_interpolation.py`, câu lệnh có tham số buộc psycopg
# dùng extended protocol — protocol không cho nhiều câu lệnh trong một lần gửi
# (xem ADR-017 §Consequences về tiêm nhiều câu lệnh).
_SET_CONFIG = text("SELECT set_config(:guc, :value, true)")
_GET_CONFIG = text("SELECT current_setting(:guc, true)")


def set_branch_scope(session: Session, branch_ids: Sequence[int]) -> None:
    """Khai các chi nhánh mà transaction này được phép chạm.

    `is_local => true` khiến giá trị mất khi transaction kết thúc. Đây là lý do
    duy nhất khiến việc dùng chung connection pool giữa các người dùng khác
    nhau là an toàn — bỏ `true` đi là mở đường cho request sau kế thừa quyền
    của request trước.

    Danh sách rỗng nghĩa là **không chi nhánh nào** → không đọc/ghi được dòng
    nào có `branch_id`. Đó là trạng thái đúng cho một người dùng chưa được gán
    chi nhánh, không phải trạng thái "xem tất".
    """
    for branch_id in branch_ids:
        # `bool` là lớp con của `int` trong Python: `True` sẽ lọt qua kiểm kiểu
        # tĩnh rồi thành chuỗi "True" trong GUC và làm hỏng phép ép `::int[]`
        # ngay giữa policy RLS.
        if isinstance(branch_id, bool):
            raise TypeError(f"branch_id phải là int, nhận {branch_id!r}")
    session.execute(
        _SET_CONFIG,
        {"guc": BRANCH_SCOPE_GUC, "value": ",".join(str(b) for b in branch_ids)},
    )


def current_branch_scope(session: Session) -> tuple[int, ...]:
    """Đọc lại phạm vi chi nhánh đang có hiệu lực (dùng cho test và chẩn đoán)."""
    raw = session.execute(_GET_CONFIG, {"guc": BRANCH_SCOPE_GUC}).scalar_one_or_none()
    if not raw:
        return ()
    return tuple(int(part) for part in raw.split(","))
