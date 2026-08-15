"""Ranh giới công khai của `ket.worker`.

Mọi thứ module khác được phép chạm phải khai ở đây (Protocol / Pydantic model
/ dataclass có kiểu). Cấm `dict[str, Any]` qua ranh giới module — ADR-015.
Đây cũng là đích neo cho `import-linter` (ADR-004).

**Cố ý gần như rỗng, và sẽ ở nguyên như vậy.** `ket.worker` là một *điểm vào
tiến trình*, không phải thư viện: không tầng nào được import nó (contract C2),
vì `api` import `worker` sẽ biến một tác vụ nền thành lời gọi đồng bộ ngay trong
request — đúng thứ ADR-014 sinh ra để chặn. Hợp đồng thật của hàng đợi nằm ở
`kernel/jobs/registry.py` (`JobType`, `JobContext`, `JobProgress`), nơi cả API
lẫn worker cùng đọc.
"""

from ket.worker.runner import Worker

__all__ = ["Worker"]
