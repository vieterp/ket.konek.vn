"""Vòng đời hóa đơn điện tử trên PostgreSQL thật (lát 7D).

Kiểm những bất biến mà lát này chịu trách nhiệm chặn cứng — bảng chuyển trạng
thái thuần đã có `test_einvoice_state_machine.py`, dãy số và Open question #13
có `test_einvoice_numbering.py`, FR-EIV-035 có `test_einvoice_source_guard.py`:

* Lập hóa đơn từ chứng từ bán; chi nhánh **đọc từ chứng từ**, không nhận từ
  người gọi, và khóa ngoại ghép giữ hai vế không lệch được.
* Loại chứng từ không bật cờ `invoiceable` thì không xuất hóa đơn được — cờ ở
  registry của `posting`, không phải một danh sách mã trong `modules/einvoice`.
* **BR-EIV-03**: không có hồ sơ đăng ký còn hiệu lực phủ ngày hóa đơn thì không
  cấp số. Hai ca — chưa đăng ký, và ngày hóa đơn trước ngày bắt đầu sử dụng.
* **BR-EIV-04**: hủy đòi đủ **hai văn bản đã nộp**; thiếu một thì thông điệp
  phải nêu đích danh văn bản còn thiếu.
* **BR-EIV-01**: hóa đơn `status >= DA_PHAT_HANH` bất biến — kiểm bằng `UPDATE`
  thẳng bằng SQL trên **từng cột nội dung**, vì API không có đường sửa nào để
  mà thử. Bài ấy duyệt cột thật của bảng, nên một cột thêm ở 7E/7F mà quên dựng
  lại trigger sẽ làm nó đỏ.
* **FR-INV-002/008**: số vượt dải đã thông báo bị chặn; dải chồng lấn bị chặn
  **xuyên chi nhánh**, kể cả khi người lập không nhìn thấy hồ sơ của chi nhánh
  kia (RLS) — đó là toàn bộ lý do phép kiểm ấy chạy ngoài RLS.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    EInvoiceCancellationIncompleteError,
    EInvoiceNoticeSubmittedError,
    EInvoiceNumberRangeExhaustedError,
    EInvoiceRegistrationMissingError,
    EInvoiceTransitionError,
    InvoiceFormBranchConflictError,
    InvoiceFormNotUsableError,
    InvoiceRegistrationOverlapError,
)
from ket.kernel.master_data.models.invoice_form import InvoiceFormKind
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.pricing import PriceSource
from ket.modules.einvoice.models import (
    EInvoice,
    EInvoiceStatus,
    ErrorNoticeKind,
    RegistrationStatus,
)
from ket.modules.einvoice.registration_service import InvoiceRegistrationService
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.documents.models import Voucher
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
FEB_10 = date(2026, 2, 10)

# Khối id 97xx là của ba tệp hóa đơn điện tử. `id` cố định là quy ước sẵn của
# bộ test danh mục (`path` phải là chuỗi id có dấu chấm), nhưng nó biến mỗi con
# số thành một **tài nguyên dùng chung cả phiên**: bản đầu của lát này lấy 9401,
# đúng số `test_voucher_print_api` dùng cho khách hàng CÓ ĐỊA CHỈ — helper
# `ensure_customer` chạy trước, tạo bản ghi không địa chỉ, và bài in phiếu thu
# đỏ vì ô "Địa chỉ" trống. Tách khối để lát sau không phải dò lại.
CUSTOMER_ID = 9701
SALESPERSON_ID = 9702

FORM_ID = 8801
SERIAL = "C26TFL"
"""Ký hiệu riêng của tệp này — xem `einvoice_support` về việc vì sao mỗi tệp
phải có ký hiệu của mình."""

RANGE_FORM_ID = 8802
RANGE_SERIAL = "C26TRG"
"""Ký hiệu thứ hai, có dải số hữu hạn, để kiểm FR-INV-002 mà không làm cạn dãy
của các bài khác."""

_MUTABLE_AFTER_ISSUE = frozenset(
    {"status", "tax_authority_status", "tax_authority_code", "tax_authority_message", "lookup_code"}
)
"""Năm cột chở tin từ cơ quan thuế về — trigger cố ý để mở, xem `0032`."""


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    account_ids = seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7D-01")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7D-01")
    return account_ids


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


def _sales_voucher(session: Session, context: PostingContext, accounts: dict[str, int]) -> Voucher:
    """Một hóa đơn bán đã cất — chứng từ gốc của mọi hóa đơn điện tử ở lát này."""
    payload = SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=FEB_10,
        posting_date=FEB_10,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        description="bán hàng cho hóa đơn điện tử",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(1),
                unit_price_fc=Decimal(1_000_000),
                amount_fc=Decimal(1_000_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(100_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )
    return SalesInvoiceService(session).create(payload, user_id=ACTOR_ID)


def _form_id(session: Session, context: PostingContext) -> int:
    """Ký hiệu mặc định của tệp, kèm hồ sơ đăng ký đã kích hoạt (BR-EIV-03)."""
    form = ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
    ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
    return form.id


def _draft(session: Session, context: PostingContext, accounts: dict[str, int]) -> EInvoice:
    voucher = _sales_voucher(session, context, accounts)
    return EInvoiceService(session).create_draft(
        source_voucher_id=voucher.id, invoice_form_id=_form_id(session, context)
    )


def test_a_draft_takes_its_branch_from_the_source_voucher(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Chi nhánh không phải tham số — nó là sự thật của chứng từ gốc."""

    def work(session: Session) -> None:
        invoice = _draft(session, context, accounts)
        assert invoice.branch_id == context.branch_id
        assert EInvoiceStatus(invoice.status) is EInvoiceStatus.CHUA_PHAT_HANH
        assert invoice.invoice_no is None
        assert invoice.invoice_date is None

    run(work)


