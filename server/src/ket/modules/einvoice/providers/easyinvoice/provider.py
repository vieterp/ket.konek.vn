"""Adapter EasyInvoice — nối ba lời gọi HTTP vào Protocol `EInvoiceProvider`.

**`Ikey` là của MÁY CHỦ, không phải của ta.** `importInvoice` trả về một mảng
`Ikeys` do họ sinh, và đó là khóa duy nhất tra cứu được ở `issueInvoices` cùng
`getInvoicesByIkeys`. Khóa ta gắn trong thân XML chỉ là mốc đối chiếu. Bản tích
hợp đang chạy thật ở `~/code/beta.konek.vn` nói thẳng điều đó: nó vứt khóa cục
bộ, cất `server_ikeys[0]`, và coi `Ikeys` rỗng là lỗi chí tử. Bản đầu của lát
này giả định ngược lại — đó là lỗi nghiêm trọng nhất mà review bắt được, vì nó
dẫn thẳng tới hai tờ hóa đơn thật với cơ quan thuế.

Hai chặng vì thế **không gộp được**: khóa của họ phải ghi bền giữa hai lời gọi.
Xem `reconcile` về cách hai chặng thành hai lượt job.

**Nhà cung cấp cấp số** (quyết định user 2026-09-08): số về trong `KeyInvoiceNo`
của lượt phát hành, khóa theo **khóa của họ**, và `IssueOutcome.invoice_no` chở
nó ngược lên `reconcile`.

**`InvoiceStatus` phân biệt bản nháp với hóa đơn đã phát hành** — `0` chưa ký,
`1` đã ký, `2` cơ quan thuế đã cấp mã, `3`/`4`/`5` là các chặng sau. Đọc một bản
nháp thành "đã phát hành" là bỏ rơi tờ hóa đơn ở đúng lượt đáng lẽ phát hành nó.
"""

from __future__ import annotations

from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import EInvoiceProviderNotConfiguredError
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.protocols import PROVIDERS as CROSS_MODULE
from ket.kernel.security.keystore import SecretBox
from ket.modules.einvoice.models import EInvoice, EInvoiceProviderProfile
from ket.modules.einvoice.providers.contracts import (
    IssueOutcome,
    PrepareOutcome,
    ProviderAcceptance,
    ProviderBinding,
    ProviderRecordState,
    ProviderStatus,
)
from ket.modules.einvoice.providers.easyinvoice.client import (
    EasyInvoiceClient,
    EasyInvoiceCredentials,
    EasyInvoiceRefusedError,
)
from ket.modules.einvoice.providers.easyinvoice.xml_builder import build_invoice_xml
from ket.modules.einvoice.providers.registry import PROVIDERS

EASYINVOICE_PROVIDER_CODE: Final[str] = "easyinvoice"


