"""Sổ đăng ký loại job — mỗi loại khai đủ thứ worker cần biết trước khi chạy.

Cùng khuôn với registry mã quyền (`security/permissions.py`) và vì cùng một lý
do: từ phase 4 trở đi mỗi phân hệ thêm job của mình (tính giá xuất, khấu hao,
phân bổ, tính lại số dư), và một danh sách `if job_type == "..."` trong worker
sẽ là nơi mọi phase phải sửa chung một tệp.

Bốn thứ **bắt buộc** khai, không có mặc định cho ba trong số đó:

* `resume_semantics` (RT-13) — chạy lại một job dở dang có an toàn không. Không
  mặc định: câu trả lời "chắc là được" đã đủ để nhân đôi một bảng phân bổ khấu
  hao. Reaper đọc chính giá trị này để quyết định requeue hay đánh hỏng.
* `permission` — mã quyền cần có để **xếp hàng** loại job này. Job chạy dưới
  danh nghĩa người yêu cầu, nên cổng quyền phải ở lúc xếp hàng.
* `params_model` — Pydantic model của tham số. Không `dict[str, Any]` đi qua
  ranh giới (LD-13/ADR-015), và tham số sai bị bắt lúc **xếp hàng** chứ không
  phải sau vài phút nằm trong hàng đợi.
* `privilege` — job chạy dưới vai trò dataset (mặc định) hay cần kết nối
  `ket_owner`. Xem `JobPrivilege`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ket.kernel.errors import JobParamsInvalidError, JobTypeUnknownError
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.persistence.types import AuditValues
from ket.kernel.security.rls import validate_identifier


class JobPrivilege(StrEnum):
    """Kết nối mà thân job cần.

    `DATASET` là mặc định và là thứ gần như mọi job nghiệp vụ dùng: worker
    `SET LOCAL ROLE ds_<mã>_app` rồi đặt phạm vi chi nhánh, nên job bị RLS chặn
    đúng như một request HTTP.

    `CONTROL_OWNER` là ngoại lệ có kiểm soát cho việc dọn dẹp chạm bảng schema
    điều khiển mà vai trò runtime **cố ý** không có quyền (dọn phiên đăng nhập:
    `DELETE` trên `auth_sessions` bị thu hồi khỏi `ket_app` để một lỗ hổng tầng
    API không xóa được dấu vết ai đã đăng nhập). Worker chỉ chạy được loại này
    khi bản cài cấu hình **tường minh** một DSN `ket_owner` riêng cho worker;
    không cấu hình thì job hỏng với thông điệp chỉ đúng cách xử lý, chứ worker
    không âm thầm cầm quyền owner.
    """

    DATASET = "dataset"
    CONTROL_OWNER = "control_owner"


CONTROL_OWNER_JOB_TYPES: Final[frozenset[str]] = frozenset({"system.maintenance.prune_sessions"})
"""Danh sách **đóng** các loại job được phép chạy dưới kết nối `ket_owner`.

Không có nó, từ phase 4 một module chỉ cần khai `privilege=CONTROL_OWNER` là tự
cấp cho mình quyền owner — không review nào ép được, vì thứ phải xem xét nằm rải
trong mã module chứ không ở một chỗ. Ở đây thì thêm một loại là sửa **tệp này**,
tức là một dòng trong diff mà người review nhìn thấy.

