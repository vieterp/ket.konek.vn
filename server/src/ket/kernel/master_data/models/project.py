"""Công trình / vụ việc (`docs/srs/01` §7, LD-08).

Chiều phân tích lõi thứ ba (cột `project_id` cố định trên dòng phát sinh). Khác
`cost_objects` ở chỗ đối tượng tập hợp chi phí là **cách chia chi phí trong một
kỳ**, còn công trình là một thứ có ngày bắt đầu và ngày kết thúc, thường vắt qua
nhiều niên độ — SRS 14 gọi đây là một trong hai phương pháp giá thành của v1
(LD-11: giản đơn theo đối tượng THCP, và theo công trình/vụ việc).

Vì sao là cây: doanh nghiệp xây lắp theo dõi hạng mục con của một công trình, và
báo cáo chi phí phải cộng được lên công trình mẹ (`is_group` cho nút gom).
"""

from __future__ import annotations

from ket.kernel.master_data.base import MasterDataRow, master_data_table_args

PROJECT_TABLE_NAME = "projects"


class Project(MasterDataRow):
    """Một công trình, hạng mục hoặc vụ việc được tập hợp chi phí và doanh thu."""

    __tablename__ = PROJECT_TABLE_NAME
    __table_args__ = master_data_table_args(PROJECT_TABLE_NAME)
