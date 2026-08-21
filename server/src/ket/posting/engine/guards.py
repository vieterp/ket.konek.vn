"""Cổng cảnh báo nghiệp vụ cắm vào `PostingService` (FR-SYS-062, phase-06).

Validator (`engine/validators/`) trả lời "chứng từ có **hợp lệ** không" — sai
là sai, không thương lượng. Guard trả lời câu hỏi khác: "ghi sổ xong thì có
**hệ quả nghiệp vụ** đáng cảnh báo không" (chi quá tồn quỹ, vượt ngưỡng nợ,
xuất dưới tồn tối thiểu — bảng cảnh báo `docs/srs/01` §8.2). FR-SYS-062 đòi
mỗi cảnh báo cấu hình được **ba mức**: Không cảnh báo / Cảnh báo / Cảnh báo và
không cho ghi sổ — tức cùng một phát hiện, lúc là thông báo phải xác nhận, lúc
là lệnh chặn. Hai loại câu trả lời đó không nhét được vào validator, nên guard
là pipeline riêng, chạy **sau khi** bộ kiểm hợp lệ đã sạch: guard đọc số liệu
để phán hệ quả, và số liệu của một chứng từ không hợp lệ không đáng đọc.

Ba mức ánh xạ vào pipeline thế nào:

* **Không cảnh báo** — guard tự đọc cấu hình của nó và trả về rỗng.
* **Cảnh báo** — `GuardFinding(blocking=False)`: lượt ghi đầu bị từ chối kèm
  đánh dấu `warning=1`, client hỏi lại người dùng rồi gửi lại với
  `acknowledge_warnings=true`; lượt sau đi qua.
* **Chặn** — `GuardFinding(blocking=True)`: từ chối như một vi phạm thường,
  `acknowledge_warnings` không có tác dụng.

Từng guard cụ thể (CashBalanceGuard…) thuộc module nghiệp vụ và đăng ký vào
`GUARD_REGISTRY` lúc import — cùng khuôn `posting.documents.registry`, để
posting không import module nào (luật phụ thuộc C4) mà mọi module dùng lại
được cùng cơ chế thay vì mỗi phân hệ tự chế một lối cảnh báo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy.orm import Session

from ket.kernel.errors import PostingViolation
from ket.posting.documents.models import Voucher
from ket.posting.engine.prepared import PreparedLine

GUARD_WARNING_DETAIL: Final[str] = "warning"
"""Khóa trong `PostingViolation.details` đánh dấu "đây là cảnh báo xác nhận
được" (`warning=1`). Client phân biệt bằng khóa này: mọi vi phạm trong phản
hồi đều mang nó → hiện hộp "Vẫn ghi sổ?"; lẫn một vi phạm không mang → chỉ
hiện lỗi, vì gửi lại kèm xác nhận cũng sẽ bị chặn y nguyên."""


@dataclass(frozen=True)
class GuardFinding:
    """Một phát hiện của guard: nội dung + mức xử lý theo cấu hình hiện hành.

    `blocking` do **guard** quyết lúc chạy (nó là bên đọc cấu hình ba mức của
    chính nó), không phải thuộc tính tĩnh của loại cảnh báo — cùng một "chi quá
    tồn" là chặn ở dữ liệu kế toán này và chỉ nhắc ở dữ liệu kế toán khác.
    """

    violation: PostingViolation
    blocking: bool


class PostingGuard(Protocol):
    """Một cổng cảnh báo. Bản cài thuộc module nghiệp vụ, đăng ký lúc import."""

    def check(
        self,
        session: Session,
        *,
        voucher: Voucher,
        lines: Sequence[PreparedLine],
    ) -> Sequence[GuardFinding]:
        """Soi hệ quả của việc ghi sổ chứng từ này; rỗng = không có gì đáng nói.

        Chạy trong transaction ghi sổ, **trước** khi INSERT `gl_postings`:
        guard đọc số dư hiện có rồi cộng nhẩm phần chứng từ sắp ghi từ `lines`
        (đã quy đổi VND) — không có chuyện ghi thử rồi xóa.
        """
        ...


class PostingGuardRegistry:
    """Sổ đăng ký guard của một tiến trình — đối tượng để test dựng registry riêng."""

    def __init__(self) -> None:
        self._guards: list[PostingGuard] = []

    def register(self, guard: PostingGuard) -> None:
        self._guards.append(guard)

    def guards(self) -> tuple[PostingGuard, ...]:
        return tuple(self._guards)


GUARD_REGISTRY: Final[PostingGuardRegistry] = PostingGuardRegistry()
"""Registry của tiến trình. Module đăng ký guard lúc import (qua
`ket.model_registry`) — cùng chỗ với đăng ký loại chứng từ và mã quyền."""


def run_guards(
    session: Session,
    *,
    voucher: Voucher,
    lines: Sequence[PreparedLine],
    registry: PostingGuardRegistry | None = None,
) -> tuple[list[PostingViolation], list[PostingViolation]]:
    """Chạy hết mọi guard, trả `(chặn, cảnh báo)` — không dừng ở phát hiện đầu.

    Chạy **hết** rồi mới trả, cùng triết lý "trả toàn bộ" của bộ kiểm ghi sổ
    (phase-04): người dùng sửa một lượt, không bị nhỏ giọt mỗi lần một lỗi.
    Cảnh báo được đóng dấu `warning=1` ngay tại đây (một chỗ, mọi guard) để
    client không phụ thuộc vào việc từng guard nhớ tự đóng dấu.
    """
    active = (registry or GUARD_REGISTRY).guards()
    blocking: list[PostingViolation] = []
    warnings: list[PostingViolation] = []
    for guard in active:
        for finding in guard.check(session, voucher=voucher, lines=lines):
            if finding.blocking:
                blocking.append(finding.violation)
            else:
                finding.violation.details[GUARD_WARNING_DETAIL] = 1
                warnings.append(finding.violation)
    return blocking, warnings
