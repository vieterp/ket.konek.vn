"""Hợp nhất hồ sơ sao kê khi gộp hai ngân hàng (FR-SYS-016).

Tồn tại vì `bank_statement_profiles` mang ràng buộc duy nhất `(bank_id, name)`,
và `merge_service` chuyển khóa ngoại bằng một câu `UPDATE` vô danh — nó không
biết bảng con nào mang ràng buộc theo cột danh mục, và cũng không nên biết (H64:
luật hợp nhất đặt cạnh dịch vụ của chính bảng con).

Cổng `test_master_data_merge.py` bắt đúng thiếu sót này ngay khi bảng được thêm:
nó đọc khóa ngoại và ràng buộc duy nhất từ `pg_catalog` lúc chạy, rồi đòi danh
mục tương ứng phải khai hook. Nếu không có nó, lỗi sẽ lộ ra ở ca **thường gặp
nhất** của chính yêu cầu sinh ra thao tác gộp — hai ngân hàng trùng thì cả hai
thường đã có hồ sơ tên "Sao kê Internet Banking" — và người dùng nhận được tên
một chỉ mục nội bộ.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.bank_import.profile_models import NAME_MAX_LENGTH, BankStatementProfile


class BankStatementProfileMergeHook:
    """Đổi tên hồ sơ của ngân hàng nguồn khi nó đụng tên hồ sơ của ngân hàng đích.

    **Đổi tên chứ không xóa**, khác hẳn `PartnerBankAccountMergeHook`. Hai tài
    khoản ngân hàng cùng số ở cùng nhà băng đúng là **một** tài khoản khai hai
    lần, nên bỏ bớt là đúng. Hai hồ sơ **trùng tên** thì không suy ra được điều
    tương tự: tên chỉ là nhãn người dùng đặt, còn nội dung là mười mấy ô cấu hình
    có thể khác nhau hoàn toàn. Xóa một trong hai là vứt đi cách đọc một định
    dạng tệp mà không ai nhận ra cho tới lần nhập sao kê tiếp theo.

    Hồ sơ giữ nguyên nội dung, chỉ tên đổi — người dùng dọn lại bằng tay khi họ
    đã đối chiếu hai định dạng và biết cái nào còn dùng.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        taken = set(
            session.scalars(
                select(BankStatementProfile.name).where(BankStatementProfile.bank_id == target_id)
            ).all()
        )
        moving = (
            session.scalars(
                select(BankStatementProfile).where(BankStatementProfile.bank_id == source_id)
            )
            .unique()
            .all()
        )
        for profile in moving:
            if profile.name not in taken:
                taken.add(profile.name)
                continue
            profile.name = _free_name(profile.name, taken)
            taken.add(profile.name)
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Không có gì phải dọn: `before_move` đã làm mọi tên khác nhau."""


def _free_name(name: str, taken: set[str]) -> str:
    """`"Sao kê"` → `"Sao kê (2)"`, tăng dần cho tới khi không đụng ai.

    Cắt theo trần cột trước khi trả về: `name` là `VARCHAR(100)`, và một hậu tố
    thêm vào một cái tên vốn đã dài đúng trần sẽ làm câu `UPDATE` đổ — biến một
    thao tác gộp hợp lệ thành một lỗi DB thô ở đúng bước dọn dẹp.
    """
    for index in range(2, 1000):
        suffix = f" ({index})"
        candidate = name[: NAME_MAX_LENGTH - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise ValueError(f"Không tìm được tên trống cho hồ sơ {name!r}")
