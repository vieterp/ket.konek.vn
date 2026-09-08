"""Loại job bơm hàng đợi truyền tải hóa đơn (`einvoice.outbox.transmit`).

**Đặt ở `modules/` chứ không `worker/tasks/`** như phác thảo plan viết, vì `api`
là nơi xếp job này còn C2 cấm `api` import `worker` — cùng lý do và cùng khuôn
với `reporting/render_job.py`. Thân job vẫn chạy trong tiến trình worker; chỗ
**khai** loại job thì phải nằm nơi cả hai tiến trình import được.

**Cả lô trong một transaction, và điều đó an toàn.** Thân job không tự commit
(xem `JobContext`), nên không có lựa chọn nào khác; nhưng lập luận lease của
`outbox.py` làm cho lượt rollback giữa lô trở nên vô hại: mọi dòng đã gửi trong
lô quay về `in_flight` với lease **cũ**, và tới lúc lease hết hạn chúng đi đúng
nhánh "phải hỏi trước". Không có đường nào dẫn tới một lượt gửi lại mù.

**Không dùng cơ chế thử lại của hàng đợi job cho việc gửi.** Reaper của
`kernel.jobs` requeue theo `resume_semantics` của **cả job**, trong khi thứ cần
thử lại ở đây là **từng dòng** outbox với `client_ref` riêng. Job này vì thế kết
thúc "thành công" kể cả khi mọi dòng đều hoãn — trạng thái thật nằm trên bảng
outbox, nơi người vận hành đọc được.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, Field

from ket.kernel.errors import (
    EInvoiceProviderNotConfiguredError,
    EInvoiceProviderUnknownError,
)
from ket.kernel.jobs import queue
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.einvoice import EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE
from ket.modules.einvoice.outbox import (
    ClaimedRow,
    claim_due,
    load_for_send,
    mark_needs_reconcile,
)
from ket.modules.einvoice.providers.contracts import ProviderBinding
from ket.modules.einvoice.providers.registry import PROVIDERS
from ket.modules.einvoice.reconcile import transmit

TRANSMIT_JOB_CODE: Final[str] = "einvoice.outbox.transmit"

INVOICE_EDIT: Final[str] = permission_code(
    EINVOICE_PERMISSION_MODULE, INVOICE_PERMISSION_CODE, Action.EDIT
)

DEFAULT_BATCH: Final[int] = 20
"""Số dòng một lượt bơm. Nhỏ vì mỗi dòng là một lượt gọi mạng: lô lớn giữ job
chạy lâu mà không làm nó nhanh hơn, còn lô nhỏ trả quyền điều phối lại cho hàng
đợi giữa các lượt."""


class OutboxTransmitParams(BaseModel):
    """Dòng cần gửi ngay, và trần của lượt quét kèm theo."""

    outbox_id: int | None = Field(default=None, ge=1)
    """Dòng mà endpoint phát hành vừa xếp — lượt gửi **đầu tiên** của nó.

    Đường riêng chứ không để lượt quét nhặt, vì dòng ấy đang giữ lease do chính
    endpoint đặt: lượt quét cố ý không đụng tới dòng còn lease, nếu không nó sẽ
    giành mất dòng một worker khác đang gửi. Xem §"Hai đường vào" trong
    `outbox.py`."""

    batch_size: int = Field(default=DEFAULT_BATCH, ge=1, le=100)


def _run_transmit(context: JobContext, params: OutboxTransmitParams) -> JobResult:
    """Đưa dòng được chỉ định tiến một chặng, rồi quét nốt những dòng phải hỏi lại.

    **Một lượt job = một chặng.** Phát hành qua nhà cung cấp thật là hai lời gọi
    HTTP, và khóa chống trùng do lời gọi thứ nhất sinh ra phải commit trước lời
    gọi thứ hai (xem `reconcile`). Thân job không tự commit được, nên chặng thứ
    hai đi bằng một lượt job mới — xếp ngay tại đây, trong cùng transaction với
    lượt ghi khóa.

    Lượt quét đi kèm là thứ đẩy một dòng `needs_reconcile` về đích: bản cài
    chưa có bộ lập lịch định kỳ nào, nên mỗi lượt phát hành mới cũng là một lượt
    dọn tồn đọng, và `POST /api/v1/einvoices/outbox/actions/pump` là đường dọn
    theo yêu cầu khi chưa có hóa đơn mới nào.

    Lượt quét chạy dưới **chi nhánh đang thao tác** (RLS), nên nó chỉ dọn tồn
    đọng của chính chi nhánh ấy. Đó là hệ quả cố ý của việc bảng có `branch_id`
    + policy: một lượt bơm nhìn thấy chi nhánh khác là một worker gửi hóa đơn
    của chi nhánh mà người bấm nút không có quyền. Cái giá là mỗi chi nhánh phải
    tự dọn — chấp nhận được khi đường dọn là một nút bấm.
    """
    work: list[ClaimedRow] = []
    if params.outbox_id is not None:
        first = load_for_send(
            context.session,
            params.outbox_id,
            # Hàng rào của RT-10. `attempt` tăng lúc worker giành job, trong
            # transaction riêng của hàng đợi, nên nó sống sót qua lượt rollback
            # mà `_fence_before_commit` gây ra — còn lease trên dòng outbox thì
            # không. Xem §"Hai đường vào" trong `outbox.py`.
            force_reconcile=context.attempt > 1,
        )
        if first is not None:
            work.append(first)
    work.extend(claim_due(context.session, limit=params.batch_size))

    binding = ProviderBinding(session=context.session, secret_box=context.secret_box)
    transmitted = 0
    reconciled = 0
    prepared = 0
    unresolved = 0
    for index, item in enumerate(work, start=1):
        try:
            provider = PROVIDERS.resolve(item.row.provider_code, binding)
        except (EInvoiceProviderUnknownError, EInvoiceProviderNotConfiguredError) as error:
            # KHÔNG để bay lên: một dòng mang mã nhà cung cấp mà bản cài không
            # còn cài đặt — hoặc chưa khai thông tin đăng nhập — sẽ kéo cả lô
            # rollback, gồm cả những dòng vừa gửi thành công, và lượt sau giành
            # đúng tập ấy rồi hỏng đúng chỗ ấy: hàng đợi kẹt vĩnh viễn vì một
            # dòng. Hai lỗi này người vận hành sửa được, nên chúng thuộc về
            # `last_error` của dòng chứ không phải lượt chạy.
            mark_needs_reconcile(item.row, message=str(error))
            unresolved += 1
            continue
        report = transmit(context.session, item, provider)
        transmitted += 1
        if report.asked_first and not report.issued:
            reconciled += 1
        if report.prepared:
            # Chặng nạp xong. Khóa của nhà cung cấp vừa ghi vào dòng, và lượt
            # job xếp ở đây đi vào **cùng transaction** — nên khóa và lượt sẽ
            # dùng nó cùng commit hoặc cùng không. Không có cửa sổ nào tồn tại
            # một bản nháp đã nạp mà không ai đi phát hành nó.
            queue.enqueue(
                context.session,
                job_type=TRANSMIT_JOB,
                params={"outbox_id": item.row.id},
                requested_by=context.requested_by,
                branch_id=item.row.branch_id,
            )
            prepared += 1
        context.progress.report(
            int(index * 100 / len(work)), f"Đã xử lý {index}/{len(work)} hóa đơn"
        )
    return {
        "claimed": len(work),
        "transmitted": transmitted,
        "reconciled": reconciled,
        "prepared": prepared,
        "unresolved": unresolved,
    }


TRANSMIT_JOB: Final[JobType[OutboxTransmitParams]] = JobType(
    code=TRANSMIT_JOB_CODE,
    permission=INVOICE_EDIT,
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=OutboxTransmitParams,
    handler=_run_transmit,
    description="Gửi hóa đơn điện tử đang chờ tới nhà cung cấp",
    # Bơm chạy dưới đúng chi nhánh đã xếp nó — mặc định `ACTING_BRANCH`: dòng
    # outbox mang `branch_id`, và một lượt bơm nhìn thấy chi nhánh khác là một
    # worker gửi hóa đơn của chi nhánh mà người bấm nút không có quyền.
    #
    # `direct_enqueue=False` vì cùng lý do với job kết xuất báo cáo: quyền phát
    # hành hóa đơn là quyền **có 2FA**, và một đường xếp hàng thẳng qua
    # `POST /api/v1/jobs` sẽ cho phép gửi hóa đơn mà không đi qua endpoint phát
    # hành — nơi bất biến BR-EIV-03 và hồ sơ đăng ký được kiểm.
    direct_enqueue=False,
)

REGISTRY.register(TRANSMIT_JOB)
