"""Nhà cung cấp `internal` — phát hành **không qua bên thứ ba**.

Đây không phải một bản giả cho test: nó là hành vi phát hành nội bộ mà 7D đã
dựng (cấp số + lật trạng thái, không gửi đi đâu), nay nói ra thành một adapter
để đường ghi của `outbox` chỉ có **một** hình dạng. Không có nó, `outbox.py`
phải mang một nhánh "nếu chưa cấu hình nhà cung cấp thì bỏ qua bước gửi" — đúng
loại nhánh mà registry sinh ra để không phải viết.

Nó cũng là người dùng thật đầu tiên của registry, nên cơ chế tra cứu adapter
được đo trên đường chạy thật ngay từ lát này thay vì chỉ trong test.

**Dùng thật ở đâu:** bản cài chưa ký hợp đồng với nhà cung cấp nào, hoặc dữ liệu
huấn luyện. Hóa đơn đi hết vòng đời trong phần mềm; số vẫn cấp gap-free và mọi
bất biến của 7D vẫn canh y nguyên. Nó **không** thay được nghĩa vụ pháp lý gửi
cơ quan thuế — 7E-2 cắm EasyInvoice vào đúng chỗ này.
"""

from __future__ import annotations

from uuid import UUID

from ket.modules.einvoice.providers.contracts import (
    IssueOutcome,
    ProviderAcceptance,
    ProviderStatus,
)
from ket.modules.einvoice.providers.registry import PROVIDERS

INTERNAL_PROVIDER_CODE = "internal"


class InternalProvider:
    """Nhận mọi tờ hóa đơn, ngay lập tức, không đi ra khỏi tiến trình."""

    def issue(self, *, client_ref: UUID, invoice_id: UUID) -> IssueOutcome:
        return IssueOutcome(acceptance=ProviderAcceptance.ACCEPTED)

    def query_status(self, *, client_ref: UUID) -> ProviderStatus:
        """Luôn "chưa biết".

        Đường `needs_reconcile` không tới được adapter này — `issue` của nó
        không có bước mạng nào để mất tín hiệu — nên câu trả lời trung thực là
        không biết gì về `client_ref`, chứ không phải một `ACCEPTED` bịa ra.
        Bịa sẽ biến một dòng lẽ ra không tồn tại thành `done` mà không ai gửi gì.
        """
        return ProviderStatus(known=False)


PROVIDERS.register(INTERNAL_PROVIDER_CODE, InternalProvider())
