"""Hợp đồng với nhà cung cấp hóa đơn điện tử (FR-EIV-001/042, LD-10).

**Phát hành là HAI chặng, không phải một** — và đó là phát hiện đắt nhất của lát
7E-2. Nhà cung cấp thật (EasyInvoice) nạp bản nháp ở lời gọi thứ nhất rồi mới ký
và gửi cơ quan thuế ở lời gọi thứ hai, và **khóa chống trùng là thứ lời gọi thứ
nhất sinh ra**, không phải thứ ta gửi đi. Bản tích hợp đang chạy thật ở
`~/code/beta.konek.vn` nói thẳng điều đó: nó vứt khóa cục bộ (`_local_ikey`),
cất `server_ikeys[0]`, và mọi lượt tra cứu về sau đều dùng khóa ấy.

Hệ quả với thiết kế: khóa của nhà cung cấp **phải được ghi bền trước** lời gọi
thứ hai. Nếu không, một lượt rollback sau khi phát hành thành công sẽ xóa mất
khóa, và lượt sau nạp một bản nháp MỚI rồi phát hành nó — hai tờ hóa đơn thật
với cơ quan thuế, không thao tác nào trong phần mềm gỡ được. Vì thân job không
tự commit (xem `JobContext`), hai chặng phải là **hai lượt job**.

Bốn phương thức, và bộ ấy là tối thiểu không cắt được nữa:

* `prepare` — nạp tờ hóa đơn, **chưa** phát hành; trả khóa của nhà cung cấp;
* `issue` — phát hành tờ đã nạp, theo **khóa của họ**;
* `query_status` — họ đang giữ gì cho khóa ấy. Đây là câu trả lời duy nhất giải
  được một lượt mất tín hiệu, và nó phải phân biệt *bản nháp* với *đã phát
  hành*: đọc một bản nháp chưa ký thành "cơ quan thuế đã nhận" là bỏ rơi đúng
  tờ hóa đơn mà `needs_reconcile` sinh ra để dọn.

Gửi cho người mua, hủy, thay thế, điều chỉnh **chưa khai ở đây**: chúng thuộc
lát sau, và một phương thức Protocol không ai gọi là một phương thức mọi bản cài
phải viết `raise NotImplementedError` để thỏa mãn.

**Ba loại kết quả, không phải hai.** `IssueOutcome` phân biệt *nhận* / *từ chối*
/ *không rõ*; đường thứ ba là toàn bộ lý do `OutboxStatus.NEEDS_RECONCILE` tồn
tại. Adapter nào gộp "không rõ" vào "từ chối" — hoặc tệ hơn, vào "chưa nhận" —
sẽ làm hệ thống phát hành lại một tờ hóa đơn nhà cung cấp đã cấp số.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.security.keystore import SecretBox


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
    invoice_no: str | None = None
    """Số hóa đơn **do nhà cung cấp cấp** (quyết định user 2026-09-08).

    `None` cho adapter không cấp số — `internal`, và mọi ký hiệu hóa đơn đặt in
    hay tự in, nơi dãy `gap_free` cục bộ đã cấp số từ lúc phát hành. Nơi gọi
    điền nó vào tờ hóa đơn **một lần**: trigger `einvoices_immutable_after_issue`
    cho `NULL → giá trị` và chặn mọi lượt đổi sau đó."""
    tax_authority_code: str | None = None
    lookup_code: str | None = None
    message: str | None = None
    """Lý do, dùng cho cả `REJECTED` (giải trình cho người dùng) lẫn `UNKNOWN`
    (dấu vết cho người vận hành)."""


@dataclass(frozen=True)
class PrepareOutcome:
    """Kết quả chặng nạp — tờ hóa đơn đã lên nhà cung cấp nhưng **chưa** phát hành."""

    acceptance: ProviderAcceptance
    provider_ref: str | None = None
    """Khóa của **nhà cung cấp**, và là thứ duy nhất tra cứu được về sau.

    `None` khi không phải `ACCEPTED`. Một lượt `ACCEPTED` mà thiếu khóa là mâu
    thuẫn: nơi gọi coi nó là "không rõ" thay vì đi tiếp, vì đi tiếp nghĩa là
    phát hành một tờ hóa đơn ta không còn cách nào tra lại."""

    message: str | None = None


class ProviderRecordState(StrEnum):
    """Nhà cung cấp đang giữ tờ hóa đơn ở chặng nào.

    Ba giá trị, và ranh giới giữa hai giá trị đầu là chỗ dễ đọc sai nhất: một
    **bản nháp** đã nạp thì nhà cung cấp *biết* tới nó, nhưng cơ quan thuế thì
    chưa. Gộp nó vào `ISSUED` là bỏ rơi tờ hóa đơn ở đúng lượt đáng lẽ phải
    phát hành nó.
    """

    UNKNOWN = "unknown"
    """Nhà cung cấp không biết khóa này — chưa lượt nạp nào tới nơi."""
    PREPARED = "prepared"
    """Đã nạp, **chưa** phát hành. Đường đi tiếp là `issue`, không phải `prepare`."""
    ISSUED = "issued"
    """Đã ký và đã gửi cơ quan thuế. Không gửi gì thêm."""


@dataclass(frozen=True)
class ProviderStatus:
    """Kết quả tra cứu theo khóa của **nhà cung cấp**.

    Không có `known: bool` nữa: câu hỏi thật có ba câu trả lời, và một `bool`
    ép hai trong số đó thành một — đúng chỗ lát 7E-2 đã đọc sai lần đầu.
    """

    state: ProviderRecordState
    outcome: IssueOutcome | None = None
    """Kết quả họ đang giữ, khi `ISSUED`."""


class EInvoiceProvider(Protocol):
    """Adapter một nhà cung cấp. Đăng ký theo `provider_code` — xem `registry`."""

    def prepare(self, *, client_ref: UUID, invoice_id: UUID) -> PrepareOutcome:
        """Nạp tờ hóa đơn lên nhà cung cấp, **chưa** phát hành.

        `client_ref` là khóa của **ta**, đi vào thân chứng từ để nhà cung cấp có
        một mốc đối chiếu ổn định. Khóa trả về mới là thứ dùng cho hai phương
        thức còn lại — xem docstring đầu tệp về vì sao hai thứ ấy khác nhau.
        """
        ...

    def issue(self, *, provider_ref: str, invoice_id: UUID) -> IssueOutcome:
        """Phát hành tờ đã nạp: ký số và gửi cơ quan thuế.

        Chỉ được gọi với `provider_ref` **đã ghi bền**. Gọi nó với một khóa vừa
        nhận trong cùng transaction chưa commit là mở lại đúng đường dẫn tới hai
        tờ hóa đơn thật.
        """
        ...

    def query_status(self, *, provider_ref: str) -> ProviderStatus:
        """Nhà cung cấp đang giữ tờ hóa đơn này ở chặng nào."""
        ...


@dataclass(frozen=True)
class ProviderBinding:
    """Mọi thứ một adapter cần để dựng chính nó, cho **một** dữ liệu kế toán.

    Adapter không thể là một thể hiện đăng ký sẵn lúc import như phác thảo đầu:
    thông tin đăng nhập nhà cung cấp nằm ở DB, khác nhau theo từng dữ liệu kế
    toán, và mật khẩu thì đã mã hóa. Nên registry giữ **nhà máy**, và nhà máy
    nhận cái này.

    `secret_box` có thể `None`: bản cài chưa cấu hình khóa mã hóa ứng dụng
    (ADR-019). Adapter nào cần nó thì tự từ chối bằng một lỗi nghiệp vụ nói
    đúng thứ phải cấu hình — adapter `internal` không cần, và đó là lý do
    trường này không bắt buộc.
    """

    session: Session
    secret_box: SecretBox | None = None


ProviderFactory = Callable[[ProviderBinding], EInvoiceProvider]
"""Nhà máy dựng adapter cho một lượt dùng. Xem `ProviderBinding`."""
