"""Hợp đồng với nhà cung cấp hóa đơn điện tử (FR-EIV-001/042, LD-10).

Hai phương thức, và cặp ấy là tối thiểu **không cắt được nữa** cho pipeline
RT-10: `issue` đưa tờ hóa đơn đi, `query_status` trả lời câu hỏi duy nhất mà một
lượt gửi mất tín hiệu để lại — "anh nhận chưa?". Bỏ phương thức thứ hai là bỏ
luôn khả năng phân biệt *chưa gửi được* với *đã gửi rồi mà không nghe thấy trả
lời*, và hai thứ ấy khác nhau đúng bằng một tờ hóa đơn thừa.

Gửi cho người mua, hủy, thay thế, điều chỉnh **chưa khai ở đây**: chúng thuộc
7E-2 và 7F, và một phương thức Protocol không ai gọi là một phương thức mọi bản
cài phải viết `raise NotImplementedError` để thỏa mãn.

**Ba loại kết quả, không phải hai.** `IssueOutcome` phân biệt *nhận* / *từ chối*
/ *không rõ*; đường thứ ba là toàn bộ lý do `OutboxStatus.NEEDS_RECONCILE` tồn
tại. Adapter nào gộp "không rõ" vào "từ chối" sẽ làm hệ thống phát hành lại một
tờ hóa đơn provider đã cấp mã.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderAcceptance(StrEnum):
    """Ba câu trả lời có thể có cho một lượt gửi."""

    ACCEPTED = "accepted"
    """Provider đã nhận và chịu trách nhiệm về tờ hóa đơn."""
    REJECTED = "rejected"
    """Provider trả lời rõ ràng là **không** — sai định dạng, sai ký hiệu, hết
    hạn chứng thư. Câu trả lời này chắc chắn, nên xử lý được ngay."""
    UNKNOWN = "unknown"
    """Không có câu trả lời nào đáng tin: hết thời gian chờ, đứt kết nối, mã lỗi
    lạ. **Không phải** từ chối — xem docstring đầu tệp."""


@dataclass(frozen=True)
class IssueOutcome:
    """Kết quả một lượt gửi tờ hóa đơn."""

    acceptance: ProviderAcceptance
    provider_ref: str | None = None
    """Mã tờ hóa đơn phía provider, nếu họ dùng mã riêng khác `client_ref`."""
    tax_authority_code: str | None = None
    lookup_code: str | None = None
    message: str | None = None
    """Lý do, dùng cho cả `REJECTED` (giải trình cho người dùng) lẫn `UNKNOWN`
    (dấu vết cho người vận hành)."""


@dataclass(frozen=True)
class ProviderStatus:
    """Kết quả tra cứu theo `client_ref`.

    `known` là câu trả lời cốt lõi: provider **có** biết tới khóa chống trùng
    này không. `False` nghĩa là lượt gửi trước chưa từng tới nơi, và chỉ lúc ấy
    mới được gửi lại.
    """

    known: bool
    outcome: IssueOutcome | None = None
    """Kết quả provider đang giữ cho `client_ref` ấy, khi `known`."""


class EInvoiceProvider(Protocol):
    """Adapter một nhà cung cấp. Đăng ký theo `provider_code` — xem `registry`."""

    def issue(self, *, client_ref: UUID, invoice_id: UUID) -> IssueOutcome:
        """Gửi tờ hóa đơn, dùng `client_ref` làm khóa chống trùng.

        Bản cài **phải** truyền `client_ref` sang provider dưới đúng vai trò
        idempotency key của họ (`ikey` với EasyInvoice). Gửi lại cùng
        `client_ref` phải trả về đúng kết quả cũ, không lập tờ thứ hai.
        """
        ...

    def query_status(self, *, client_ref: UUID) -> ProviderStatus:
        """Provider đang giữ kết quả nào cho khóa chống trùng này."""
        ...