Kèm điều kiện: mọi loại trong danh sách phải đòi `system.installation.create`
(quyền cấp bản cài, có 2FA) — `JobRegistry.register` kiểm cả hai."""


class JobCancelled(Exception):  # noqa: N818 — luồng điều khiển, không phải lỗi
    """Thân job dừng lại vì người dùng đã bấm Hủy.

    Ngoại lệ chứ không giá trị trả về: hủy phải **rollback** phần việc dở dang,
    và cách duy nhất để một `with unit_of_work(...)` rollback là ném ra khỏi
    khối. Trả về một giá trị đặc biệt sẽ commit đúng nửa bảng phân bổ vừa ghi —
    tệ hơn hẳn việc không làm gì.

    Không phải `DomainError`: nó không đi ra API và không có mã lỗi nào cho
    client. `ket.worker` bắt nó và đánh dấu job `cancelled`.
    """


class JobProgress(Protocol):
    """Kênh báo tiến độ + nhận yêu cầu hủy, worker cấp cho thân job.

    Protocol chứ không lớp cụ thể: `kernel` khai hợp đồng, `ket.worker` thi công
    nó bằng một connection riêng (tiến độ phải nhìn thấy được **trong khi**
    transaction nghiệp vụ còn đang mở — nếu ghi cùng transaction thì thanh tiến
    độ đứng im tới lúc job xong, tức là vô dụng). Test cấp bản giả.
    """

    def report(self, percent: int, message: str | None = None) -> None:
        """Ghi tiến độ 0–100 và một dòng mô tả việc đang làm."""

    def cancel_requested(self) -> bool:
        """Người dùng đã bấm Hủy chưa — job kiểm **giữa các lô**, không giữa chừng."""

    def checkpoint(self, state: AuditValues) -> None:
        """Ghi mốc để lần chạy sau đi tiếp (chỉ job khai `checkpointed`).

        Ghi bằng connection riêng của worker, **ngoài** transaction nghiệp vụ —
        nếu không, một mốc ghi cùng transaction sẽ biến mất đúng lúc cần nó
        nhất (job hỏng giữa chừng và transaction rollback).

        Hệ quả phải biết trước khi dùng ở phase 4: mốc và phần việc nó mô tả
        **không nguyên tử** với nhau. Job `checkpointed` vì thế phải chịu được
        việc chạy lại một lô đã làm xong — cùng đòi hỏi mà `idempotent-restart`
        đặt ra, chỉ hẹp hơn về phạm vi.
        """


@dataclass(frozen=True)
class JobContext:
    """Mọi thứ thân job được phép chạm.

    `session` đã bind đúng dataset (vai trò + `search_path` + phạm vi chi nhánh)
    và nằm trong một transaction đang mở — cùng hợp đồng với `unit_of_work` của
    một request HTTP: thân job **không** tự commit, người mở transaction là
    người đóng.
    """

    job_id: UUID
    session: Session
    progress: JobProgress
    attempt: int
    dataset_schema: str
    branch_id: int | None
    requested_by: int

    storage_root: Path | None = None
    """Thư mục kho tệp của bản cài, khi job cần đọc một tệp đã tải lên.

    `None` khi bản cài chưa cấu hình `KET_ATTACHMENTS_DIR` — thân job nào cần
    tệp thì tự nói ra điều đó bằng một lỗi nghiệp vụ chỉ đúng biến môi trường
    phải đặt, thay vì đổ ở một `AttributeError` giữa chừng.

    Đặt ở đây thay vì để thân job tự đọc `Settings`: `kernel` không được biết
    tới lớp cấu hình của ứng dụng (`import-linter` canh chiều phụ thuộc đó), và
    một thân job tự dựng `Settings()` lấy lại từ biến môi trường sẽ đọc ra một
    cấu hình khác với cấu hình worker đang chạy dưới.
    """


JobResult = AuditValues | None
"""Kết quả job ghi vào `jobs.result` (JSONB). `None` = không có gì để trả."""


@dataclass(frozen=True)
class JobType[ParamsT: BaseModel]:
    """Một loại tác vụ nền có thể xếp hàng."""

    code: str
    permission: str
    resume_semantics: ResumeSemantics
    params_model: type[ParamsT]
    handler: Callable[[JobContext, ParamsT], JobResult]
    privilege: JobPrivilege = JobPrivilege.DATASET
    description: str = ""

    direct_enqueue: bool = True
    """Xếp được hàng thẳng qua `POST /api/v1/jobs` hay bắt buộc đi qua endpoint riêng.

    `False` cho loại job mà **phạm vi quyền phụ thuộc tham số**. Nhập liệu danh
    mục là ca đầu tiên: `permission` ở đây là một chuỗi tĩnh, nên nó trả lời được
    "người này có được dùng chức năng nhập liệu không" nhưng không trả lời được
    "trên danh mục nào". Câu thứ hai chỉ trả lời được ở chỗ biết `slug` — tức
    `routers/imports.py`, nơi có `spec.permission_code(...)`.

    Không có cờ này thì hai lớp quyền ấy vô nghĩa: ai có `master.import.create`
    sẽ ghi được vào **mọi** danh mục chỉ bằng cách gọi thẳng endpoint hàng đợi
    với `type` và `params` tự đặt, đi vòng qua đúng phép kiểm per-danh-mục mà
    H48 dựng ra ("kế toán kho sửa được danh mục kho không có nghĩa là sửa được
    điều khoản thanh toán")."""

    def __post_init__(self) -> None:
        # Mã loại job đi vào cột `jobs.type` và vào tham số của client, nên nó
        # chịu cùng luật đặt tên với mã quyền: `{nhóm}.{việc}`, chữ thường.
        for part in self.code.split("."):
            validate_identifier(part)

    def parse_params(self, raw: Mapping[str, object] | None) -> ParamsT:
        """Ép tham số thô về model đã khai. Sai → lỗi nghiệp vụ, không phải 500."""
        try:
            return self.params_model.model_validate(raw or {})
        except ValidationError as exc:
            raise JobParamsInvalidError(
                "Tham số của tác vụ không hợp lệ",
                job_type=self.code,
                # Chỉ số **lượng** lỗi và trường đầu tiên: thông điệp Pydantic
                # thô lộ tên lớp và cấu trúc nội bộ ra ngoài API.
                errors=len(exc.errors()),
                field=".".join(str(part) for part in exc.errors()[0]["loc"]) or None,
            ) from exc

    def run(self, context: JobContext, raw_params: Mapping[str, object] | None) -> JobResult:
        """Phân giải tham số rồi gọi thân job."""
        return self.handler(context, self.parse_params(raw_params))


class JobRegistry:
    """Sổ đăng ký của một tiến trình.

    Là đối tượng chứ không phải biến toàn cục cứng: test dựng registry riêng để
    kiểm vòng đời job mà không phải đăng ký một loại giả vào registry thật —
    cùng lý do với `PermissionRegistry`.
    """

    def __init__(self) -> None:
        self._types: dict[str, JobType[Any]] = {}

    def register(self, job_type: JobType[Any]) -> None:
        """Đăng ký một loại. Trùng mã → lỗi lập trình, ném ngay.

        Ném thay vì ghi đè: hai module cùng khai một mã nghĩa là một trong hai
        sẽ chạy thân job của bên kia, tùy thứ tự import — thứ không tái hiện
        được và không ai nghi ngờ.
        """
        if job_type.code in self._types:
            raise ValueError(f"Loại tác vụ {job_type.code} đã được đăng ký")
        if (
            job_type.privilege is JobPrivilege.CONTROL_OWNER
            and job_type.code not in CONTROL_OWNER_JOB_TYPES
        ):
            raise ValueError(
                f"Loại tác vụ {job_type.code} khai kết nối đặc quyền nhưng không nằm trong "
                "danh sách CONTROL_OWNER_JOB_TYPES — thêm vào danh sách đó (và chịu review) "
                "trước khi đăng ký"
            )
        self._types[job_type.code] = job_type

    def get(self, code: str) -> JobType[Any]:
        """Loại job theo mã. Không có → `JobTypeUnknownError` (không `KeyError`).

        Đây là đường mà **worker chạy binary cũ** đi vào: nó nhận một job có mã
        mà registry của nó chưa biết. Lỗi có mã cho phép worker đánh dấu job
        hỏng kèm thông điệp đúng nguyên nhân, thay vì để một `KeyError` giết
        vòng lặp và biến nó thành job mồ côi.
        """
        try:
            return self._types[code]
        except KeyError:
            raise JobTypeUnknownError(
                "Bản cài này không biết loại tác vụ đó", job_type=code
            ) from None

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))

    def types(self) -> tuple[JobType[Any], ...]:
        return tuple(self._types[code] for code in sorted(self._types))


REGISTRY: Final[JobRegistry] = JobRegistry()
"""Registry của tiến trình. `kernel.jobs.builtin` đăng ký loại nền tảng lúc
import; module nghiệp vụ đăng ký loại của mình từ phase 4."""
