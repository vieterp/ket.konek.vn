"""Kết luận một lượt gửi — và luật "hỏi trước khi làm lại" (RT-10).

Một câu giữ cho hệ thống không bao giờ phát hành hai lần:

    Không bao giờ gọi `issue` với một khóa nhà cung cấp chưa ghi bền, và không
    bao giờ gọi lại nó khi chưa hỏi `query_status`.

Kịch bản hỏng mà nó chặn: worker phát hành, nhà cung cấp ký và gửi cơ quan thuế,
câu trả lời rơi mất trên đường về, worker lùi lịch rồi làm lại — cơ quan thuế
thấy hai tờ hóa đơn cho một lần bán hàng, và không thao tác nào trong phần mềm
gỡ được.

## Hai chặng, hai lượt job

Nhà cung cấp thật nạp bản nháp ở lời gọi thứ nhất rồi mới ký ở lời gọi thứ hai,
và **khóa chống trùng do lời gọi thứ nhất sinh ra** (xem `providers/contracts`).
Khóa ấy phải commit trước lời gọi thứ hai; thân job thì không tự commit được.
Nên:

* lượt job thứ nhất `prepare`, ghi `provider_ref`, rồi **xếp một lượt tiếp**;
* lượt thứ hai đọc `provider_ref` đã commit và `issue`.

Điều đó làm mọi lượt hỏng trở nên an toàn, theo đúng hai chiều:

* hỏng ở chặng nạp → `provider_ref` chưa có → lượt sau nạp lại. Sinh ra một bản
  nháp mồ côi phía nhà cung cấp, **không** phải một tờ hóa đơn — bản nháp chưa
  ký thì chưa tồn tại với cơ quan thuế;
* hỏng ở chặng phát hành → `provider_ref` **vẫn còn** (đã commit ở lượt trước) →
  lượt sau hỏi `query_status` và thấy nó đã phát hành, nên không phát hành lại.

## Ba chặng của câu trả lời tra cứu

`query_status` phải phân biệt *không biết* / *bản nháp* / *đã phát hành*. Bản
nháp là chặng dễ đọc sai nhất: nhà cung cấp **biết** tới nó, nhưng cơ quan thuế
thì chưa — đọc nó thành "đã nhận" là bỏ rơi đúng tờ hóa đơn mà cả đường
`needs_reconcile` sinh ra để dọn.

**Không ngoại lệ nào của adapter được thoát ra khỏi tệp này.** Thư viện HTTP báo
hết thời gian chờ bằng cách **ném**, và một lượt hết giờ xảy ra *sau* khi yêu
cầu tới nơi cũng thường như trước — nên ném là `UNKNOWN`, không phải `REJECTED`,
và tuyệt đối không phải "họ chưa nhận".
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
    PrepareOutcome,
    ProviderAcceptance,
    ProviderRecordState,
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
    prepared: bool
    """Lượt này chỉ **nạp** xong. Dòng cần thêm một lượt nữa để phát hành, và
    `outbox_job` đọc trường này để xếp nó."""
    issued: bool
    """Lượt này có thật sự gọi `issue` không."""


def transmit(session: Session, claimed: ClaimedRow, provider: EInvoiceProvider) -> TransmitReport:
    """Đưa một dòng đã giành tiến thêm **đúng một chặng**."""
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
        return _report(row, asked_first=False, prepared=False, issued=False)

    provider_ref = row.provider_ref
    asked_first = claimed.must_reconcile and provider_ref is not None
    if asked_first and provider_ref is not None:
        settled = _reconcile(session, row, provider, provider_ref=provider_ref)
        if settled is not None:
            return settled

    if provider_ref is None:
        return _prepare(row, provider, asked_first=asked_first)
    return _issue(session, row, provider, provider_ref=provider_ref, asked_first=asked_first)


def _reconcile(
    session: Session, row: EInvoiceOutbox, provider: EInvoiceProvider, *, provider_ref: str
) -> TransmitReport | None:
    """Hỏi nhà cung cấp trước khi làm lại. `None` = còn phải đi tiếp.

    Ba nhánh, đúng ba chặng mà `ProviderRecordState` phân biệt. Nhánh `PREPARED`
    **không** kết thúc lượt: bản nháp đã có nên bỏ qua chặng nạp, nhưng tờ hóa
    đơn vẫn chưa tới cơ quan thuế và vẫn phải phát hành.
    """
    status = _ask(provider, provider_ref)
    if status.state == ProviderRecordState.ISSUED:
        outcome = status.outcome or IssueOutcome(acceptance=ProviderAcceptance.ACCEPTED)
        _apply(session, row, outcome)
        return _report(row, asked_first=True, prepared=False, issued=False)
    if status.state == ProviderRecordState.UNKNOWN:
        # Ta giữ khóa của họ mà họ không biết nó: một mâu thuẫn thật sự — khóa
        # sai, hồ sơ trỏ sang tài khoản khác, hoặc bản nháp đã bị xóa phía họ.
        # Nạp lại một bản nháp mới ở đây là đoán, nên dừng và để người xử lý.
        mark_needs_reconcile(
            row,
            message="Nhà cung cấp không nhận ra khóa hóa đơn đã cấp — cần kiểm tra thủ công",
        )
        return _report(row, asked_first=True, prepared=False, issued=False)
    return None


def _prepare(
    row: EInvoiceOutbox, provider: EInvoiceProvider, *, asked_first: bool
) -> TransmitReport:
    """Chặng một: nạp bản nháp và **cất khóa của nhà cung cấp**."""
    outcome = _run_prepare(provider, client_ref=row.client_ref, invoice_id=row.einvoice_id)
    if outcome.acceptance == ProviderAcceptance.REJECTED:
        mark_failed(row, message=outcome.message or "Nhà cung cấp từ chối hóa đơn")
        return _report(row, asked_first=asked_first, prepared=False, issued=False)
    if outcome.acceptance != ProviderAcceptance.ACCEPTED or not outcome.provider_ref:
        # `ACCEPTED` mà không có khóa là mâu thuẫn, và đi tiếp nghĩa là phát hành
        # một tờ hóa đơn ta không còn cách nào tra lại. Coi là không rõ.
        mark_needs_reconcile(
            row, message=outcome.message or "Nhà cung cấp không trả về khóa hóa đơn"
        )
        return _report(row, asked_first=asked_first, prepared=False, issued=False)

    row.provider_ref = outcome.provider_ref
    # Dòng ở lại `in_flight` với lease vừa làm mới; `outbox_job` xếp lượt tiếp
    # trong **cùng transaction** này, nên khóa và lượt dùng nó cùng commit.
    return _report(row, asked_first=asked_first, prepared=True, issued=False)


def _issue(
    session: Session,
    row: EInvoiceOutbox,
    provider: EInvoiceProvider,
    *,
    provider_ref: str,
    asked_first: bool,
) -> TransmitReport:
    """Chặng hai: phát hành tờ đã nạp, theo khóa **đã ghi bền** của nhà cung cấp."""
    outcome = _run_issue(provider, provider_ref=provider_ref, invoice_id=row.einvoice_id)
    _apply(session, row, outcome)
    return _report(row, asked_first=asked_first, prepared=False, issued=True)


def _report(
    row: EInvoiceOutbox, *, asked_first: bool, prepared: bool, issued: bool
) -> TransmitReport:
    return TransmitReport(
        outbox_id=row.id,
        status=OutboxStatus(row.status),
        asked_first=asked_first,
        prepared=prepared,
        issued=issued,
    )


def _ask(provider: EInvoiceProvider, provider_ref: str) -> ProviderStatus:
    """`query_status`, và một lượt hỏng **không** được coi là "chưa nhận".

    Trả `UNKNOWN` khi adapter ném sẽ đẩy dòng sang nhánh nạp lại rồi phát hành
    lại — đúng thứ luật RT-10 cấm. Không biết thì phải nói là không biết, và ở
    đây "không biết" nghĩa là để dòng nằm lại `needs_reconcile`.
    """
    try:
        return provider.query_status(provider_ref=provider_ref)
    except Exception as error:  # Bắt rộng có chủ đích — xem docstring đầu tệp
        return ProviderStatus(
            state=ProviderRecordState.ISSUED,
            outcome=_unknown(f"Không tra cứu được trạng thái: {error}"),
        )


def _run_prepare(
    provider: EInvoiceProvider, *, client_ref: UUID, invoice_id: UUID
) -> PrepareOutcome:
    try:
        return provider.prepare(client_ref=client_ref, invoice_id=invoice_id)
    except Exception as error:  # Bắt rộng có chủ đích — xem docstring đầu tệp
        return PrepareOutcome(
            acceptance=ProviderAcceptance.UNKNOWN,
            message=f"Lỗi khi nạp hóa đơn lên nhà cung cấp: {error}",
        )


def _run_issue(
    provider: EInvoiceProvider, *, provider_ref: str, invoice_id: UUID
) -> IssueOutcome:
    """`issue`, và một lượt ném là `UNKNOWN` chứ không phải `REJECTED`.

    Ngoại lệ của thư viện HTTP không nói được nhà cung cấp đã phát hành hay
    chưa. Đánh nó thành từ chối là kết luận một điều không ai biết.
    """
    try:
        return provider.issue(provider_ref=provider_ref, invoice_id=invoice_id)
    except Exception as error:  # Bắt rộng có chủ đích — xem docstring đầu tệp
        return _unknown(f"Lỗi khi phát hành tại nhà cung cấp: {error}")


def _unknown(message: str) -> IssueOutcome:
    return IssueOutcome(acceptance=ProviderAcceptance.UNKNOWN, message=message)


def _apply(session: Session, row: EInvoiceOutbox, outcome: IssueOutcome) -> None:
    """Ghi kết quả lên **cả** dòng hàng đợi và tờ hóa đơn.

    Hai vế đi cùng một transaction, không tách được: một dòng `done` cạnh tờ hóa
    đơn còn `DANG_PHAT_HANH` là trạng thái không ai đọc đúng được — panel hàng
    đợi nói xong, màn danh sách hóa đơn nói đang chờ.
    """
    service = EInvoiceService(session)
    if outcome.acceptance == ProviderAcceptance.ACCEPTED:
        if not _number_is_settled(session, row, outcome):
            # Nhà cung cấp bảo đã nhận nhưng ta không đọc ra số của tờ hóa đơn.
            # Không đánh `done`: ràng buộc `issued_invoice_has_a_number` sẽ chặn
            # lượt xác nhận, và một ngoại lệ ở đây kéo cả lô rollback. Để dòng
            # lại chờ một lượt tra cứu — đúng nghĩa "chưa biết đủ".
            mark_needs_reconcile(
                row, message="Nhà cung cấp đã nhận nhưng chưa trả về số hóa đơn"
            )
            return
        mark_done(row, provider_ref=outcome.provider_ref)
        # `transmit` đã bảo đảm hóa đơn còn ở `DANG_PHAT_HANH`, nên cạnh này
        # luôn hợp lệ. Không thêm phép kiểm phòng thủ: nó sẽ nuốt đúng thứ đáng
        # phải nổ nếu bất biến ấy vỡ.
        service.confirm(
            row.einvoice_id,
            tax_authority_code=outcome.tax_authority_code,
            lookup_code=outcome.lookup_code,
            invoice_no=outcome.invoice_no,
        )
        return
    if outcome.acceptance == ProviderAcceptance.REJECTED:
        message = outcome.message or "Nhà cung cấp từ chối hóa đơn"
        mark_failed(row, message=message)
        service.reject(row.einvoice_id, message=message)
        return
    mark_needs_reconcile(row, message=outcome.message or "Không rõ kết quả từ nhà cung cấp")


def _number_is_settled(session: Session, row: EInvoiceOutbox, outcome: IssueOutcome) -> bool:
    """Tờ hóa đơn sẽ có số sau lượt xác nhận này chưa.

    Hai đường hợp lệ: số đã cấp cục bộ từ lúc xếp hàng (hóa đơn đặt in, tự in,
    `internal`), hoặc nhà cung cấp vừa trả số về. Không đường nào thì tờ hóa đơn
    sẽ vi phạm `issued_invoice_has_a_number` — bắt ở đây, trước khi nó thành một
    lỗi ràng buộc kéo đổ cả lô.
    """
    invoice = EInvoiceService(session).require(row.einvoice_id)
    return invoice.invoice_no is not None or bool(outcome.invoice_no)
