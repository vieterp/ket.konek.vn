"""Luồng hỏi–đáp xử lý sai sót hóa đơn (lát 7F-1, `docs/srs/07` §4.4).

Bốn kịch bản của SRS phải ra **bốn cách xử lý khác nhau**, và bảng quyết định
phải là thứ quyết định — không phải một chuỗi `if` chép lại bảng ấy vào mã. Vì
vậy bài đầu tiên duyệt cả bốn trong một phép so sánh: một nhánh gán nhầm chỉ lộ
ra khi nhìn cả bốn cạnh nhau.

**Bài đối chứng của lượt nới guard là bài quan trọng nhất tệp này.** Lát này nới
FR-EIV-035 để chứng từ gốc mở ra ở ba trạng thái cuối. Một lượt nới quá tay —
`issued_for_voucher` trả `None` cho mọi thứ — vẫn làm các bài "mở ra" xanh, và
lúc ấy kế toán sửa được chứng từ của một tờ hóa đơn đang trên đường tới cơ quan
thuế. `test_a_live_invoice_still_locks_its_voucher` là bài duy nhất bắt được
điều đó, nên nó duyệt **từng** trạng thái còn sống chứ không lấy một cái làm đại
diện.

**Bẫy thứ tự tệp lần thứ mười hai** (bài học 7E-3): ký hiệu riêng thôi chưa đủ,
`FORM_ID` cũng phải riêng — mỗi tệp dựng chi nhánh mới, nên trùng id sẽ đâm vào
`invoice_form_branch_owner` ở lượt chạy đầy đủ mà xanh khi chạy riêng.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    EInvoiceCancellationIncompleteError,
    EInvoiceErrorFlowUnknownError,
    EInvoiceRemedyNotAvailableError,
    VoucherHasIssuedInvoiceError,
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import PriceSource
from ket.modules.einvoice.error_flow import ErrorFlowService
from ket.modules.einvoice.models import (
    SUPERSEDED_STATUSES,
    EInvoice,
    EInvoiceErrorFlow,
    EInvoiceStatus,
    ErrorKind,
    ErrorNoticeKind,
    Remedy,
)
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.einvoice.state_machine import TRANSITIONS
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.contracts import VoucherService
from ket.posting.documents.models import Voucher
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAY_08 = date(2026, 5, 8)
CUSTOMER_ID = 9751
SALESPERSON_ID = 9752
FORM_ID = 8861
SERIAL = "C26TERR"

NOTICE_NO = "04SS-001"


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
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7F-ERR")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7F-ERR")
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


def _payload(context: PostingContext, accounts: dict[str, int]) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=MAY_08,
        posting_date=MAY_08,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        description="bán hàng cho bài xử lý sai sót",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(1),
                unit_price_fc=Decimal(300_000),
                amount_fc=Decimal(300_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(30_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )


def _issued_invoice(
    session: Session, context: PostingContext, accounts: dict[str, int]
) -> tuple[Voucher, EInvoice]:
    """Chứng từ bán kèm một hóa đơn đã ở `DA_PHAT_HANH` — điểm vào của mọi lượt xử lý."""
    voucher = SalesInvoiceService(session).create(_payload(context, accounts), user_id=ACTOR_ID)
    form = ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
    ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
    service = EInvoiceService(session)
    invoice = service.create_draft(source_voucher_id=voucher.id, invoice_form_id=form.id)
    service.issue(invoice.id, invoice_date=MAY_08)
    service.confirm(invoice.id, tax_authority_code="MCQT-7F", lookup_code="TRA-7F")
    session.flush()
    return voucher, invoice


def _submit_cancellation_documents(session: Session, einvoice_id: UUID) -> None:
    """Hai văn bản BR-EIV-04, đã nộp — điều kiện để nhánh HỦY chạy được."""
    service = EInvoiceService(session)
    for kind, number in (
        (ErrorNoticeKind.THONG_BAO_HUY, "TBH-001"),
        (ErrorNoticeKind.BIEN_BAN_HUY, "BBH-001"),
    ):
        notice = service.add_notice(
            einvoice_id,
            kind=kind,
            notice_no=number,
            notice_date=MAY_08,
        )
        service.submit_notice(notice.id)


# --- Bảng quyết định ------------------------------------------------------


def test_the_four_scenarios_resolve_to_four_remedies(run: Runner) -> None:
    """Tiêu chí nghiệm thu của lát: bốn kịch bản → bốn cách xử lý (U4).

    Duyệt cả bốn trong một phép so sánh chứ không bốn bài rời: cái sai hay gặp ở
    một bảng quyết định là **hai nhánh trỏ cùng một chỗ**, và điều đó chỉ lộ ra
    khi nhìn cả bốn cạnh nhau.
    """

    def work(session: Session) -> None:
        flows = ErrorFlowService(session)
        resolved = {
            (ErrorKind.SAI_THONG_TIN, False): Remedy.THAY_THE,
            (ErrorKind.SAI_THONG_TIN, True): Remedy.DIEU_CHINH_THONG_TIN,
            (ErrorKind.SAI_SO_TIEN, False): Remedy.THAY_THE,
            (ErrorKind.SAI_SO_TIEN, True): Remedy.DIEU_CHINH_TIEN,
            (ErrorKind.KHONG_PHAT_SINH, None): Remedy.HUY,
        }
        actual = {
            answers: Remedy(flows.resolve(error_kind=answers[0], buyer_declared=answers[1]).remedy)
            for answers in resolved
        }
        assert actual == resolved
        # Bốn *cách xử lý* phân biệt, dù năm bộ câu trả lời.
        assert len(set(actual.values())) == 4

    run(work)


def test_the_no_transaction_branch_stops_after_the_first_question(run: Runner) -> None:
    """ "Không phát sinh giao dịch" thì thôi hỏi "khách đã kê khai chưa".

    Và câu trả lời ấy đọc từ **bảng**, không từ một hằng số "hai loại đầu thì
    hỏi" trong mã — đó lại là bảng quyết định chép lần thứ hai.
    """

    def work(session: Session) -> None:
        flows = ErrorFlowService(session)
        assert flows.asks_whether_buyer_declared(ErrorKind.SAI_THONG_TIN)
        assert flows.asks_whether_buyer_declared(ErrorKind.SAI_SO_TIEN)
        assert not flows.asks_whether_buyer_declared(ErrorKind.KHONG_PHAT_SINH)

    run(work)


def test_an_unanswered_second_question_is_an_error_not_a_default(run: Runner) -> None:
    """Thiếu câu trả lời thứ hai phải **đỏ**, không rơi vào nhánh nào.

    Nếu nó lặng lẽ chọn "chưa kê khai" thì một tờ hóa đơn khách **đã** kê khai
    sẽ bị thay thế — tức bị rút khỏi kỳ thuế của người mua.
    """

    def work(session: Session) -> None:
        with pytest.raises(EInvoiceErrorFlowUnknownError):
            ErrorFlowService(session).resolve(error_kind=ErrorKind.SAI_SO_TIEN, buyer_declared=None)

    run(work)


def test_two_rows_cannot_answer_the_same_question(run: Runner) -> None:
    """`UNIQUE NULLS NOT DISTINCT` — kể cả ở nhánh không hỏi câu thứ hai.

    `UNIQUE` thường của PostgreSQL coi hai `NULL` là khác nhau, nên **chính
    nhánh `KHONG_PHAT_SINH` mới là chỗ thủng** nếu thiếu `NULLS NOT DISTINCT`:
    hai dòng nói ngược nhau lọt vào bảng và phép tra trả về dòng nào là tùy
    planner. Bài này gieo đúng dòng thứ hai ấy.
    """

    def work(session: Session) -> None:
        # SAVEPOINT quanh lệnh ghi hỏng: một `IntegrityError` làm hỏng cả
        # transaction, nên thiếu nó thì `unit_of_work` của `run` đâm vào lượt
        # COMMIT trên một transaction đã abort — bài đỏ vì lý do khác lý do nó
        # viết ra. Cùng khuôn `test_einvoice_source_guard`.
        with (
            pytest.raises(IntegrityError, match="uq_einvoice_error_flows_answers"),
            session.begin_nested(),
        ):
            session.add(
                EInvoiceErrorFlow(
                    code="NO_TRANSACTION_DUPLICATE",
                    error_kind=ErrorKind.KHONG_PHAT_SINH,
                    buyer_declared=None,
                    remedy=Remedy.THAY_THE,
                    legal_basis="dòng mâu thuẫn cố ý",
                )
            )
            session.flush()

    run(work)


# --- Thi hành: thay thế ---------------------------------------------------


def test_replacing_supersedes_the_old_invoice_and_drafts_a_new_one(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """FR-EIV-030 đầu-cuối: tờ cũ `DA_THAY_THE`, tờ mới trỏ ngược về nó.

    Tờ mới nằm trên **chính chứng từ cũ** — hình dạng mà chỉ mục riêng phần
    `uq_einvoices_live_source_voucher` của 7D đã chừa sẵn chỗ bằng cách loại ba
    trạng thái cuối. Nếu tờ cũ không rời `DA_PHAT_HANH` trước thì chỉ mục ấy
    chặn tờ thứ hai, nên bài này cũng là bài ghim **thứ tự** hai nửa.
    """

    def work(session: Session) -> None:
        voucher, invoice = _issued_invoice(session, context, accounts)
        outcome = ErrorFlowService(session).apply(
            invoice.id,
            error_kind=ErrorKind.SAI_SO_TIEN,
            buyer_declared=False,
            notice_no=NOTICE_NO,
            notice_date=MAY_08,
        )
        assert Remedy(outcome.flow.remedy) is Remedy.THAY_THE

        session.refresh(invoice)
        assert EInvoiceStatus(invoice.status) is EInvoiceStatus.DA_THAY_THE

        replacement = outcome.replacement
        assert replacement is not None
        assert replacement.replaces_invoice_id == invoice.id
        assert replacement.source_voucher_id == voucher.id
        assert EInvoiceStatus(replacement.status) is EInvoiceStatus.CHUA_PHAT_HANH
        # Tờ mới chưa mang số: nó là bản nháp, và số chỉ cấp lúc phát hành.
        assert replacement.invoice_no is None

    run(work)


def test_the_replacement_can_be_issued(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Tờ thay thế phát hành được — nếu không thì lượt "thay thế" là ngõ cụt.

    Nó lấy **số mới**, không dùng lại số cũ: ADR-013 giữ số cho tờ bị từ chối
    phát hành lại, còn đây là một tờ hóa đơn khác.
    """

    def work(session: Session) -> None:
        _, invoice = _issued_invoice(session, context, accounts)
        old_number = invoice.invoice_no
        outcome = ErrorFlowService(session).apply(
            invoice.id,
            error_kind=ErrorKind.SAI_THONG_TIN,
            buyer_declared=False,
            notice_no=NOTICE_NO,
            notice_date=MAY_08,
        )
        assert outcome.replacement is not None
        issued = EInvoiceService(session).issue(outcome.replacement.id, invoice_date=MAY_08)
        assert issued.invoice_no is not None
        assert issued.invoice_no != old_number

    run(work)


