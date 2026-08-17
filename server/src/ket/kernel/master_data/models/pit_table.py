"""Biểu tính thuế thu nhập cá nhân (`docs/srs/01` §7).

**Chỉ bảng đầu ở lát này** — quyết định H50. Các bậc lũy tiến (ngưỡng thu nhập →
thuế suất) và phép tra bậc thuộc phase 9, nơi có luồng tính lương thật gọi tới
chúng. Khai bảng bậc bây giờ là viết logic thuế trước khi có nơi gọi, và hình
dạng nó phải mang (bậc theo tháng hay theo năm, xử lý người nước ngoài, giảm trừ
gia cảnh) chỉ lộ ra khi bảng lương đầu tiên chạy.

Bảng đầu có mặt từ đây để nó hưởng ngay router sinh từ registry và khung nhập
Excel của lát 3C — nếu để tới phase 9 thì phân hệ thuế phải tự nối lại cả hai.
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

PIT_TABLE_TABLE_NAME = "pit_tables"


class PitTable(MasterDataRow):
    """Một biểu thuế TNCN có hiệu lực trong một thời kỳ."""

    __tablename__ = PIT_TABLE_TABLE_NAME
    __table_args__ = master_data_table_args(PIT_TABLE_TABLE_NAME)
