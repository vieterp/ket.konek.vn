"""Vòng đời hóa đơn điện tử — lập, phát hành, xác nhận, từ chối, hủy.

Lát 7D cài **nền**: cấp số gap-free, năm bất biến chặn cứng được, và một đường
phát hành **nội bộ** (chưa ký, chưa gọi nhà cung cấp). Đường ấy không phải mã
tạm: RT-10 chia lượt phát hành thành ba việc — ký đồng bộ, rồi *cấp số + đưa
vào hàng đợi trong một transaction*, rồi truyền tải — và tệp này cài đúng việc
thứ hai. 7E chèn việc thứ nhất trước `issue` và thay `confirm`/`reject` thủ
công bằng kết quả có thật của nhà cung cấp; không dòng nào ở đây phải viết lại.

**Bất biến của lát, và chỗ chặn của từng cái:**

| Quy tắc | Chặn ở đâu |
| --- | --- |
| BR-EIV-01 hóa đơn đã phát hành bất biến | trigger DB (`0032`) + không có endpoint sửa |
| BR-EIV-02 số liên tục theo (mẫu số, ký hiệu) | dãy `gap_free` + `uq_einvoices_form_number` + trigger cấm đổi số |
| BR-EIV-03 ngày hóa đơn ≥ ngày đăng ký sử dụng | `_require_registration` ở `issue` |
| BR-EIV-04 hủy cần đủ hai văn bản | `_require_cancellation_documents` ở `cancel` |
| BR-EIV-07 tổng tiền khớp chứng từ gốc | **theo cấu trúc** — hóa đơn không giữ số tiền nào (xem `models.py`) |
| FR-EIV-035 chặn sửa/xóa chứng từ gốc | `guards.py` |

Hai bất biến còn lại của bảng phase-07 **không** đóng ở lát này, và nói thẳng ra
thay vì để chúng xanh-vì-rỗng: **BR-EIV-05** (thay thế phải trỏ đúng hóa đơn bị
thay) mới có phần cấu trúc — cột, khóa ngoại, ràng buộc "không tự thay thế
mình"; phép kiểm chuỗi không vòng đi cùng đường ghi ở 7F. **BR-EIV-06** (không
phát hành khi chứng thư số hết hạn) thuộc 7E — lát này chưa có chữ ký nào để mà
kiểm hạn, và một phép kiểm giả trên một khái niệm chưa tồn tại là một dòng xanh
không chứng minh gì.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    EInvoiceCancellationIncompleteError,
    EInvoiceNotFoundError,
    EInvoiceNoticeSubmittedError,
    EInvoiceNotSentError,
    EInvoiceNumberRangeExhaustedError,
    EInvoiceRegistrationMissingError,
    InvoiceFormNotUsableError,
    MasterDataNotFoundError,
    VoucherNotFoundError,
)
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.numbering.service import NumberingService
from ket.modules.einvoice.models import (
    CANCELLATION_KINDS,
    SUPERSEDED_STATUSES,
    EInvoice,
    EInvoiceErrorNotice,
    EInvoiceRepresentation,
    EInvoiceStatus,
    ErrorNoticeKind,
    InvoiceRegistration,
    NoticeStatus,
)
from ket.modules.einvoice.numbering import scope_key_for
from ket.modules.einvoice.providers.internal import INTERNAL_PROVIDER_CODE
from ket.modules.einvoice.registration_service import InvoiceRegistrationService
from ket.modules.einvoice.state_machine import EInvoiceAction, transition, transition_to
from ket.posting.documents.models import Voucher
from ket.posting.documents.registry import REGISTRY as POSTING_DOCUMENT_REGISTRY


@dataclass(frozen=True, slots=True)
class UsableInvoiceForm:
    """Ký hiệu hóa đơn đã qua kiểm — mẫu số và ký hiệu **không** còn `None`.

    Một giá trị nhỏ thay vì trả thẳng dòng danh mục: `InvoiceForm.form_no` cho
    phép `NULL` (nút nhóm không có mẫu số), nên mọi chỗ dùng nó phải hoặc kiểm
    lại hoặc khẳng định suông. Bọc kết quả của lượt kiểm vào một kiểu mà trạng
    thái sai không biểu diễn được thì lượt kiểm ấy chỉ chạy một lần, và bộ kiểm
    kiểu canh giúp phần còn lại.
    """

    id: int
    form_no: str
    serial: str
    provider_code: str | None
    """Nhà cung cấp mà ký hiệu này khai (`0032`). `None` = không qua bên thứ ba
    nào — hóa đơn đặt in, tự in, hoặc bản cài dùng `internal`; đó cũng là điều
    kiện duy nhất quyết định dãy số cục bộ có được cấp hay không."""


_NOTICE_TITLES: dict[ErrorNoticeKind, str] = {
    ErrorNoticeKind.THONG_BAO_HUY: "thông báo hủy gửi cơ quan thuế",
    ErrorNoticeKind.BIEN_BAN_HUY: "biên bản hủy thỏa thuận với người mua",
}
"""Tên tiếng Việt của từng văn bản, để thông điệp BR-EIV-04 nói được **văn bản
nào** còn thiếu thay vì "thiếu giấy tờ"."""


class EInvoiceService:
    """Đọc và ghi hóa đơn điện tử, trong transaction của người gọi.

    **Không tự mở transaction**, cùng lý do `NumberingService`: lượt cấp số nhích
    bộ đếm trong transaction của người gọi, nên hóa đơn ghi hỏng thì số cũng lùi
    lại — đó là điều kiện để dãy không thủng (BR-EIV-02).
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._numbering = NumberingService(session)
        self._registrations = InvoiceRegistrationService(session)

    def create_draft(self, *, source_voucher_id: UUID, invoice_form_id: int) -> EInvoice:
        """Lập hóa đơn từ một chứng từ gốc (FR-EIV-010).

        Chi nhánh **không** là tham số: nó đọc từ chứng từ gốc, và khóa ngoại
        ghép của bảng bảo đảm hai vế không lệch được. Một tham số `branch_id` ở
        đây chỉ là một cơ hội để người gọi truyền sai.
        """
        voucher = self._require_invoiceable_voucher(source_voucher_id)
        self._require_usable_form(invoice_form_id)
        invoice = EInvoice(
            branch_id=voucher.branch_id,
            source_voucher_id=voucher.id,
            invoice_form_id=invoice_form_id,
            status=EInvoiceStatus.CHUA_PHAT_HANH,
        )
        self._session.add(invoice)
        self._session.flush()
        return invoice

    def issue(self, einvoice_id: UUID, *, invoice_date: date) -> EInvoice:
        """Cấp số và đưa hóa đơn vào trạng thái đang phát hành (FR-EIV-013).

        **Ai cấp số phụ thuộc ký hiệu** (quyết định user 2026-09-08). Ký hiệu
        khai `provider_code` thì **nhà cung cấp cấp số** — nó tới cùng câu trả
        lời của lượt phát hành, và `reconcile` điền vào. Ký hiệu không khai (hóa
        đơn đặt in, tự in, hoặc nhà cung cấp `internal`) thì dãy `gap_free` cục
        bộ cấp, đúng như 7D dựng: với chúng không có bên thứ ba nào cấp số, nên
        dãy ấy là sự thật duy nhất.

        **Mốc "đã làm lượt đầu" là `issued_at`, không phải `invoice_no`.** Với
        hóa đơn qua nhà cung cấp, số còn `NULL` suốt cả lượt phát hành đầu tiên,
        nên đọc `invoice_no` sẽ khiến lượt phát hành **lại** (sau khi bị từ
        chối) chạy lại trọn phần kiểm hồ sơ đăng ký và ghi đè ngày hóa đơn — tức
        sửa nội dung một tờ hóa đơn đã gửi đi.

        Phát hành lại một hóa đơn đã bị từ chối dùng lại đúng số cũ (ADR-013):
        số đã tiêu, đã nằm trong `allocated_numbers`, và cấp thêm một số nữa cho
        cùng tờ hóa đơn là bỏ lại một số không thuộc về ai. Trigger DB canh
        chiều còn lại nếu đường ghi nào lách được chỗ này.

        `invoice_date` cũng chỉ nhận ở lượt đầu: BR-EIV-03 đã kiểm nó một lần,
        và đổi ngày của một tờ hóa đơn đã phát hành là sửa nội dung hóa đơn.
        """
        invoice = self.require(einvoice_id)
        target = transition_to(EInvoiceStatus(invoice.status), EInvoiceAction.ISSUE)

        if invoice.issued_at is None:
            form = self._require_usable_form(invoice.invoice_form_id)
            self._require_registration(
                invoice_form_id=form.id, branch_id=invoice.branch_id, on_date=invoice_date
            )
            if form.provider_code is None:
                number = self._allocate_number(invoice, form=form)
                self._refuse_out_of_range(
                    number,
                    invoice_form_id=form.id,
                    branch_id=invoice.branch_id,
                    on_date=invoice_date,
                )
                invoice.invoice_no = number
            invoice.invoice_date = invoice_date
            invoice.issued_at = datetime.now(UTC)

        invoice.status = target
        self._session.flush()
        return invoice

    def confirm(
        self,
        einvoice_id: UUID,
        *,
        tax_authority_code: str | None = None,
        lookup_code: str | None = None,
        invoice_no: str | None = None,
    ) -> EInvoice:
        """Cơ quan thuế / nhà cung cấp đã nhận hóa đơn.

        Đây là chỗ `outbox` gọi khi nhà cung cấp trả về thành công, và cũng là
        thao tác tay của người đã tra cứu trên cổng cơ quan thuế.

        **Số hóa đơn có thể tới ở chính lượt này.** Với ký hiệu khai nhà cung
        cấp thì họ cấp số (quyết định user 2026-09-08), và số về cùng câu trả
        lời của lượt phát hành — xem `issue`.
        """
        invoice = self.require(einvoice_id)
        invoice.status = transition_to(EInvoiceStatus(invoice.status), EInvoiceAction.CONFIRM)
        if invoice.invoice_no is None and invoice_no is not None:
            # Số do nhà cung cấp cấp, điền **một lần** (quyết định user
            # 2026-09-08). Điều kiện `is None` không phải phòng thủ thừa: trigger
            # `einvoices_immutable_after_issue` chặn mọi lượt đổi một số đã có,
            # nên không có nó thì một lượt xác nhận lại của hóa đơn cấp số cục bộ
            # sẽ đâm vào trigger và làm kẹt hàng đợi thay vì đi qua.
            invoice.invoice_no = invoice_no
        invoice.tax_authority_status = 2
        invoice.tax_authority_code = tax_authority_code
        invoice.lookup_code = lookup_code
        invoice.tax_authority_message = None
        self._session.flush()
        return invoice

    def reject(self, einvoice_id: UUID, *, message: str) -> EInvoice:
        """Bị từ chối. **Giữ nguyên số đã cấp** (ADR-013) — xem `EInvoiceStatus`."""
        invoice = self.require(einvoice_id)
        invoice.status = transition_to(EInvoiceStatus(invoice.status), EInvoiceAction.REJECT)
        invoice.tax_authority_status = 3
        invoice.tax_authority_message = message
        self._session.flush()
        return invoice

    def mark_sent(
        self,
        einvoice_id: UUID,
        *,
        sent_to: str,
        sent_at: datetime | None = None,
        user_id: int,
    ) -> EInvoice:
        """Đánh dấu đã gửi bản thể hiện cho người mua (FR-EIV-020, cạnh `SEND`).

        **Lượt gửi xảy ra ngoài phần mềm** — quyết định user 2026-09-08, phương
        án A3. Hai đường thật đều nằm ngoài: kế toán gửi từ hộp thư của mình,
        hoặc chính nhà cung cấp gửi theo cấu hình bên họ (bản tích hợp
        EasyInvoice đang chạy thật bỏ hẳn `CusEmails` khỏi XML vì trường ấy làm
        họ trả `Code=127`, và tự lo phần thư từ). Bản ghi ở đây vì thế là **lời
        khai về một việc đã xảy ra**, không phải kết quả một thao tác của máy.

        Hệ quả với thiết kế, và là chỗ dễ làm sai nhất: **không** đòi phải có
        đính kèm bản thể hiện trước. Đòi nó sẽ chặn đúng ca thường gặp nhất —
        nhà cung cấp đã gửi, kế toán chưa từng bấm tải tệp về — tức chặn một lời
        khai trung thực. Thứ đòi được mà vẫn đúng là **người nhận**: một dấu
        "đã gửi" không nói được gửi cho ai thì không ai đối chiếu lại được.

        Ba đầu chặn của `sent_at`, mỗi cái đóng một hình dạng vô nghĩa khác nhau:
        không rỗng (mặc định là bây giờ), không sau hôm nay (ngày ghi trong sổ
        không đi trước đồng hồ — cùng trần `book_date` của 6F-2), và không trước
        `issued_at` (không gửi được thứ chưa tồn tại).
        """
        # `FOR UPDATE`, không phải `require` thường — và đây là bản sửa của một
        # lỗ đo được. Miễn trừ khóa idempotency của cửa này dựa trên lập luận
        # "lượt thứ hai đâm vào máy trạng thái trước khi chạm ba cột dấu gửi",
        # nhưng lập luận ấy chỉ đúng khi hai lượt **tuần tự**: hai luồng song
        # song cùng đọc `DA_PHAT_HANH`, cùng qua `transition_to`, và cả hai
        # cùng ghi — người nhận của lần bấm thứ nhất biến mất khỏi sổ. Khóa dòng
        # buộc lượt sau đọc lại trạng thái đã lật rồi mới quyết định.
        invoice = self.require(einvoice_id, for_update=True)
        recipient = sent_to.strip()
        if not recipient:
            raise EInvoiceNotSentError(
                "Khai người nhận bản thể hiện trước khi đánh dấu đã gửi",
                einvoice=str(einvoice_id),
            )
        stamp = sent_at if sent_at is not None else datetime.now(UTC)
        now = datetime.now(UTC)
        if stamp > now:
            raise EInvoiceNotSentError(
                "Thời điểm gửi không được ở tương lai", einvoice=str(einvoice_id)
            )
        if invoice.issued_at is not None and stamp < invoice.issued_at:
            raise EInvoiceNotSentError(
                "Thời điểm gửi không được trước lúc phát hành hóa đơn",
                einvoice=str(einvoice_id),
            )
        invoice.status = transition_to(EInvoiceStatus(invoice.status), EInvoiceAction.SEND)
        invoice.sent_at = stamp
        invoice.sent_to = recipient
        invoice.sent_by = user_id
        self._session.flush()
        return invoice

    def cancel(self, einvoice_id: UUID) -> EInvoice:
        """Hủy hóa đơn — đòi đủ thông báo hủy **và** biên bản hủy (BR-EIV-04)."""
        invoice = self.require(einvoice_id)
        target = transition_to(EInvoiceStatus(invoice.status), EInvoiceAction.CANCEL)
        self._require_cancellation_documents(invoice)
        invoice.status = target
        self.discard_representations(einvoice_id)
        self._session.flush()
        return invoice

    def mark_replaced(self, einvoice_id: UUID) -> EInvoice:
        """Đánh dấu tờ cũ **đã bị thay thế** (FR-EIV-030) — nửa thứ nhất của lượt thay thế.

        Cùng khuôn `cancel`: đi một cạnh của máy trạng thái rồi bỏ bản thể hiện
        đã lưu, để lượt tải kế tiếp dựng lại tờ có dấu trạng thái. Nửa thứ hai —
        dựng hóa đơn nháp mới trỏ ngược về tờ này — nằm ở `error_flow`, vì nó là
        phần *quy trình* chứ không phải phần *vòng đời một tờ hóa đơn*.

        Tách đôi chứ không gộp: chừng nào tờ cũ chưa rời `DA_PHAT_HANH` thì chỉ
        mục `uq_einvoices_live_source_voucher` còn chặn tờ thứ hai trên cùng
        chứng từ, nên thứ tự hai nửa là một **bất biến**, không phải sở thích.
        """
        invoice = self.require(einvoice_id)
        invoice.status = transition_to(EInvoiceStatus(invoice.status), EInvoiceAction.REPLACE)
        self.discard_representations(einvoice_id)
        self._session.flush()
        return invoice

    def discard_representations(self, einvoice_id: UUID) -> None:
        """Bỏ bản thể hiện đã lưu khi tờ hóa đơn thôi còn hiệu lực.

        **Không xóa tệp, chỉ bỏ dòng trỏ tới nó** — kho định địa chỉ theo nội
        dung giữ nguyên byte, nên hồ sơ lưu trữ không mất gì và lượt tải kế tiếp
        dựng lại đúng nội dung ấy, lần này **có dấu trạng thái**.

        Vì sao phải có bước này: `representation.ensure` trả bản đã cất trước
        mọi phép kiểm — đó là điều làm nó rẻ — nên dấu "ĐÃ HỦY" mà lát này thêm
        vào chỉ đóng được lúc **dựng**. Thứ tự thật của kế toán lại là *tải →
        gửi cho người mua → sau đó mới hủy*, tức đúng thứ tự khiến dấu ấy không
        bao giờ xuất hiện: người dùng bấm tải sau khi hủy và nhận lại y nguyên
        tờ giấy sạch của hôm trước. Đo được, và vòng review pre-landing bắt được.

        Bản của nhà cung cấp cũng bỏ theo: tờ họ dựng sau lượt hủy thường mang
        trạng thái của họ, nên lấy lại là **đúng hơn** giữ bản cũ.

        **7F gọi thêm ở hai cạnh còn lại** (`REPLACE`, `ADJUST`) — chúng đưa hóa
        đơn ra khỏi hiệu lực bằng cùng một nghĩa, và cùng cần dấu ấy.
        """
        self._session.execute(
            delete(EInvoiceRepresentation).where(EInvoiceRepresentation.einvoice_id == einvoice_id)
        )

    def delete(self, einvoice_id: UUID) -> None:
        """Xóa hóa đơn chưa phát hành. Số hóa đơn không tái sử dụng — nhưng ở
        trạng thái này thì chưa có số nào để mà nói tới."""
        invoice = self.require(einvoice_id)
        transition(EInvoiceStatus(invoice.status), EInvoiceAction.DELETE)
        self._session.delete(invoice)
        self._session.flush()

    def add_notice(
        self,
        einvoice_id: UUID,
        *,
        kind: ErrorNoticeKind,
        notice_no: str,
        notice_date: date,
        reason_code: str | None = None,
        reason: str | None = None,
        submitted: bool = False,
    ) -> EInvoiceErrorNotice:
        """Lập một văn bản kèm hóa đơn (FR-EIV-031/032)."""
        invoice = self.require(einvoice_id)
        notice = EInvoiceErrorNotice(
            einvoice_id=invoice.id,
            kind=kind,
            notice_no=notice_no,
            notice_date=notice_date,
            reason_code=reason_code,
            reason=reason,
            status=NoticeStatus.DA_NOP if submitted else NoticeStatus.NHAP,
            submitted_at=datetime.now(UTC) if submitted else None,
        )
        self._session.add(notice)
        self._session.flush()
        return notice

    def submit_notice(self, notice_id: UUID) -> EInvoiceErrorNotice:
        """Đánh dấu một văn bản đã nộp cơ quan thuế (FR-EIV-031/032).

        Vế thứ hai của quy trình hai bước "lập rồi nộp", và **thiếu nó thì bước
        thứ nhất là ngõ cụt** (review 7D H-3): văn bản lập ở trạng thái nháp
        không nộp được, không lập lại được (chỉ mục duy nhất theo loại), không
        xóa được — nên hóa đơn ấy vĩnh viễn không hủy được qua API.

        Gọi lại trên một văn bản đã nộp thì trả về nguyên trạng, không đóng dấu
        lại: thời điểm nộp là một sự kiện xảy ra một lần.
        """
        notice = self._require_notice(notice_id)
        if notice.status is NoticeStatus.DA_NOP:
            return notice
        notice.status = NoticeStatus.DA_NOP
        notice.submitted_at = datetime.now(UTC)
        self._session.flush()
        return notice

    def delete_notice(self, notice_id: UUID) -> None:
        """Xóa một văn bản **còn nháp** — đường sửa khi lập nhầm loại.

        Văn bản đã nộp thì không: nó đã ra khỏi phần mềm, và xóa nó khỏi sổ là
        làm sổ nói khác thứ cơ quan thuế đang giữ.
        """
        notice = self._require_notice(notice_id)
        if notice.status is NoticeStatus.DA_NOP:
            raise EInvoiceNoticeSubmittedError(
                "Văn bản đã nộp cơ quan thuế nên không xóa được",
                notice_id=str(notice_id),
            )
        self._session.delete(notice)
        self._session.flush()

    def _require_notice(self, notice_id: UUID) -> EInvoiceErrorNotice:
        notice = self._session.get(EInvoiceErrorNotice, notice_id)
        if notice is None:
            raise EInvoiceNotFoundError(
                "Không tìm thấy văn bản của hóa đơn", notice_id=str(notice_id)
            )
        return notice

    def require(self, einvoice_id: UUID, *, for_update: bool = False) -> EInvoice:
        """Hóa đơn theo id, hoặc 404.

        `for_update` khóa dòng cho tới cuối transaction. Chỉ đường ghi nào có
        **hai lượt cùng hợp lệ trên cùng trạng thái nguồn** mới cần nó: ở đó máy
        trạng thái không phân xử được, vì cả hai lượt đều đọc ra cùng một trạng
        thái trước khi lượt nào kịp ghi. Xem `mark_sent`.
        """
        invoice = self._session.get(EInvoice, einvoice_id, with_for_update=for_update)
        if invoice is None:
            raise EInvoiceNotFoundError(
                "Không tìm thấy hóa đơn điện tử", einvoice_id=str(einvoice_id)
            )
        return invoice

    def list_by_status(
        self,
        *,
        statuses: Sequence[EInvoiceStatus] | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[EInvoice], int]:
        """Một trang hóa đơn + tổng số, lọc theo trạng thái (FR-EIV-015).

        Phân trang chứ không trả tất (review 7D M-3): bảng này mọc theo số hóa
        đơn của doanh nghiệp, và `routers/vouchers.py` đã phân trang cho đúng
        loại dữ liệu ấy. RLS lọc chi nhánh.
        """
        query = select(EInvoice)
        if statuses:
            query = query.where(EInvoice.status.in_(list(statuses)))
        total = self._session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = self._session.scalars(
            query.order_by(EInvoice.issued_at.desc().nullslast(), EInvoice.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return rows, total

    def issued_for_voucher(self, voucher_id: UUID) -> EInvoice | None:
        """Hóa đơn **còn hiệu lực** của một chứng từ, nếu có — nguồn của FR-EIV-035.

        `DANG_PHAT_HANH` tính là đã phát hành ở đây dù chưa có xác nhận của cơ
        quan thuế: số đã cấp và bản XML đã ra khỏi phần mềm, nên sửa chứng từ
        gốc lúc ấy là làm lệch một tờ hóa đơn có thể đang trên đường tới CQT.

        **Ba trạng thái cuối thì nhả chứng từ ra** (`SUPERSEDED_STATUSES`, quyết
        định user 2026-09-10). Lát 7D chỉ chừa `CHUA_PHAT_HANH`, và điều đó khóa
        chứng từ **vĩnh viễn** sau khi xử lý sai sót — trong khi chính thông điệp
        của guard bảo người dùng "xử lý ở phía hóa đơn trước", tức hứa rằng xử lý
        xong thì chứng từ mở ra. Không nhả thì hai kịch bản bắt buộc của
        `docs/srs/07` §4.4 chết theo: "sai số tiền + khách chưa kê khai ⇒ THAY
        THẾ" cần sửa số tiền trên chứng từ, còn "không phát sinh giao dịch ⇒ HỦY"
        cần bỏ ghi sổ chứng từ khống để đảo tác động kế toán của nó.

        **Nới tới đây thôi: sửa và bỏ ghi sổ, KHÔNG phải xóa.**
        `fk_einvoices_source_voucher` khai `RESTRICT`, nên chừng nào tờ hóa đơn
        còn thì chứng từ gốc còn — và tờ hóa đơn *phải* còn: số đã tiêu
        (ADR-013) và nó nằm trong nghĩa vụ lưu trữ mười năm, nên một tờ hóa đơn
        không có chứng từ gốc là chỗ trống không giải trình được. Khóa ngoại là
        lớp canh cuối cho đúng ranh giới ấy; guard này không nói gì về nó.

        Dấu vết không mất theo: bản XML/PDF lưu trữ của 7E-3 mới là bằng chứng
        "đã gửi đi những con số nào", và nó nằm ở kho định địa chỉ theo nội dung
        chứ không đọc lại từ chứng từ. Chứng từ đổi sau khi thay thế vì thế không
        viết lại được lịch sử — đó đúng là hình dạng mà chỉ mục riêng phần
        `uq_einvoices_live_source_voucher` đã chừa sẵn từ 7D.
        """
        return self._session.scalars(
            select(EInvoice)
            .where(
                EInvoice.source_voucher_id == voucher_id,
                EInvoice.status != EInvoiceStatus.CHUA_PHAT_HANH,
                EInvoice.status.not_in(SUPERSEDED_STATUSES),
            )
            .limit(1)
        ).first()

    def _require_invoiceable_voucher(self, voucher_id: UUID) -> Voucher:
        voucher = self._session.get(Voucher, voucher_id)
        if voucher is None:
            raise VoucherNotFoundError("Không tìm thấy chứng từ", voucher_id=str(voucher_id))
        document_type = POSTING_DOCUMENT_REGISTRY.get(voucher.document_type)
        if not document_type.invoiceable:
            raise InvoiceFormNotUsableError(
                f"Loại chứng từ {document_type.title} không xuất hóa đơn điện tử được",
                document_type=voucher.document_type,
            )
        return voucher

    def _require_usable_form(self, invoice_form_id: int) -> UsableInvoiceForm:
        form = self._session.get(InvoiceForm, invoice_form_id)
        if form is None:
            raise MasterDataNotFoundError(
                "Không tìm thấy ký hiệu hóa đơn",
                entity_type="invoice_forms",
                entity_id=invoice_form_id,
            )
        if form.is_group or form.form_no is None:
            raise InvoiceFormNotUsableError(
                "Đây là nút nhóm trong danh mục mẫu số, không phải một ký hiệu hóa đơn"
            )
        if not form.is_active:
            raise InvoiceFormNotUsableError("Ký hiệu hóa đơn này đã ngừng theo dõi")
        return UsableInvoiceForm(
            id=form.id,
            form_no=form.form_no,
            serial=form.code,
            provider_code=form.provider_code,
        )

    def _require_registration(
        self, *, invoice_form_id: int, branch_id: int, on_date: date
    ) -> InvoiceRegistration:
        """BR-EIV-03 — ngày hóa đơn phải ≥ ngày bắt đầu sử dụng đã đăng ký."""
        registration = self._registrations.effective_for(
            invoice_form_id=invoice_form_id, branch_id=branch_id, on_date=on_date
        )
        if registration is None:
            raise EInvoiceRegistrationMissingError(
                "Ngày hóa đơn nằm trước ngày bắt đầu sử dụng đã đăng ký với cơ "
                "quan thuế, hoặc ký hiệu này chưa có hồ sơ đăng ký còn hiệu lực "
                "ở chi nhánh này",
                invoice_date=on_date.isoformat(),
            )
        return registration

    def _allocate_number(self, invoice: EInvoice, *, form: UsableInvoiceForm) -> str:
        scope_key = scope_key_for(form_no=form.form_no, serial=form.serial)
        return self._numbering.allocate_by_scope_key(scope_key, document_id=invoice.id)

    def _refuse_out_of_range(
        self, number: str, *, invoice_form_id: int, branch_id: int, on_date: date
    ) -> None:
        """FR-INV-002 — số vừa cấp phải nằm trong một dải đã thông báo phát hành.

        Hỏi **mọi** hồ sơ còn hiệu lực, không riêng hồ sơ mới nhất: dải cũ vẫn
        còn giá trị, và lý do đầy đủ ở `InvoiceRegistrationService.covers_number`.

        Kiểm **sau** khi cấp chứ không trước: `peek` không khóa dòng bộ đếm nên
        con số nó trả về là con số của người khác ngay khi có hai người cùng
        phát hành. Số cấp ra rồi mới đo là đúng — transaction này sẽ rollback và
        trả số lại cho dãy, đúng như mọi lượt phát hành hỏng khác.
        """
        if self._registrations.covers_number(
            invoice_form_id=invoice_form_id,
            branch_id=branch_id,
            on_date=on_date,
            number=int(number),
        ):
            return
        raise EInvoiceNumberRangeExhaustedError(
            "Số hóa đơn vừa cấp nằm ngoài mọi dải đã thông báo phát hành — lập "
            "thông báo phát hành mới trước khi tiếp tục",
            number=number,
        )

    def _require_cancellation_documents(self, invoice: EInvoice) -> None:
        """BR-EIV-04 — đủ **cả hai** văn bản, và cả hai phải đã nộp.

        Một phép đếm chứ không một vòng phân loại, nhờ chỉ mục duy nhất
        `(einvoice_id, kind)` của bảng văn bản: mỗi loại tối đa một bản, nên
        "có đủ hai loại" và "đếm được hai dòng thuộc bộ văn bản hủy" là cùng
        một câu.
        """
        present = set(
            self._session.scalars(
                select(EInvoiceErrorNotice.kind).where(
                    EInvoiceErrorNotice.einvoice_id == invoice.id,
                    EInvoiceErrorNotice.kind.in_(list(CANCELLATION_KINDS)),
                    EInvoiceErrorNotice.status == NoticeStatus.DA_NOP,
                )
            ).all()
        )
        missing = sorted(CANCELLATION_KINDS - present, key=int)
        if missing:
            raise EInvoiceCancellationIncompleteError(
                "Hủy hóa đơn phải có đủ hai văn bản đã nộp — còn thiếu: "
                + ", ".join(_NOTICE_TITLES[kind] for kind in missing),
                missing=",".join(str(int(kind)) for kind in missing),
            )

    def count_by_status(self) -> dict[int, int]:
        """Đếm hóa đơn theo trạng thái — nguồn của bộ lọc U3/FR-EIV-015 trên UI."""
        rows = self._session.execute(
            select(EInvoice.status, func.count()).group_by(EInvoice.status)
        ).all()
        return {int(status): count for status, count in rows}

    def provider_code_for(self, einvoice_id: UUID) -> str:
        """Nhà cung cấp mà ký hiệu của tờ hóa đơn này khai (FR-EIV-001).

        `invoice_forms.provider_code` là chỗ duy nhất giữ lời khai ấy — `0032`
        đã đặt nó ở đó cùng ràng buộc `provider_only_for_electronic`, nên một ký
        hiệu hóa đơn đặt in không mang nhà cung cấp nào là đúng theo cấu trúc.

        Trống thì trả `internal`: bản cài chưa ký hợp đồng với nhà cung cấp nào
        vẫn phải phát hành được trong phần mềm — xem `providers/internal.py`. Đó
        là một mặc định có chủ đích, không phải một lượt né lỗi: đường thay thế
        duy nhất là từ chối phát hành, và nó biến việc chưa cấu hình một tích
        hợp thành việc không dùng được phần mềm.
        """
        invoice = self.require(einvoice_id)
        form = self._session.get(InvoiceForm, invoice.invoice_form_id)
        if form is None or form.provider_code is None:
            return INTERNAL_PROVIDER_CODE
        return form.provider_code
