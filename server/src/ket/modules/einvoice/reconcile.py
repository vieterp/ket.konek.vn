"""Kết luận một lượt gửi — và luật "hỏi trước khi gửi lại" (RT-10).

Một câu duy nhất giữ cho hệ thống không bao giờ phát hành hai lần:

    Dòng nào đã từng được gửi đi thì phải `query_status(client_ref)` **trước**
    mọi lượt gửi kế tiếp.

Không có nó, kịch bản hỏng đi như sau: worker gửi tờ hóa đơn, provider nhận và
cấp mã cơ quan thuế, câu trả lời rơi mất trên đường về, worker lùi lịch rồi gửi
lại — provider thấy một tờ hóa đơn thứ hai. Với cơ quan thuế thì đó là hai tờ
hóa đơn cho một lần bán hàng, và không thao tác nào trong phần mềm gỡ được.

`client_ref` là thứ làm câu hỏi ấy trả lời được: nó cố định trong suốt đời dòng
outbox, nên provider tra ra đúng lượt gửi cũ chứ không phải "có tờ nào giống
giống không".

**Câu trả lời "có phải hỏi trước không" tính ở lượt giành, không tính ở đây.**
Nó đọc lease, mà lượt giành thì làm mới lease — nên tính sau là tính trên dấu
vết đã bị xóa. `ClaimedRow` mang sẵn câu trả lời tới; xem §"Hai đường vào"
trong `outbox.py`.

**Không ngoại lệ nào của adapter được thoát ra khỏi tệp này.** Một adapter thật
gọi HTTP, và thư viện HTTP báo hết thời gian chờ bằng cách **ném** chứ không
bằng một giá trị trả về. Để nó bay lên thân job là kéo cả lô rollback — kể cả
những dòng đã gửi thật — rồi lượt sau giành đúng tập ấy và gửi lại. Nên mọi
lượt gọi provider đi qua `_ask` / `_send`: hỏng không rõ nguyên nhân **là**
`UNKNOWN`, đúng nghĩa của nó, và dòng đi về `needs_reconcile` thay vì làm hỏng
lượt chạy.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from ket.modules.einvoice.models import EInvoiceOutbox, EInvoiceStatus, OutboxStatus
from ket.modules.einvoice.outbox import (
    ClaimedRow,
    mark_done,
    mark_failed,
    mark_needs_reconcile,
    mark_superseded,
)
from ket.modules.einvoice.providers.contracts import (
    EInvoiceProvider,
    IssueOutcome,
    ProviderAcceptance,
    ProviderStatus,
)
from ket.modules.einvoice.service import EInvoiceService


@dataclass(frozen=True)
class TransmitReport:
    """Chuyện gì đã xảy ra với một dòng — cho log của worker và cho test."""

    outbox_id: int
    status: OutboxStatus
    asked_first: bool
    """Có đi qua `query_status` trước không. Test của luật RT-10 khẳng định trên
    chính trường này, vì "không gửi lần hai" là thứ khó chứng minh bằng cách
    nhìn kết quả cuối."""
    sent: bool
    """Có thật sự gọi `issue` lượt này không."""


def transmit(session: Session, claimed: ClaimedRow, provider: EInvoiceProvider) -> TransmitReport:
    """Đưa một dòng đã giành tới kết luận.

    Ba nhánh, đúng ba câu trả lời mà `ProviderAcceptance` phân biệt. Nhánh
    `UNKNOWN` **không** là lỗi: nó trả dòng về hàng đợi ở chặng
    `needs_reconcile` để lượt sau hỏi lại, và tờ hóa đơn giữ nguyên số cùng
    trạng thái `DANG_PHAT_HANH` — đúng nghĩa "chưa biết".
    """
    row = claimed.row
    service = EInvoiceService(session)
    invoice = service.require(row.einvoice_id)
    if EInvoiceStatus(invoice.status) != EInvoiceStatus.DANG_PHAT_HANH:
        # Tờ hóa đơn đã rời hàng đợi bằng đường khác (xác nhận / từ chối tay).
        # Gửi nó đi lúc này là đường ngắn nhất tới tờ hóa đơn thứ hai — xem
        # `outbox.mark_superseded`.
        mark_superseded(
            row, message="Hóa đơn đã rời trạng thái đang phát hành trước khi hàng đợi gửi đi"
        )
        return TransmitReport(
            outbox_id=row.id, status=OutboxStatus(row.status), asked_first=False, sent=False
        )

    asked_first = claimed.must_reconcile
    if asked_first:
        known = _ask(provider, row.client_ref)
        if known.known:
            # Provider đã cầm tờ hóa đơn này. Không gửi gì nữa — kết luận bằng
            # chính kết quả họ đang giữ.
            outcome = known.outcome or IssueOutcome(acceptance=ProviderAcceptance.ACCEPTED)
            _apply(session, row, outcome)
            return TransmitReport(
                outbox_id=row.id, status=OutboxStatus(row.status), asked_first=True, sent=False
            )

    outcome = _send(provider, client_ref=row.client_ref, invoice_id=row.einvoice_id)
    _apply(session, row, outcome)
    return TransmitReport(
        outbox_id=row.id, status=OutboxStatus(row.status), asked_first=asked_first, sent=True
    )


def _ask(provider: EInvoiceProvider, client_ref: UUID) -> ProviderStatus:
    """`query_status`, và một lượt hỏng **không** được coi là "chưa nhận".

    Trả `known=False` khi adapter ném sẽ đẩy dòng thẳng sang nhánh gửi lại —
    đúng thứ luật RT-10 cấm. Không biết thì phải nói là không biết.
    """
    try:
        return provider.query_status(client_ref=client_ref)
    except Exception as error:  # Bắt rộng có chủ đích — xem docstring đầu tệp
        return ProviderStatus(known=True, outcome=_unknown(f"Không tra cứu được: {error}"))


def _send(provider: EInvoiceProvider, *, client_ref: UUID, invoice_id: UUID) -> IssueOutcome:
    """`issue`, và một lượt ném là `UNKNOWN` chứ không phải `REJECTED`.

    Ngoại lệ của thư viện HTTP không nói được nhà cung cấp đã nhận hay chưa —
    hết thời gian chờ xảy ra **sau** khi yêu cầu đã tới nơi cũng thường như
    trước. Đánh nó thành từ chối là kết luận một điều không ai biết.
    """
    try:
        return provider.issue(client_ref=client_ref, invoice_id=invoice_id)
    except Exception as error:  # Bắt rộng có chủ đích — xem docstring đầu tệp
        return _unknown(f"Lỗi khi gửi tới nhà cung cấp: {error}")


def _unknown(message: str) -> IssueOutcome:
    return IssueOutcome(acceptance=ProviderAcceptance.UNKNOWN, message=message)


def _apply(session: Session, row: EInvoiceOutbox, outcome: IssueOutcome) -> None:
    """Ghi kết quả lên **cả** dòng hàng đợi và tờ hóa đơn.

    Hai vế đi cùng một transaction, không tách được: một dòng `done` cạnh tờ hóa
    đơn còn `DANG_PHAT_HANH` là trạng thái không ai đọc đúng được — panel hàng
    đợi nói xong, màn danh sách hóa đơn nói đang chờ.
    """
    service = EInvoiceService(session)
    # `transmit` đã bảo đảm hóa đơn còn ở `DANG_PHAT_HANH`, nên hai cạnh dưới
    # đây luôn hợp lệ. Không thêm phép kiểm phòng thủ nào ở đây: nó sẽ nuốt mất
    # đúng thứ đáng phải nổ nếu bất biến ấy vỡ.
    if outcome.acceptance == ProviderAcceptance.ACCEPTED:
        mark_done(row, provider_ref=outcome.provider_ref)
        service.confirm(
            row.einvoice_id,
            tax_authority_code=outcome.tax_authority_code,
            lookup_code=outcome.lookup_code,
        )
        return
    if outcome.acceptance == ProviderAcceptance.REJECTED:
        message = outcome.message or "Nhà cung cấp từ chối hóa đơn"
        mark_failed(row, message=message)
        service.reject(row.einvoice_id, message=message)
        return
    # UNKNOWN: tờ hóa đơn **không** đổi trạng thái. Nó vẫn đang phát hành, vì
    # đó chính xác là điều ta biết.
    mark_needs_reconcile(row, message=outcome.message or "Không rõ kết quả từ nhà cung cấp")
