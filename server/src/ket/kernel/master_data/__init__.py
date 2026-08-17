"""Danh mục dùng chung (`docs/srs/01`, FR-SYS-010..018).

Import gói này **nạp luôn sổ đăng ký danh mục** (`registry.py`), và việc đó có
tác dụng phụ: mỗi danh mục đăng ký một loại quyền vào
`kernel/security/permissions.REGISTRY`. Cùng lối đã dùng ở `kernel/jobs/__init__`
— buộc mỗi điểm vào tự nhớ `import ket.kernel.master_data.registry` là cách một
điểm vào sẽ quên, và cái giá của việc quên là một dữ liệu kế toán được cấp mà
thiếu mã quyền danh mục trong bảng `permissions`.

`ket.model_registry` import gói này để nạp model, nên mọi đường đi qua đó
(migration, khởi tạo dataset, khởi động app) đều có đủ.
"""

from __future__ import annotations

from ket.kernel.master_data import registry as registry

__all__ = ["registry"]