def test_a_document_type_without_the_invoiceable_flag_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cờ ở registry của `posting` là thứ quyết định, không một danh sách mã.

    Dựng bằng cách đổi `document_type` của một chứng từ đã cất sang `GLE`: mọi
    thứ khác giữ nguyên, nên bài này đo đúng một điều — cờ.
    """

    def work(session: Session) -> None:
        voucher = _sales_voucher(session, context, accounts)
        voucher.document_type = "GLE"
        session.flush()
        with pytest.raises(InvoiceFormNotUsableError, match="không xuất hóa đơn điện tử được"):
            EInvoiceService(session).create_draft(
                source_voucher_id=voucher.id, invoice_form_id=_form_id(session, context)
            )

    run(work)


def test_a_group_node_is_not_a_serial(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Nút nhóm trong danh mục mẫu số không phát hành được gì."""

    def work(session: Session) -> None:
        voucher = _sales_voucher(session, context, accounts)
        group = ensure_invoice_form(
            session, form_id=8803, serial="C26TGRP", form_no=None, kind=None, is_group=True
        )
        with pytest.raises(InvoiceFormNotUsableError, match="nút nhóm"):
            EInvoiceService(session).create_draft(
                source_voucher_id=voucher.id, invoice_form_id=group.id
            )

    run(work)


