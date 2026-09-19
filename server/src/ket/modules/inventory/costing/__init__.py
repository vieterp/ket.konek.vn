"""Engine tính giá xuất kho (SRS 09 §3, FR-STK-001..003, lát 8B).

Bốn phương pháp chọn theo năm tài chính (`fiscal_years.inventory_valuation_
method`), mỗi phương pháp là MỘT câu SQL set-based trong `sql/` tính mọi khóa
tồn kho cần tính của một (chi nhánh, năm) — Python lặp theo **vòng** (chuyển
kho chéo khóa: vế đến nhận giá vế đi của vòng trước, dừng khi không dòng nào
đổi) và theo **năm** (horizon ghi cắt ở cuối niên độ, cùng luật `lock_check`),
không bao giờ theo dòng chứng từ (ADR-014, LD-14).

Ba đường vào:

* `job.py` — job nền `inventory.costing.recalc` (per-branch, một transaction,
  khuôn `posting.balances.recalc_job`): tính → repost giá vốn → dựng
  `stock_layers`/`inventory_balances` → xóa dấu bẩn đúng phiên bản đã đọc.
* `affected.py` — xem trước FR-STK-003: chứng từ bị ảnh hưởng + kỳ đã khóa bị
  chạm (RT-11 phương án A: job từ chối trước khi ghi).
* `engine.py` — lõi, nhận `Session` đang mở, không tự commit.

Quyền: `inventory.costing.view` (xem trước) và `inventory.costing.create`
(chạy job) — đăng ký ở `inventory/__init__` cùng ba loại phiếu.
"""

from __future__ import annotations

from typing import Final

from ket.kernel.security.permissions import Action, permission_code

COSTING_PERMISSION_MODULE: Final[str] = "inventory"
COSTING_PERMISSION_CODE: Final[str] = "costing"
COSTING_VIEW: Final[str] = permission_code(
    COSTING_PERMISSION_MODULE, COSTING_PERMISSION_CODE, Action.VIEW
)
COSTING_RUN: Final[str] = permission_code(
    COSTING_PERMISSION_MODULE, COSTING_PERMISSION_CODE, Action.CREATE
)

__all__ = [
    "COSTING_PERMISSION_CODE",
    "COSTING_PERMISSION_MODULE",
    "COSTING_RUN",
    "COSTING_VIEW",
]
