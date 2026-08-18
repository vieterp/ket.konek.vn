"""Khung nhập liệu Excel cho danh mục (`docs/srs/01` §10, FR-SYS-080..083).

Import gói này **nạp luôn hai loại job** (`job.py`), và việc đó có tác dụng phụ
có chủ đích: chúng đăng ký mã quyền `master.import.*` vào
`kernel/security/permissions.REGISTRY`. Cùng lối đã dùng ở
`kernel/jobs/__init__` và `kernel/master_data/__init__` — buộc mỗi điểm vào tự
nhớ một dòng import là cách một điểm vào sẽ quên, và cái giá của việc quên là
một dữ liệu kế toán được cấp mà thiếu mã quyền trong bảng `permissions`.
"""

from __future__ import annotations

from ket.kernel.excel import job as job

__all__ = ["job"]
