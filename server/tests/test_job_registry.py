"""Hai hàng rào của sổ đăng ký loại job — cả hai đều là hàng rào *review*.

Không cần PostgreSQL: chúng là luật về **cách khai** một loại job, và chúng phải
đỏ ở máy lập trình viên trước khi ai kịp chạy DB.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ket.kernel.errors import JobParamsInvalidError
from ket.kernel.jobs.builtin import INSTALLATION_RUN, PRUNE_SESSIONS
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import (
    CONTROL_OWNER_JOB_TYPES,
    REGISTRY,
    JobContext,
    JobPrivilege,
    JobRegistry,
    JobResult,
    JobType,
)


class _Params(BaseModel):
    """Không tham số."""


def _job(code: str, *, privilege: JobPrivilege = JobPrivilege.DATASET) -> JobType[_Params]:
    def handler(_context: JobContext, _params: _Params) -> JobResult:
        return None

    return JobType(
        code=code,
        permission="system.job.create",
        resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
        params_model=_Params,
        handler=handler,
        privilege=privilege,
    )


def test_registering_the_same_code_twice_is_refused() -> None:
    """Ghi đè im lặng = thân job nào chạy tùy **thứ tự import**.

    Đó là loại lỗi không tái hiện được và không ai nghi ngờ: hai module cùng khai
    một mã, bên thua cuộc mất việc của mình mà bộ test vẫn xanh.
    """
    registry = JobRegistry()
    registry.register(_job("test.trung.ma"))

    with pytest.raises(ValueError, match="đã được đăng ký"):
        registry.register(_job("test.trung.ma"))


def test_a_privileged_job_outside_the_allowlist_is_refused() -> None:
    """Không có danh sách đóng, khai `CONTROL_OWNER` là tự cấp quyền `ket_owner`.

    Từ phase 4 mỗi phân hệ thêm loại job của mình; thứ phải xem xét khi ấy nằm
    rải trong mã module, không ở một chỗ nào người review nhìn được. Danh sách
    đóng biến việc đó thành **một dòng trong diff**.
    """
    registry = JobRegistry()

    with pytest.raises(ValueError, match="CONTROL_OWNER_JOB_TYPES"):
        registry.register(_job("test.tu.cap.quyen", privilege=JobPrivilege.CONTROL_OWNER))


def test_the_allowlist_is_exactly_what_was_reviewed() -> None:
    """Khẳng định **bằng giá trị**, không bằng thuộc tính.

    Cùng bài học với danh sách miễn trừ idempotency: một test kiểu "danh sách có
    chứa X" vẫn xanh khi ai đó thêm Y vào, mà thêm Y mới là thứ cần người xem.
    """
    assert CONTROL_OWNER_JOB_TYPES == frozenset({"system.maintenance.prune_sessions"})


def test_every_privileged_job_requires_the_installation_wide_permission() -> None:
    """Quyền cấp **dataset** không được mở khóa thao tác cấp **bản cài**.

    `role_permissions` nằm trong schema của từng dữ liệu kế toán, nên một mã
    quyền dùng chung sẽ cho người chỉ quản trị doanh nghiệp B chạy được tác vụ
    xóa dữ liệu dùng chung của mọi doanh nghiệp.
    """
    privileged = [
        job_type
        for job_type in REGISTRY.types()
        if job_type.privilege is JobPrivilege.CONTROL_OWNER
    ]

    assert privileged, "mất loại job đặc quyền thì test này không còn canh gì"
    for job_type in privileged:
        assert job_type.permission == INSTALLATION_RUN, job_type.code


def test_the_session_pruning_job_keeps_a_retention_floor() -> None:
    """`retention_days = 0` cắt tại **hiện tại** — xóa sạch phiên vừa kết thúc.

    Đó đúng là thao tác một người muốn làm để xóa dấu vết của chính mình, nên
    sàn không phải chuyện thẩm mỹ tham số.
    """
    with pytest.raises(JobParamsInvalidError) as exc:
        PRUNE_SESSIONS.parse_params({"retention_days": 0})
    assert exc.value.details["field"] == "retention_days"

    assert PRUNE_SESSIONS.parse_params({"retention_days": 7}).retention_days == 7


def test_only_the_session_pruning_job_declares_a_privileged_connection() -> None:
    """Cờ mà API lộ cho màn hình (`requires_privileged_connection`) đọc từ đây.

    Canh ở tầng registry chứ không qua HTTP: quyền chạy loại job này đòi 2FA,
    nên dựng một người gọi có nó qua HTTP cần cả vòng đăng ký thiết bị.
    """
    privileged = {
        job_type.code
        for job_type in REGISTRY.types()
        if job_type.privilege is JobPrivilege.CONTROL_OWNER
    }
    dataset_scoped = {
        job_type.code for job_type in REGISTRY.types() if job_type.privilege is JobPrivilege.DATASET
    }

    assert privileged == {"system.maintenance.prune_sessions"}
    assert "system.maintenance.prune_idempotency_keys" in dataset_scoped


def test_every_job_permission_is_a_registered_permission_code() -> None:
    """Review pre-landing 6G-2 M-4: mã quyền của job phải CÓ trong sổ đăng ký.

    Hậu quả của việc thiếu là tuyệt đối và im lặng: mã không đăng ký ⇒
    `provision_dataset` không gieo dòng nào vào bảng `permissions` ⇒ không vai
    trò nào cấp được ⇒ **kể cả quản trị viên** cũng không xếp hàng nổi job đó,
    403 vĩnh viễn. Không cổng nào bắt được điều ấy cho tới lát 6G-2: phép đột
    biến gỡ `Action.EDIT` khỏi `posting.integrity` sống sót qua ba tệp test.

    Kiểm theo BẤT BIẾN chứ không theo danh sách mã: mọi loại job thêm ở phase
    7–9 tự động nằm trong tầm.
    """
    from ket import model_registry as _registry  # noqa: F401 — nạp mọi bản đăng ký
    from ket.kernel.jobs.registry import REGISTRY as JOB_REGISTRY
    from ket.kernel.security.permissions import REGISTRY as PERMISSION_REGISTRY

    declared = set(PERMISSION_REGISTRY.codes())
    missing = {
        code: JOB_REGISTRY.get(code).permission
        for code in JOB_REGISTRY.codes()
        if JOB_REGISTRY.get(code).permission not in declared
    }
    assert missing == {}, missing