def test_issuing_allocates_a_number_and_moves_into_the_queue(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cấp số + lật trạng thái trong một transaction (RT-10)."""

    def work(session: Session) -> None:
        invoice = _draft(session, context, accounts)
        issued = EInvoiceService(session).issue(invoice.id, invoice_date=FEB_10)
        assert EInvoiceStatus(issued.status) is EInvoiceStatus.DANG_PHAT_HANH
        assert issued.invoice_no is not None
        # Tám chữ số theo NĐ123 — độ dài này in trên tờ hóa đơn, nó là một con
        # số của pháp luật chứ không của cấu hình.
        assert len(issued.invoice_no) == 8
        assert issued.invoice_date == FEB_10
        assert issued.issued_at is not None

    run(work)


def test_a_rejected_invoice_keeps_its_number_when_reissued(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """ADR-013: phát hành lại dùng lại số cũ, không đốt thêm số.

    Đây là chỗ dãy gap-free thủng nếu ai đó cấp số lần hai — và cái thủng ấy
    phải giải trình với cơ quan thuế, nên bài kiểm cả số của hóa đơn lẫn con
    số kế tiếp của dãy.
    """

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        first = service.issue(invoice.id, invoice_date=FEB_10).invoice_no
        service.reject(invoice.id, message="CQT từ chối: sai mã số thuế người mua")
        assert EInvoiceStatus(invoice.status) is EInvoiceStatus.PHAT_HANH_LOI
        assert invoice.invoice_no == first

        again = service.issue(invoice.id, invoice_date=FEB_10)
        assert again.invoice_no == first
        assert EInvoiceStatus(again.status) is EInvoiceStatus.DANG_PHAT_HANH

    run(work)


def test_an_invoice_date_before_the_registration_start_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """BR-EIV-03 — ngày hóa đơn phải ≥ ngày bắt đầu sử dụng đã đăng ký."""

    def work(session: Session) -> None:
        invoice = _draft(session, context, accounts)
        with pytest.raises(EInvoiceRegistrationMissingError):
            EInvoiceService(session).issue(invoice.id, invoice_date=date(2025, 12, 31))

    run(work)


def test_a_serial_without_an_active_registration_cannot_issue(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Vế thứ hai của BR-EIV-03: hồ sơ còn ở trạng thái nháp không mở đường nào."""

    def work(session: Session) -> None:
        voucher = _sales_voucher(session, context, accounts)
        form = ensure_invoice_form(session, form_id=8804, serial="C26TNOREG")
        registration = InvoiceRegistrationService(session).create(
            branch_id=context.branch_id, invoice_form_id=form.id, start_date=date(2026, 1, 1)
        )
        assert RegistrationStatus(registration.status) is RegistrationStatus.NHAP
        invoice = EInvoiceService(session).create_draft(
            source_voucher_id=voucher.id, invoice_form_id=form.id
        )
        with pytest.raises(EInvoiceRegistrationMissingError):
            EInvoiceService(session).issue(invoice.id, invoice_date=FEB_10)

    run(work)


def test_cancelling_needs_both_documents_and_says_which_one_is_missing(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """BR-EIV-04 — người dùng đang ở giữa quy trình hai bước, phải biết còn bước nào."""

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        service.issue(invoice.id, invoice_date=FEB_10)
        service.confirm(invoice.id, tax_authority_code="M1-26-ABC", lookup_code="TRA-CUU-1")

        with pytest.raises(EInvoiceCancellationIncompleteError, match="thông báo hủy"):
            service.cancel(invoice.id)

        service.add_notice(
            invoice.id,
            kind=ErrorNoticeKind.THONG_BAO_HUY,
            notice_no="TBH-001",
            notice_date=FEB_10,
            submitted=True,
        )
        with pytest.raises(EInvoiceCancellationIncompleteError, match="biên bản hủy"):
            service.cancel(invoice.id)

        service.add_notice(
            invoice.id,
            kind=ErrorNoticeKind.BIEN_BAN_HUY,
            notice_no="BBH-001",
            notice_date=FEB_10,
            submitted=True,
        )
        assert EInvoiceStatus(service.cancel(invoice.id).status) is EInvoiceStatus.DA_HUY

    run(work)


def test_a_notice_still_in_draft_does_not_unlock_the_cancellation(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Văn bản **chưa nộp** không phải một văn bản đã có — **và nộp được**.

    Đếm cả bản nháp thì hủy được một hóa đơn mà cơ quan thuế chưa nhận được
    thông báo nào, trong khi tờ hóa đơn ấy vẫn đang có hiệu lực với họ.

    Bài đi tiếp một bước nữa, và bước ấy là thứ bản đầu thiếu (review 7D H-3):
    nó **nộp** hai văn bản rồi hủy thành công. Không có bước ấy thì bài này ghim
    một cái bẫy như thể nó là hành vi mong muốn — văn bản nháp không nộp được,
    không lập lại được (chỉ mục duy nhất theo loại), không xóa được, nên hóa đơn
    vĩnh viễn không hủy được qua API và không bài nào thấy.
    """

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        service.issue(invoice.id, invoice_date=FEB_10)
        service.confirm(invoice.id)
        notices = [
            service.add_notice(
                invoice.id, kind=kind, notice_no=number, notice_date=FEB_10, submitted=False
            )
            for kind, number in (
                (ErrorNoticeKind.THONG_BAO_HUY, "TBH-002"),
                (ErrorNoticeKind.BIEN_BAN_HUY, "BBH-002"),
            )
        ]
        with pytest.raises(EInvoiceCancellationIncompleteError):
            service.cancel(invoice.id)

        for notice in notices:
            service.submit_notice(notice.id)
        assert EInvoiceStatus(service.cancel(invoice.id).status) is EInvoiceStatus.DA_HUY

    run(work)


def test_a_notice_of_the_wrong_kind_can_be_deleted_and_replaced(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Lập nhầm loại văn bản sửa được — **trước** khi nộp (review 7D H-3).

    Chỉ mục duy nhất `(einvoice_id, kind)` là thứ làm BR-EIV-04 gọn thành một
    phép đếm; cái giá của nó là "lập nhầm rồi thì không lập lại được", nên phải
    có đường xóa. Sau khi nộp thì không: văn bản đã ra khỏi phần mềm.
    """

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        service.issue(invoice.id, invoice_date=FEB_10)
        service.confirm(invoice.id)
        wrong = service.add_notice(
            invoice.id,
            kind=ErrorNoticeKind.THONG_BAO_HUY,
            notice_no="TBH-SAI",
            notice_date=FEB_10,
        )
        service.delete_notice(wrong.id)
        again = service.add_notice(
            invoice.id,
            kind=ErrorNoticeKind.THONG_BAO_HUY,
            notice_no="TBH-DUNG",
            notice_date=FEB_10,
            submitted=True,
        )
        with pytest.raises(EInvoiceNoticeSubmittedError):
            service.delete_notice(again.id)

    run(work)


def test_an_issued_invoice_refuses_every_content_update_from_raw_sql(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """BR-EIV-01 bằng trigger, đo trên **từng cột thật của bảng**.

    Duyệt cột theo ORM chứ không theo một danh sách gõ tay: cột thêm ở 7E
    (`sent_at`) hay 7F (chuỗi sai sót) mà quên dựng lại trigger sẽ làm bài này
    đỏ, thay vì lặng lẽ nằm ngoài bất biến quan trọng nhất của phân hệ.

    Giá trị thử dựng theo `python_type` của chính cột, không theo tên kiểu SQL:
    tên kiểu là thứ đổi theo dialect (`Uuid` in ra `CHAR(32)` ở bộ dịch chung),
    và một bảng tra theo tên là một chỗ nữa để bài kiểm mục nát trong im lặng.

    `UPDATE` đi vòng qua service vì API **không có** đường sửa nào — đó chính là
    lớp bảo vệ thứ nhất, và bài này đo lớp thứ hai.
    """

    def sample_for(python_type: type) -> object:
        if issubclass(python_type, bool):
            return True
        if issubclass(python_type, int):
            return -1
        if issubclass(python_type, str):
            return "x"
        if issubclass(python_type, datetime):
            return datetime(2030, 1, 1, tzinfo=UTC)
        if issubclass(python_type, date):
            return date(2030, 1, 1)
        if issubclass(python_type, UUID):
            return uuid4()
        raise AssertionError(f"bài kiểm chưa biết dựng giá trị cho {python_type}")

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        service.issue(invoice.id, invoice_date=FEB_10)
        service.confirm(invoice.id)
        session.flush()
        invoice_id = invoice.id

        columns = inspect(EInvoice).columns
        guarded = [column for column in columns if column.name not in _MUTABLE_AFTER_ISSUE]
        assert guarded, "không còn cột nào bị khóa — trigger đã mất ý nghĩa"

        for column in guarded:
            with session.begin_nested() as savepoint:
                with pytest.raises(ProgrammingError) as raised:
                    session.execute(
                        update(EInvoice)
                        .where(EInvoice.id == invoice_id)
                        .values({column.name: sample_for(column.type.python_type)})
                    )
                message = str(raised.value)
                assert "bat bien" in message or "khong doi duoc" in message, column.name
                savepoint.rollback()

    run(work)


def test_the_allocated_number_never_changes_even_before_issue(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Nửa BR-EIV-02: số bất biến kể từ lúc CÓ, không chờ ngưỡng đã phát hành.

    Hóa đơn ở `DANG_PHAT_HANH` chưa vượt ngưỡng bất biến, nên nhánh BR-EIV-01
    của trigger không chạm nó — nhánh riêng cho số hóa đơn mới là thứ giữ dãy
    không thủng ở đúng khoảng thời gian ấy.
    """

    def work(session: Session) -> None:
        invoice = _draft(session, context, accounts)
        EInvoiceService(session).issue(invoice.id, invoice_date=FEB_10)
        session.flush()
        assert EInvoiceStatus(invoice.status) is EInvoiceStatus.DANG_PHAT_HANH
        with session.begin_nested() as savepoint:
            with pytest.raises(ProgrammingError, match="So hoa don da cap khong doi duoc"):
                session.execute(
                    text("UPDATE einvoices SET invoice_no = '99999999' WHERE id = :id"),
                    {"id": invoice.id},
                )
            savepoint.rollback()

    run(work)


def test_an_issued_invoice_cannot_be_deleted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Bỏ một số đã cấp đi bằng đường xóa là làm dãy thủng không giải trình được."""

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        service.issue(invoice.id, invoice_date=FEB_10)
        with pytest.raises(EInvoiceTransitionError):
            service.delete(invoice.id)

    run(work)


def test_a_draft_can_be_deleted(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        invoice_id = invoice.id
        service.delete(invoice_id)
        assert session.get(EInvoice, invoice_id) is None

    run(work)


def test_a_number_past_the_notified_range_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-INV-002 — hóa đơn giấy mang số ngoài dải đã thông báo là hóa đơn không
    hợp lệ về thuế, và nó chỉ lộ ra ở kỳ quyết toán nếu không chặn ở đây."""

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        form = ensure_invoice_form(
            session,
            form_id=RANGE_FORM_ID,
            serial=RANGE_SERIAL,
            form_no="01GTKT3/001",
            kind=InvoiceFormKind.DAT_IN,
        )
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=form.id,
            range_from=1,
            range_to=1,
        )
        first = service.create_draft(
            source_voucher_id=_sales_voucher(session, context, accounts).id,
            invoice_form_id=form.id,
        )
        assert service.issue(first.id, invoice_date=FEB_10).invoice_no == "00000001"

        second = service.create_draft(
            source_voucher_id=_sales_voucher(session, context, accounts).id,
            invoice_form_id=form.id,
        )
        with pytest.raises(EInvoiceNumberRangeExhaustedError):
            service.issue(second.id, invoice_date=FEB_10)

    run(work)


def test_overlapping_ranges_of_one_branch_are_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-INV-008 — hai thông báo phát hành của cùng ký hiệu không đè dải nhau.

    Sau quyết định C-1 (một ký hiệu thuộc một chi nhánh), chồng lấn **xuyên chi
    nhánh** không còn biểu diễn được: lượt đăng ký của chi nhánh thứ hai bị
    `InvoiceFormBranchConflictError` chặn trước — xem
    `test_a_serial_registered_by_another_branch_is_refused`. Thứ còn lại để đo
    là chồng lấn **trong cùng một chi nhánh**: doanh nghiệp xin thêm dải mà gõ
    nhầm số đầu, và hai thông báo cùng phủ một số.
    """

    def work(session: Session) -> None:
        form = ensure_invoice_form(session, form_id=8809, serial="C26TOVL")
        service = InvoiceRegistrationService(session)
        first = service.create(
            branch_id=context.branch_id,
            invoice_form_id=form.id,
            start_date=date(2026, 1, 1),
            notice_no="TB-OVL-1",
            range_from=100,
            range_to=200,
        )
        service.activate(first.id)
        with pytest.raises(InvoiceRegistrationOverlapError):
            service.create(
                branch_id=context.branch_id,
                invoice_form_id=form.id,
                start_date=date(2026, 2, 1),
                notice_no="TB-OVL-2",
                range_from=150,
                range_to=250,
            )

    run(work)


def test_activating_a_registration_starts_the_sequence_at_the_range_start(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Dải `500–600` phải bắt đầu ở 500, không ở 1.

    Đếm từ 1 là năm trăm số nằm ngoài thông báo phát hành — chúng in ra được và
    không tờ nào hợp lệ.
    """

    def work(session: Session) -> None:
        form = ensure_invoice_form(session, form_id=8806, serial="C26TSTART")
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=form.id,
            range_from=500,
            range_to=600,
        )
        invoice = EInvoiceService(session).create_draft(
            source_voucher_id=_sales_voucher(session, context, accounts).id,
            invoice_form_id=form.id,
        )
        issued = EInvoiceService(session).issue(invoice.id, invoice_date=FEB_10)
        assert issued.invoice_no == "00000500"

    run(work)


def test_two_invoices_of_one_serial_cannot_share_a_number(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """`uq_einvoices_form_number` là lớp chót của BR-EIV-02.

    Dãy `gap_free` **sinh** ra số liên tục; ràng buộc này chặn một đường ghi
    thứ hai gán trùng số. Hai cơ chế cho hai loại sai sót khác nhau, nên bài
    kiểm cơ chế thứ hai bằng cách ghi thẳng, bỏ qua cơ chế thứ nhất.
    """

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        number = service.issue(invoice.id, invoice_date=FEB_10).invoice_no
        session.flush()
        twin = service.create_draft(
            source_voucher_id=_sales_voucher(session, context, accounts).id,
            invoice_form_id=invoice.invoice_form_id,
        )
        with session.begin_nested() as savepoint:
            with pytest.raises(Exception, match="uq_einvoices_form_number"):
                session.execute(
                    text(
                        "UPDATE einvoices SET invoice_no = :no, invoice_date = :day,"
                        " status = 1 WHERE id = :id"
                    ),
                    {"no": number, "day": FEB_10, "id": twin.id},
                )
                session.flush()
            savepoint.rollback()

    run(work)


def test_one_voucher_cannot_carry_two_live_invoices(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Một chứng từ, một hóa đơn còn hiệu lực (review 7D H-1).

    Đây là chiều mà lập luận "BR-EIV-07 đúng theo cấu trúc" **không** đóng: hóa
    đơn không mang số tiền nên nó đọc từ chứng từ gốc, và hai tờ hóa đơn của
    cùng một chứng từ đều khớp chứng từ gốc từng đồng — doanh thu khai gấp đôi
    mà không phép kiểm nào thấy. Chỉ mục riêng phần là thứ chặn.
    """

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        invoice = _draft(session, context, accounts)
        session.flush()
        with pytest.raises(IntegrityError, match="uq_einvoices_live_source_voucher"):
            with session.begin_nested() as savepoint:
                service.create_draft(
                    source_voucher_id=invoice.source_voucher_id,
                    invoice_form_id=invoice.invoice_form_id,
                )
                session.flush()
                savepoint.rollback()

    run(work)


def test_a_numbered_invoice_cannot_be_deleted_by_raw_sql(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Trigger `DELETE` — máy trạng thái sống ở Python, dãy số thì không.

    Một `DELETE` thẳng bằng SQL bỏ tờ hóa đơn đi và để lại một dòng
    `allocated_numbers` trỏ vào hư không: dãy thủng ở đúng chỗ khó phát hiện
    nhất, vì không còn gì để đối chiếu (review 7D M-2).
    """

    def work(session: Session) -> None:
        invoice = _draft(session, context, accounts)
        EInvoiceService(session).issue(invoice.id, invoice_date=FEB_10)
        session.flush()
        with session.begin_nested() as savepoint:
            with pytest.raises(ProgrammingError, match="Hoa don da cap so khong xoa duoc"):
                session.execute(text("DELETE FROM einvoices WHERE id = :id"), {"id": invoice.id})
            savepoint.rollback()

    run(work)


def test_a_serial_registered_by_another_branch_is_refused(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """C-1 — **một ký hiệu thuộc đúng một chi nhánh** (quyết định user 2026-09-07).

    Đây là thứ giữ cho dãy số và dải số cùng một trục phạm vi. Không có nó, chi
    nhánh A đăng ký `1–500` và chi nhánh B đăng ký `501–600` trên cùng ký hiệu,
    bộ đếm dùng chung bắt đầu ở 1, và B phát hành ra `00000001` — một số thuộc
    dải đã thông báo của A.

    Bài chạy dưới phạm vi hẹp của chi nhánh thứ hai, có chủ đích: người ấy
    **không nhìn thấy** hồ sơ của chi nhánh đầu (RLS), nên nếu phép kiểm đọc
    dưới phạm vi họ thì nó cho qua. Chính điều đó làm nó phải chạy ngoài RLS.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        form = ensure_invoice_form(session, form_id=8807, serial="C26TOWN")
        form_id = form.id
        ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form_id)

    second = seed_posting_context(session_factory, dataset_alpha)
    other_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(second.branch_id,),
        acting_branch_id=second.branch_id,
    )
    with unit_of_work(session_factory, other_scope) as session:
        service = InvoiceRegistrationService(session)
        assert not service.list_for_form(invoice_form_id=form_id), "RLS phải giấu hồ sơ kia"
        with pytest.raises(InvoiceFormBranchConflictError):
            service.create(
                branch_id=second.branch_id,
                invoice_form_id=form_id,
                start_date=date(2026, 1, 1),
                notice_no="TB-OWN-2",
            )


def test_an_older_range_still_covers_its_own_numbers(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Dải cũ **vẫn còn giá trị** khi có thông báo phát hành thứ hai.

    Doanh nghiệp thông báo `1–500` rồi thông báo thêm `501–600`. Bộ đếm mới tới
    số 2 thì tờ hóa đơn `00000002` là hợp lệ — nó thuộc thông báo thứ nhất. Đọc
    dải từ hồ sơ **mới nhất** sẽ từ chối đúng tờ ấy, nên phép kiểm phải hỏi
    "có hồ sơ nào phủ số này" chứ không "hồ sơ mới nhất có phủ không".
    """

    def work(session: Session) -> None:
        form = ensure_invoice_form(session, form_id=8808, serial="C26TTWO")
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=form.id,
            range_from=1,
            range_to=500,
        )
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=form.id,
            start_date=date(2026, 2, 1),
            range_from=501,
            range_to=600,
        )
        invoice = EInvoiceService(session).create_draft(
            source_voucher_id=_sales_voucher(session, context, accounts).id,
            invoice_form_id=form.id,
        )
        issued = EInvoiceService(session).issue(invoice.id, invoice_date=FEB_10)
        assert issued.invoice_no == "00000001"

    run(work)


def test_the_einvoice_tables_are_covered_by_row_level_security(
    run: Runner, context: PostingContext
) -> None:
    """Hai bảng mang `branch_id` phải có `p_branch_scope` — lỗ 7A đã phải đi vá.

    Cổng `test_rls_policy_coverage` canh chuyện này cho toàn bộ schema; bài ở
    đây khẳng định lại tại chỗ để người đọc tệp này biết hai bảng ấy có RLS mà
    không phải đi tìm.
    """

    def work(session: Session) -> None:
        rows = session.execute(
            text(
                "SELECT tablename FROM pg_policies"
                " WHERE policyname = 'p_branch_scope'"
                "   AND tablename IN ('einvoices', 'invoice_registrations')"
            )
        ).scalars()
        assert set(rows) == {"einvoices", "invoice_registrations"}

    run(work)


def test_an_invoice_cannot_point_at_a_voucher_of_another_branch(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Khóa ngoại ghép `(source_voucher_id, branch_id)` → `vouchers (id, branch_id)`.

    `branch_id` có mặt trên bảng để RLS canh được; cái giá của việc giữ hai chỗ
    một sự thật trả bằng ràng buộc này.

    Bài chạy dưới phạm vi phủ **cả hai** chi nhánh, có chủ đích: với phạm vi hẹp
    thì RLS đã từ chối dòng ấy trước, và bài sẽ xanh mà không chạm tới khóa
    ngoại — đúng kiểu "xanh vì một lớp khác đỡ hộ". Người phạm vi rộng (kế toán
    tổng hợp nhìn mọi chi nhánh) là người duy nhất đi tới được ràng buộc này,
    nên họ là người bài này phải mô phỏng.
    """
    other = seed_posting_context(session_factory, dataset_alpha)
    wide_scope = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(context.branch_id, other.branch_id),
        acting_branch_id=context.branch_id,
    )
    with unit_of_work(session_factory, wide_scope) as session:
        voucher = _sales_voucher(session, context, accounts)
        form_id = _form_id(session, context)
        session.flush()
        with session.begin_nested() as savepoint:
            with pytest.raises(IntegrityError, match="fk_einvoices_source_voucher"):
                session.execute(
                    text(
                        "INSERT INTO einvoices (id, branch_id, source_voucher_id,"
                        " invoice_form_id, status)"
                        " VALUES (gen_random_uuid(), :branch, :voucher, :form, 0)"
                    ),
                    {"branch": other.branch_id, "voucher": voucher.id, "form": form_id},
                )
            savepoint.rollback()


def test_a_voucher_with_an_issued_invoice_cannot_be_removed_by_the_database(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """`RESTRICT` là lớp chót dưới guard FR-EIV-035.

    Guard chặn ở tầng service; khóa ngoại chặn đường ghi nào lách được guard.
    Hai lớp cho một luật, và lớp dưới không biết lớp trên tồn tại.
    """

    def work(session: Session) -> None:
        voucher = _sales_voucher(session, context, accounts)
        EInvoiceService(session).create_draft(
            source_voucher_id=voucher.id, invoice_form_id=_form_id(session, context)
        )
        session.flush()
        with session.begin_nested() as savepoint:
            with pytest.raises(Exception, match="fk_einvoices_source_voucher"):
                session.execute(text("DELETE FROM vouchers WHERE id = :id"), {"id": voucher.id})
                session.flush()
            savepoint.rollback()


def test_listing_counts_every_status(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-EIV-015 — bộ lọc và số đếm về cùng một lượt gọi, nên không lệch nhau."""

    def work(session: Session) -> None:
        service = EInvoiceService(session)
        _draft(session, context, accounts)
        session.flush()
        drafts, total = service.list_by_status(statuses=[EInvoiceStatus.CHUA_PHAT_HANH])
        counts = service.count_by_status()
        assert total == counts[int(EInvoiceStatus.CHUA_PHAT_HANH)]
        assert all(EInvoiceStatus(row.status) is EInvoiceStatus.CHUA_PHAT_HANH for row in drafts)
        # Số đếm nói về TOÀN phạm vi, trang thì không — một trang một dòng vẫn
        # phải thấy đúng tổng, nếu không thẻ lọc trên UI sẽ nói dối theo trang.
        page, page_total = service.list_by_status(
            statuses=[EInvoiceStatus.CHUA_PHAT_HANH], page=1, page_size=1
        )
        assert len(page) <= 1
        assert page_total == total

    run(work)