class EasyInvoiceProvider:
    """Bản cài `EInvoiceProvider` cho EasyInvoice (SoftDreams)."""

    def __init__(self, session: Session, client: EasyInvoiceClient) -> None:
        self._session = session
        self._client = client

    def prepare(self, *, client_ref: UUID, invoice_id: UUID) -> PrepareOutcome:
        """`importInvoice` — nạp bản XML chưa ký, lấy **khóa của họ**."""
        invoice = self._require_invoice(invoice_id)
        form = self._require_form(invoice.invoice_form_id)
        xml = self._build_xml(invoice, client_ref=client_ref)
        try:
            ikeys = self._client.import_invoice(xml, pattern=form.code, serial=form.form_no or "")
        except EasyInvoiceRefusedError as error:
            return PrepareOutcome(acceptance=ProviderAcceptance.REJECTED, message=error.message)
        if not ikeys:
            # Mảng rỗng là lỗi chí tử, không phải "thành công không có khóa":
            # bản nháp có thể đã tồn tại phía họ mà ta không cách nào trỏ tới.
            # `reconcile` đọc `UNKNOWN` thành `needs_reconcile` và dừng lại.
            return PrepareOutcome(
                acceptance=ProviderAcceptance.UNKNOWN,
                message="EasyInvoice không trả về Ikeys cho lượt nạp hóa đơn",
            )
        return PrepareOutcome(acceptance=ProviderAcceptance.ACCEPTED, provider_ref=ikeys[0])

    def issue(self, *, provider_ref: str, invoice_id: UUID) -> IssueOutcome:
        """`issueInvoices` — ký số và gửi cơ quan thuế, theo khóa của họ."""
        invoice = self._require_invoice(invoice_id)
        form = self._require_form(invoice.invoice_form_id)
        try:
            issued = self._client.issue_invoices(
                [provider_ref], pattern=form.code, serial=form.form_no or ""
            )
        except EasyInvoiceRefusedError as error:
            return IssueOutcome(acceptance=ProviderAcceptance.REJECTED, message=error.message)
        return _accepted(issued, provider_ref=provider_ref)

    def query_status(self, *, provider_ref: str) -> ProviderStatus:
        """`getInvoicesByIkeys` — và **chỉ** một mảng rỗng mới là "họ không biết".

        Bản đầu của lát này đọc *mọi* lượt từ chối thành "chưa nhận", tức sai mật
        khẩu hay máy chủ quá tải cũng dẫn thẳng tới nhánh phát hành lại. Nay lượt
        từ chối ném ra ngoài để `reconcile` đọc thành "không biết".
        """
        records = self._client.get_invoices_by_ikeys([provider_ref])
        if not records:
            return ProviderStatus(state=ProviderRecordState.UNKNOWN)
        record = records[0]
        if _is_issued(record):
            return ProviderStatus(
                state=ProviderRecordState.ISSUED,
                outcome=_from_record(record, provider_ref=provider_ref),
            )
        return ProviderStatus(state=ProviderRecordState.PREPARED)

    def _build_xml(self, invoice: EInvoice, *, client_ref: UUID) -> str:
        document = None
        for source in CROSS_MODULE.einvoice_sources():
            document = source.read(self._session, voucher_id=invoice.source_voucher_id)
            if document is not None:
                break
        if document is None:
            raise EInvoiceProviderNotConfiguredError(
                "Không đọc được nội dung chứng từ gốc của hóa đơn — phân hệ sở hữu "
                "loại chứng từ này chưa khai nguồn hóa đơn điện tử",
                provider_code=EASYINVOICE_PROVIDER_CODE,
            )
        partner = self._session.get(Partner, document.partner_id)
        if partner is None:
            raise EInvoiceProviderNotConfiguredError(
                "Không tìm thấy đối tác của chứng từ gốc",
                provider_code=EASYINVOICE_PROVIDER_CODE,
            )
        return build_invoice_xml(document, partner, client_ref=client_ref)

    def _require_invoice(self, invoice_id: UUID) -> EInvoice:
        invoice = self._session.get(EInvoice, invoice_id)
        if invoice is None:
            raise EInvoiceProviderNotConfiguredError(
                "Hóa đơn không còn tồn tại", provider_code=EASYINVOICE_PROVIDER_CODE
            )
        return invoice

    def _require_form(self, invoice_form_id: int) -> InvoiceForm:
        form = self._session.get(InvoiceForm, invoice_form_id)
        if form is None:
            raise EInvoiceProviderNotConfiguredError(
                "Ký hiệu hóa đơn không còn tồn tại",
                provider_code=EASYINVOICE_PROVIDER_CODE,
            )
        return form


ISSUED_STATUSES = frozenset({1, 2, 3, 4, 5})
"""`InvoiceStatus` nghĩa là tờ hóa đơn **đã ký và rời phần mềm**.

`0` (nháp chưa ký) cố ý nằm ngoài: nhà cung cấp biết tới nó, nhưng cơ quan thuế
thì chưa, nên nó là chặng `PREPARED` chứ không phải `ISSUED`. Ba giá trị cuối
(thay thế / điều chỉnh / hủy) vẫn tính là đã phát hành — chúng là các chặng
**sau** khi phát hành, và với hàng đợi thì việc đã xong."""


