"""Cấu hình và điểm vào của tiến trình worker — không cần PostgreSQL.

Hai thứ đáng canh ở mức này, và cả hai đều hỏng âm thầm nếu không có test:

* Nhịp heartbeat so với lease. Cấu hình sai cho ra triệu chứng "tác vụ chạy mãi
  không xong, thỉnh thoảng chạy hai lần" — hàng giờ để lần ra một phép so sánh
  hai số.
* Kết nối đặc quyền của worker. Mặc định phải là **không có**; một lần đổi mặc
  định là mọi bản cài bỗng chạy tác vụ nền dưới quyền `ket_owner`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ket.settings import Settings
from ket.worker.__main__ import build_parser


def test_the_default_worker_holds_no_privileged_connection() -> None:
    """Mặc định của bản cài: worker không cầm quyền `ket_owner`.

    Test này là chỗ một thay đổi "cho tiện" phải dừng lại: đặt sẵn DSN owner
    trong mặc định nghĩa là mọi bản cài đều để tiến trình chạy nền xóa được dấu
    vết đăng nhập, mà không ai chủ ý quyết định điều đó.
    """
    assert Settings().worker_owner_database_url is None


def test_the_worker_logs_in_with_its_own_role_not_the_api_role() -> None:
    settings = Settings()
    assert "ket_worker" in settings.worker_database_url
    assert settings.worker_database_url != settings.database_url


def test_a_heartbeat_slower_than_the_lease_is_refused_at_startup() -> None:
    """Reaper sẽ cướp job của một worker đang chạy bình thường."""
    with pytest.raises(ValidationError) as exc:
        Settings(job_lease_seconds=20, job_heartbeat_seconds=15)
    assert "job_heartbeat_seconds" in str(exc.value)


def test_a_heartbeat_three_times_inside_the_lease_is_accepted() -> None:
    """Ranh giới: đúng hệ số 3 là ngưỡng chấp nhận được."""
    settings = Settings(job_lease_seconds=45, job_heartbeat_seconds=15)
    assert settings.job_lease_seconds == 45


def test_the_worker_can_be_asked_to_make_a_single_pass() -> None:
    """`--once` cho phép đặt lịch bằng OS thay vì chạy dịch vụ thường trực.

    Bản cài một máy (LD-01) có thể không muốn thêm một dịch vụ chạy suốt; một
    lượt quét theo lịch Task Scheduler là đủ.
    """
    assert build_parser().parse_args(["--once"]).once is True
    assert build_parser().parse_args([]).once is False