# --- Thi hành: hủy --------------------------------------------------------


def test_cancelling_through_the_wizard_still_needs_both_documents(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """BR-EIV-04 không nới ra vì đi qua wizard.

    Nhánh HỦY gọi lại chính `cancel` của 7D chứ không dựng đường ghi thứ hai —
    và bài này là thứ chứng minh nó không dựng.
    """

    def work(session: Session) -> None:
        _, invoice = _issued_invoice(session, context, accounts)
        with pytest.raises(EInvoiceCancellationIncompleteError):
            ErrorFlowService(session).apply(
                invoice.id,
                error_kind=ErrorKind.KHONG_PHAT_SINH,
                buyer_declared=None,
                notice_no=NOTICE_NO,
                notice_date=MAY_08,
            )

    run(work)


def test_cancelling_with_both_documents_succeeds(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Đủ hai văn bản đã nộp thì nhánh HỦY chạy trọn và lập thông báo sai sót."""

    def work(session: Session) -> None:
        _, invoice = _issued_invoice(session, context, accounts)
        _submit_cancellation_documents(session, invoice.id)
        outcome = ErrorFlowService(session).apply(
            invoice.id,
            error_kind=ErrorKind.KHONG_PHAT_SINH,
            buyer_declared=None,
            notice_no=NOTICE_NO,
            notice_date=MAY_08,
        )
        assert Remedy(outcome.flow.remedy) is Remedy.HUY
        assert outcome.replacement is None
        assert outcome.notice.kind is ErrorNoticeKind.THONG_BAO_SAI_SOT
        # Mã nhánh lưu lại trên văn bản — vết giải trình "vì sao xử lý kiểu này".
        assert outcome.notice.reason_code == outcome.flow.code

        session.refresh(invoice)
        assert EInvoiceStatus(invoice.status) is EInvoiceStatus.DA_HUY

    run(work)


def test_the_error_notice_does_not_satisfy_the_cancellation_pair(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Thông báo sai sót **không** đếm vào bộ văn bản hủy (`CANCELLATION_KINDS`).

    Đây đúng là lý do 7D tách sẵn hằng số ấy ra. Loại thứ ba dùng chung bảng, nên
    một phép đếm "mọi văn bản của hóa đơn này" sẽ mở khóa lượt hủy khi mới có
    thông báo sai sót + biên bản hủy — thiếu hẳn thông báo gửi cơ quan thuế.
    """

    def work(session: Session) -> None:
        _, invoice = _issued_invoice(session, context, accounts)
        service = EInvoiceService(session)
        notice = service.add_notice(
            invoice.id,
            kind=ErrorNoticeKind.THONG_BAO_SAI_SOT,
            notice_no=NOTICE_NO,
            notice_date=MAY_08,
        )
        service.submit_notice(notice.id)
        record = service.add_notice(
            invoice.id,
            kind=ErrorNoticeKind.BIEN_BAN_HUY,
            notice_no="BBH-002",
            notice_date=MAY_08,
        )
        service.submit_notice(record.id)

        with pytest.raises(EInvoiceCancellationIncompleteError) as raised:
            service.cancel(invoice.id)
        assert raised.value.details["missing"] == str(int(ErrorNoticeKind.THONG_BAO_HUY))

    run(work)


# --- Nhánh chưa thi hành được ---------------------------------------------


def test_an_adjustment_reports_the_right_remedy_but_refuses_to_run(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Điều chỉnh: `resolve` đúng, `apply` từ chối bằng mã lỗi riêng.

    Không phải `500` và không phải một hóa đơn nửa vời: hóa đơn điều chỉnh mang
    **phần chênh**, nên nó cần một chứng từ bán mang phần chênh (phân hệ bán
    hàng, lát sau). Thông điệp phải nói ra cách xử lý đúng, vì kế toán vẫn phải
    làm việc ấy — bằng tay, ở bản này.
    """

    def work(session: Session) -> None:
        _, invoice = _issued_invoice(session, context, accounts)
        flows = ErrorFlowService(session)
        flow = flows.resolve(error_kind=ErrorKind.SAI_SO_TIEN, buyer_declared=True)
        assert Remedy(flow.remedy) is Remedy.DIEU_CHINH_TIEN

        with pytest.raises(EInvoiceRemedyNotAvailableError) as raised:
            flows.apply(
                invoice.id,
                error_kind=ErrorKind.SAI_SO_TIEN,
                buyer_declared=True,
                notice_no=NOTICE_NO,
                notice_date=MAY_08,
            )
        assert raised.value.details["remedy"] == int(Remedy.DIEU_CHINH_TIEN)

        # Và nó **không** để lại gì: không đổi trạng thái, không lập văn bản.
        session.refresh(invoice)
        assert EInvoiceStatus(invoice.status) is EInvoiceStatus.DA_PHAT_HANH

    run(work)


# --- FR-EIV-035: nhả chứng từ ở ba trạng thái cuối -------------------------


def test_a_replaced_invoice_releases_its_voucher(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Sau khi thay thế, chứng từ gốc sửa được — nếu không thì "sai số tiền ⇒
    THAY THẾ" là ngõ cụt: số tiền đúng nằm trên chứng từ, không trên hóa đơn."""

    def work(session: Session) -> None:
        voucher, invoice = _issued_invoice(session, context, accounts)
        ErrorFlowService(session).apply(
            invoice.id,
            error_kind=ErrorKind.SAI_SO_TIEN,
            buyer_declared=False,
            notice_no=NOTICE_NO,
            notice_date=MAY_08,
        )
        corrected = _payload(context, accounts).model_copy(
            update={"description": "sửa lại sau khi thay thế hóa đơn"}
        )
        SalesInvoiceService(session).update(
            voucher.id,
            corrected,
            expected_row_version=voucher.row_version,
            user_id=ACTOR_ID,
        )

    run(work)


def test_a_cancelled_invoice_releases_its_voucher_for_unposting(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Sau khi hủy, chứng từ khống **bỏ ghi sổ** được — nhưng vẫn không xóa được.

    Kịch bản "không phát sinh giao dịch" cần đảo tác động kế toán của một chứng
    từ lẽ ra không tồn tại, và lượt bỏ ghi sổ làm đúng việc ấy: guard FR-EIV-035
    nhả ra, nên đường này mở.

    **Xóa thì không, và đó là đúng.** `fk_einvoices_source_voucher` khai
    `RESTRICT`, nên chừng nào tờ hóa đơn còn thì chứng từ gốc còn. Tờ hóa đơn ấy
    *phải* còn: nó đã cấp số, số đã tiêu (ADR-013), và nó nằm trong nghĩa vụ lưu
    trữ mười năm — một tờ hóa đơn đã hủy không có chứng từ gốc là một chỗ trống
    không giải trình được. Nói cách khác, lượt nới guard của lát này trả lại
    **sửa** và **bỏ ghi sổ**, không trả lại **xóa**; khóa ngoại là lớp canh cuối.
    """

    def work(session: Session) -> None:
        voucher, invoice = _issued_invoice(session, context, accounts)
        _submit_cancellation_documents(session, invoice.id)
        SalesInvoiceService(session).post(voucher.id, user_id=ACTOR_ID)
        session.flush()
        ErrorFlowService(session).apply(
            invoice.id,
            error_kind=ErrorKind.KHONG_PHAT_SINH,
            buyer_declared=None,
            notice_no=NOTICE_NO,
            notice_date=MAY_08,
        )
        # Cửa mà lát này mở: bỏ ghi sổ không còn bị guard chặn.
        SalesInvoiceService(session).unpost(voucher.id, user_id=ACTOR_ID)
        session.flush()

        # Cửa vẫn đóng, và đóng bằng khóa ngoại chứ không bằng guard.
        with (
            pytest.raises(IntegrityError, match="fk_einvoices_source_voucher"),
            session.begin_nested(),
        ):
            VoucherService(session).delete(voucher.id)
            session.flush()

    run(work)


@pytest.mark.parametrize(
    "status",
    [
        EInvoiceStatus.DANG_PHAT_HANH,
        EInvoiceStatus.PHAT_HANH_LOI,
        EInvoiceStatus.DA_PHAT_HANH,
        EInvoiceStatus.DA_GUI,
    ],
)
def test_a_live_invoice_still_locks_its_voucher(
    run: Runner,
    context: PostingContext,
    accounts: dict[str, int],
    status: EInvoiceStatus,
) -> None:
    """**Bài đối chứng của lượt nới guard** — xem docstring đầu tệp.

    Duyệt từng trạng thái còn sống chứ không lấy một cái làm đại diện: lượt nới
    của lát này thao tác trên một tập trạng thái, và một tập viết sai (ví dụ
    loại nhầm `DA_GUI` vì nó cũng "đã xong việc") sẽ nhả chứng từ của một tờ hóa
    đơn đã tới tay người mua.
    """

    def work(session: Session) -> None:
        voucher, invoice = _issued_invoice(session, context, accounts)
        # Đặt thẳng trạng thái: bài này đo **ranh giới của guard**, không đo
        # đường đi tới từng trạng thái (những đường ấy có bài riêng ở 7D/7E).
        invoice.status = status
        session.flush()
        with pytest.raises(VoucherHasIssuedInvoiceError):
            VoucherService(session).delete(voucher.id)

    run(work)


def test_only_the_superseding_statuses_are_released() -> None:
    """Ranh giới khai ở `SUPERSEDED_STATUSES` khớp đúng ba trạng thái cuối.

    Ghim bằng metadata chứ không bằng ba con số chép tay: bảng chuyển trạng thái
    và chỉ mục riêng phần `uq_einvoices_live_source_voucher` cũng nói về đúng bộ
    ba này, nên một trạng thái mới mọc thêm ở lát sau phải làm bài này đỏ.
    """
    assert SUPERSEDED_STATUSES == {
        EInvoiceStatus.DA_THAY_THE,
        EInvoiceStatus.DA_DIEU_CHINH,
        EInvoiceStatus.DA_HUY,
    }
    # Và không trạng thái nào trong bộ ấy còn cạnh ra — nếu có thì "đã xử lý
    # xong sai sót" thôi là sự thật, và guard nhả chứng từ quá sớm.
    assert not [key for key in TRANSITIONS if key[0] in SUPERSEDED_STATUSES]


def test_the_seeded_table_covers_every_error_kind(run: Runner) -> None:
    """Mỗi loại sai sót phải có ít nhất một nhánh — không loại nào là ngõ cụt."""

    def work(session: Session) -> None:
        covered = set(
            session.scalars(
                select(EInvoiceErrorFlow.error_kind).where(EInvoiceErrorFlow.is_active)
            ).all()
        )
        assert covered == {int(kind) for kind in ErrorKind}

    run(work)