def _is_issued(record: dict[str, Any]) -> bool:
    """Bản ghi tra ra đã phát hành chưa.

    Thiếu trường `InvoiceStatus` thì trả `False`: một tờ hóa đơn có số hẳn hoi
    nhưng không nói được chặng nào vẫn an toàn hơn khi coi là bản nháp — lượt sau
    chỉ phát hành lại theo đúng khóa cũ, còn đọc nhầm chiều kia là bỏ rơi nó."""
    status = record.get("InvoiceStatus")
    return isinstance(status, int) and status in ISSUED_STATUSES


def _accepted(payload: dict[str, Any], *, provider_ref: str) -> IssueOutcome:
    """Đọc số hóa đơn và mã cơ quan thuế từ câu trả lời của `issueInvoices`.

    Số khóa theo **khóa của nhà cung cấp**: `issueInvoices` nhận một danh sách,
    nên câu trả lời có thể mang số của tờ khác, và lấy nhầm là gán cho tờ này
    một số thuộc về tờ kia — rồi trigger bất biến đóng băng đúng con số sai ấy.
    """
    numbers = payload.get("KeyInvoiceNo")
    invoice_no = None
    if isinstance(numbers, dict):
        raw = numbers.get(provider_ref)
        invoice_no = raw if isinstance(raw, str) and raw else None
    invoices = payload.get("Invoices")
    record = invoices[0] if isinstance(invoices, list) and invoices else {}
    return IssueOutcome(
        acceptance=ProviderAcceptance.ACCEPTED,
        provider_ref=provider_ref,
        invoice_no=invoice_no,
        tax_authority_code=_text(record, "TaxAuthorityCode") or _text(record, "ReservationCode"),
        lookup_code=_text(record, "LookupCode"),
    )


def _from_record(record: dict[str, Any], *, provider_ref: str) -> IssueOutcome:
    """Kết quả nhà cung cấp đang giữ cho một `Ikey` đã tra ra."""
    return IssueOutcome(
        acceptance=ProviderAcceptance.ACCEPTED,
        provider_ref=provider_ref,
        invoice_no=_text(record, "InvoiceNo"),
        tax_authority_code=_text(record, "TaxAuthorityCode") or _text(record, "ReservationCode"),
        lookup_code=_text(record, "LookupCode"),
    )


def _text(record: dict[str, Any], key: str) -> str | None:
    value = record.get(key)
    return value if isinstance(value, str) and value else None


def build(binding: ProviderBinding) -> EasyInvoiceProvider:
    """Dựng adapter cho một dữ liệu kế toán: đọc hồ sơ, giải mã mật khẩu.

    Từ chối **rõ ràng** ở hai chỗ người vận hành sửa được: chưa khai hồ sơ đăng
    nhập, và bản cài chưa có khóa mã hóa ứng dụng (ADR-019). Cả hai đi ra dưới
    dạng lỗi nghiệp vụ nên chúng dừng lại ở `last_error` của một dòng hàng đợi
    thay vì làm hỏng cả lượt bơm — xem `outbox_job`.
    """
    profile = binding.session.scalars(
        select(EInvoiceProviderProfile).where(
            EInvoiceProviderProfile.provider_code == EASYINVOICE_PROVIDER_CODE,
            EInvoiceProviderProfile.is_active.is_(True),
        )
    ).one_or_none()
    if profile is None:
        raise EInvoiceProviderNotConfiguredError(
            "Chưa khai hồ sơ đăng nhập EasyInvoice cho dữ liệu kế toán này",
            provider_code=EASYINVOICE_PROVIDER_CODE,
        )
    secret_box = binding.secret_box
    if not isinstance(secret_box, SecretBox):
        raise EInvoiceProviderNotConfiguredError(
            "Bản cài chưa cấu hình khóa mã hóa ứng dụng nên không mở được mật khẩu "
            "nhà cung cấp — xem `python -m ket.admin generate-app-key`",
            provider_code=EASYINVOICE_PROVIDER_CODE,
        )
    credentials = EasyInvoiceCredentials(
        base_url=profile.base_url,
        username=profile.username,
        password=secret_box.decrypt(profile.password_enc),
        tax_code=profile.tax_code,
    )
    return EasyInvoiceProvider(binding.session, EasyInvoiceClient(credentials))


PROVIDERS.register(EASYINVOICE_PROVIDER_CODE, build)
