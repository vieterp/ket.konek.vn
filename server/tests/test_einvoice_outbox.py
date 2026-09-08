"""Hàng đợi truyền tải hóa đơn trên PostgreSQL thật — lát 7E-1.

Tệp này đo **một** thứ, từ nhiều phía: hệ thống không bao giờ phát hành một tờ
hóa đơn hai lần, kể cả khi mạng đứt giữa chừng hoặc worker chết ngay sau khi
nhà cung cấp đã nhận.

Bốn nhóm bài:

* **Giao dịch tới hạn** — cấp số và xếp dòng cùng commit hoặc cùng không, và
  một hóa đơn chỉ có một dòng còn mở.
* **Ngắt mạng** — kết quả `UNKNOWN` đưa dòng về `needs_reconcile`, tờ hóa đơn
  giữ nguyên số và trạng thái. Lượt sau **hỏi trước**, và khi nhà cung cấp nói
  "tôi nhận rồi" thì không có lượt gửi thứ hai nào.
* **Worker chết** — lease hết hạn dẫn tới đúng nhánh hỏi-trước, dù không đường
  ghi nào kịp đánh dấu gì.
* **RLS** — dòng hàng đợi của chi nhánh khác không nhìn thấy được.

Nhà cung cấp trong các bài là `_ScriptedProvider`: nó **đếm** số lượt `issue`
và `query_status`, vì "không gửi lần hai" là thứ chỉ chứng minh được bằng cách
đếm lượt gọi — nhìn trạng thái cuối cùng thì một lượt gửi thừa đã bị nuốt mất.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.pricing import PriceSource
from ket.kernel.security.models import Branch
from ket.modules.einvoice.models import (
    EInvoice,
    EInvoiceOutbox,
    EInvoiceStatus,
    OutboxOperation,
    OutboxStatus,
)
from ket.modules.einvoice.outbox import (
    LEASE_DURATION,
    claim_due,
    due_now,
    enqueue_issue,
    load_for_send,
    open_rows_for,
)
from ket.modules.einvoice.providers.contracts import (
    IssueOutcome,
    ProviderAcceptance,
    ProviderStatus,
)
from ket.modules.einvoice.providers.internal import INTERNAL_PROVIDER_CODE
from ket.modules.einvoice.reconcile import transmit
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
FEB_10 = date(2026, 2, 10)

CUSTOMER_ID = 9711
SALESPERSON_ID = 9712

FORM_ID = 8841
SERIAL = "C26TOB"
"""Ký hiệu riêng của tệp này — dãy số là trạng thái toàn dataset, xem
`einvoice_support`."""


class _ScriptedProvider:
    """Nhà cung cấp đọc kịch bản, và **đếm** mọi lượt gọi.

    Bộ đếm là công cụ đo chính của tệp: luật RT-10 nói "không gửi lại khi chưa
    hỏi", và câu ấy chỉ kiểm được bằng `issue_calls`, không bằng trạng thái cuối.
    """

    def __init__(
        self,
        *,
        issue_outcomes: list[IssueOutcome],
        query_results: list[ProviderStatus] | None = None,
    ) -> None:
        self._issue_outcomes = issue_outcomes
        self._query_results = query_results or []
        self.issue_calls: list[UUID] = []
        self.query_calls: list[UUID] = []

    def issue(self, *, client_ref: UUID, invoice_id: UUID) -> IssueOutcome:
        self.issue_calls.append(client_ref)
        index = min(len(self.issue_calls) - 1, len(self._issue_outcomes) - 1)
        return self._issue_outcomes[index]

    def query_status(self, *, client_ref: UUID) -> ProviderStatus:
        self.query_calls.append(client_ref)
        if not self._query_results:
            return ProviderStatus(known=False)
        index = min(len(self.query_calls) - 1, len(self._query_results) - 1)
        return self._query_results[index]


ACCEPTED = IssueOutcome(
    acceptance=ProviderAcceptance.ACCEPTED,
    provider_ref="NCC-001",
    tax_authority_code="M1-22-ABC",
    lookup_code="TRA-CUU-1",
)
LOST_SIGNAL = IssueOutcome(
    acceptance=ProviderAcceptance.UNKNOWN, message="Hết thời gian chờ khi gửi"
)
REFUSED = IssueOutcome(acceptance=ProviderAcceptance.REJECTED, message="Sai ký hiệu hóa đơn")


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
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7E-01")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7E-01")
    return account_ids


@pytest.fixture(scope="module")
def other_branch_id(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> int:
    """Một chi nhánh **khác** chi nhánh của bài, không tạo thêm dòng nào nếu tránh được.

    Hai luật của bộ test này gặp nhau ở đây. `dataset_alpha` dùng chung cả
    phiên, và hai tệp khác cài một `autouse` chốt **số lượng chi nhánh** ở bài
    đầu rồi đòi nó đứng yên — nên tạo chi nhánh giữa một bài làm đỏ những tệp
    chẳng liên quan gì (bản đầu của tệp này làm đúng thế: 180 bài đỏ). Còn
    `ensure_second_branch` thì chỉ hứa "có ≥ 2 chi nhánh", không hứa chi nhánh
    nó trả về khác chi nhánh của bài — đúng bẫy review 7A đã bắt.

    Nên: **tìm** một chi nhánh khác trong dữ liệu sẵn có (chạy cả bộ thì luôn
    có), chỉ tạo khi thật sự không có, và tạo ở phạm vi **module** đúng như
    thông điệp của cổng ấy chỉ dẫn. `assert` cuối giữ lời hứa mà tên hàm đưa ra.
    """
    seed = RequestScope(dataset_schema=dataset_alpha.schema_name, user_id=ACTOR_ID, branch_ids=())
    with unit_of_work(session_factory, seed) as session:
        found = session.scalars(
            select(Branch.id).where(Branch.id != context.branch_id).order_by(Branch.id).limit(1)
        ).one_or_none()
        if found is None:
            code = f"PB{uuid4().hex[:6].upper()}"
            found = BranchService(session).create(code=code, name=f"Chi nhánh {code}").id
    assert found != context.branch_id
    return found


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


def _sales_voucher(session: Session, context: PostingContext, accounts: dict[str, int]) -> UUID:
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
        description="bán hàng cho hàng đợi truyền tải",
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
    return SalesInvoiceService(session).create(payload, user_id=ACTOR_ID).id


def _queued(session: Session, context: PostingContext, accounts: dict[str, int]) -> EInvoiceOutbox:
    """Một hóa đơn đã cấp số và đã nằm trong hàng đợi — điểm xuất phát chung."""
    form = ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
    ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
    voucher_id = _sales_voucher(session, context, accounts)
    draft = EInvoiceService(session).create_draft(
        source_voucher_id=voucher_id, invoice_form_id=form.id
    )
    _invoice, row = enqueue_issue(
        session,
        draft.id,
        invoice_date=FEB_10,
        provider_code=INTERNAL_PROVIDER_CODE,
    )
    return row


def _age_lease(session: Session, row: EInvoiceOutbox, *, by: timedelta) -> None:
    """Đẩy lease lùi về quá khứ — mô phỏng worker chết mà không phải chờ thật."""
    row.in_flight_since = datetime.now(UTC) - by
    session.flush()


# --- Giao dịch tới hạn ------------------------------------------------------


def test_a_queued_invoice_carries_its_number_and_a_live_lease(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cấp số và xếp hàng là **một** thao tác (RT-10).

    Không có khoảnh khắc nào tồn tại một số hóa đơn đã tiêu mà chưa nằm trong
    hàng đợi — kiểm bằng cách đọc cả hai vế ngay sau một lượt xếp hàng.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        invoice = session.get(EInvoice, row.einvoice_id)
        assert invoice is not None
        assert invoice.invoice_no is not None
        assert EInvoiceStatus(invoice.status) == EInvoiceStatus.DANG_PHAT_HANH
        assert OutboxStatus(row.status) == OutboxStatus.IN_FLIGHT
        assert row.in_flight_since is not None
        assert row.operation == OutboxOperation.ISSUE
        assert row.branch_id == invoice.branch_id

    run(work)


def test_a_second_open_row_for_the_same_invoice_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Một dòng còn mở cho mỗi (hóa đơn, việc) — chỉ mục bán phần của `0033`.

    Không có nó, hai lượt bấm "Phát hành" sinh hai `client_ref`, và hai
    `client_ref` là **hai tờ hóa đơn** dưới mắt nhà cung cấp.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        session.add(
            EInvoiceOutbox(
                einvoice_id=row.einvoice_id,
                branch_id=row.branch_id,
                provider_code=INTERNAL_PROVIDER_CODE,
                operation=OutboxOperation.ISSUE,
                client_ref=UUID(int=0x7E01),
                status=OutboxStatus.IN_FLIGHT,
                attempt_count=1,
                in_flight_since=datetime.now(UTC),
                next_attempt_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        session.flush()

    with pytest.raises(IntegrityError, match="uq_einvoice_outbox_open_operation"):
        run(work)


def test_an_in_flight_row_without_a_lease_is_not_representable(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """`lease_only_while_in_flight` — dòng bay mà không lease thì treo vĩnh viễn.

    Không lượt quét nào đòi lại được nó, đúng như dòng `jobs` kẹt "đang chạy"
    mà reaper của `kernel.jobs` sinh ra để chặn.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        row.in_flight_since = None
        session.flush()

    with pytest.raises(IntegrityError, match="lease_only_while_in_flight"):
        run(work)


# --- Ngắt mạng --------------------------------------------------------------


def test_a_lost_signal_parks_the_row_and_leaves_the_invoice_alone(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Không rõ kết quả ≠ thất bại.

    Tờ hóa đơn giữ nguyên số và vẫn `DANG_PHAT_HANH`, vì đó **chính xác** là
    điều hệ thống biết. Đánh nó thành lỗi ở đây là nói dối theo chiều nguy hiểm:
    người dùng sẽ phát hành lại một tờ nhà cung cấp có thể đã nhận.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        number_before = session.get(EInvoice, row.einvoice_id).invoice_no  # type: ignore[union-attr]
        provider = _ScriptedProvider(issue_outcomes=[LOST_SIGNAL])
        mine = load_for_send(session, row.id, force_reconcile=False)
        assert mine is not None
        assert mine.must_reconcile is False, "lượt đầu chưa gửi bao giờ, không phải hỏi"

        report = transmit(session, mine, provider)

        assert report.status == OutboxStatus.NEEDS_RECONCILE
        assert report.sent is True
        invoice = session.get(EInvoice, row.einvoice_id)
        assert invoice is not None
        assert invoice.invoice_no == number_before
        assert EInvoiceStatus(invoice.status) == EInvoiceStatus.DANG_PHAT_HANH
        assert row.next_attempt_at is not None
        assert row.last_error == "Hết thời gian chờ khi gửi"

    run(work)


def test_a_provider_that_already_has_it_never_receives_a_second_send(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Bài trung tâm của lát này (RT-10, tiêu chí `needs_reconcile`).

    Kịch bản: gửi đi, nhà cung cấp nhận, câu trả lời rơi mất. Lượt bơm kế tiếp
    **phải** hỏi trước, thấy "tôi nhận rồi", và kết luận bằng chính kết quả ấy —
    `issue` đúng **một** lần trong cả hai lượt.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        provider = _ScriptedProvider(
            issue_outcomes=[LOST_SIGNAL],
            query_results=[ProviderStatus(known=True, outcome=ACCEPTED)],
        )

        first = load_for_send(session, row.id, force_reconcile=False)
        assert first is not None
        transmit(session, first, provider)
        assert row.status == OutboxStatus.NEEDS_RECONCILE

        # Tới hạn thử lại.
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()

        second = next(item for item in claim_due(session, limit=10) if item.row.id == row.id)
        assert second.must_reconcile is True
        report = transmit(session, second, provider)

        assert report.asked_first is True
        assert report.sent is False, "hỏi ra là đã nhận rồi thì không được gửi nữa"
        assert len(provider.issue_calls) == 1, "một tờ hóa đơn, một lượt gửi"
        assert len(provider.query_calls) == 1
        assert row.status == OutboxStatus.DONE
        assert row.provider_ref == "NCC-001"
        invoice = session.get(EInvoice, row.einvoice_id)
        assert invoice is not None
        assert EInvoiceStatus(invoice.status) == EInvoiceStatus.DA_PHAT_HANH
        assert invoice.tax_authority_code == "M1-22-ABC"

    run(work)


def test_a_provider_that_never_got_it_receives_the_same_client_ref(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Chiều còn lại: chưa nhận thì gửi lại — **đúng** `client_ref` cũ.

    Một `client_ref` mới ở lượt hai sẽ vô hiệu hóa toàn bộ cơ chế chống trùng
    phía nhà cung cấp, nên bài này ghim chính giá trị ấy chứ không chỉ ghim
    trạng thái cuối.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        client_ref = row.client_ref
        provider = _ScriptedProvider(
            issue_outcomes=[LOST_SIGNAL, ACCEPTED],
            query_results=[ProviderStatus(known=False)],
        )

        first = load_for_send(session, row.id, force_reconcile=False)
        assert first is not None
        transmit(session, first, provider)
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.flush()

        second = next(item for item in claim_due(session, limit=10) if item.row.id == row.id)
        report = transmit(session, second, provider)

        assert report.asked_first is True
        assert report.sent is True
        assert provider.issue_calls == [client_ref, client_ref]
        assert row.client_ref == client_ref, "khóa chống trùng không được đổi giữa chừng"
        assert row.status == OutboxStatus.DONE

    run(work)


def test_a_refusal_keeps_the_number_and_stops_retrying(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Từ chối là câu trả lời **rõ ràng** — không lùi lịch gửi lại.

    Số hóa đơn vẫn giữ (ADR-013): đường ra là phát hành lại hoặc lập biên bản
    hủy số, không phải trả số về dãy.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        number_before = session.get(EInvoice, row.einvoice_id).invoice_no  # type: ignore[union-attr]
        provider = _ScriptedProvider(issue_outcomes=[REFUSED])

        claimed = load_for_send(session, row.id, force_reconcile=False)
        assert claimed is not None
        transmit(session, claimed, provider)

        assert row.status == OutboxStatus.FAILED
        assert row.next_attempt_at is None, "không có lượt thử lại nào được lên lịch"
        invoice = session.get(EInvoice, row.einvoice_id)
        assert invoice is not None
        assert invoice.invoice_no == number_before
        assert EInvoiceStatus(invoice.status) == EInvoiceStatus.PHAT_HANH_LOI
        assert not open_rows_for(session, row.einvoice_id), "dòng đã đóng, hàng đợi buông nó"

    run(work)


# --- Worker chết ------------------------------------------------------------


def test_an_expired_lease_forces_the_ask_first_branch(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Worker chết giữa lượt gửi — không đường ghi nào kịp đánh dấu gì.

    Đây là lý do câu hỏi an toàn đọc **lease** chứ không một bộ đếm: bộ đếm phải
    được ghi trước lượt gọi mạng, mà thân job thì không tự commit được. Lease
    thì đã nằm sẵn trên dòng từ lúc xếp hàng.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        _age_lease(session, row, by=LEASE_DURATION + timedelta(minutes=1))

        claimed = next(item for item in claim_due(session, limit=10) if item.row.id == row.id)

        assert claimed.must_reconcile is True
        assert row.status == OutboxStatus.IN_FLIGHT, "lease được làm mới cho lượt này"

    run(work)


def test_a_live_lease_is_left_to_its_owner(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Bài đối chứng: lease còn hạn thì worker khác **không** đụng vào.

    Không có nó, bài trên vẫn xanh với một `claim_due` giành mọi thứ — và hai
    worker sẽ cùng gửi một tờ hóa đơn.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        claimed_ids = [item.row.id for item in claim_due(session, limit=50)]
        assert row.id not in claimed_ids

    run(work)


def test_due_now_counts_what_the_pump_would_take(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Con số panel vận hành đọc phải là **cùng** tập mà bộ bơm sẽ giành.

    Hai câu truy vấn rời nhau cho hai câu trả lời khác nhau là cách một màn hình
    nói "không có gì chờ" trong khi hàng đợi đang tắc.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        # Đo bằng **hiệu**, không bằng số tuyệt đối: các bài trước trong tệp để
        # lại dòng `needs_reconcile` đã tới hạn, nên một con số tuyệt đối biến
        # bài này thành bài phụ thuộc thứ tự chạy — đúng bẫy đã bắt bảy lần.
        before = due_now(session)
        _age_lease(session, row, by=LEASE_DURATION + timedelta(minutes=1))
        assert due_now(session) == before + 1

    run(work)


# --- RLS --------------------------------------------------------------------


def test_the_queue_is_invisible_from_another_branch(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    other_branch_id: int,
) -> None:
    """Dòng hàng đợi của chi nhánh khác không đọc được (`0033` bật RLS).

    Bảng mang `branch_id` nên cổng `test_rls_policy_coverage` đòi nó có policy;
    bài này đo policy ấy **có tác dụng thật**, chứ không chỉ có mặt.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        row = _queued(session, context, accounts)
        outbox_id = row.id

    narrowed = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(other_branch_id,),
        acting_branch_id=other_branch_id,
    )
    with unit_of_work(session_factory, narrowed) as session:
        found = session.scalars(
            select(EInvoiceOutbox).where(EInvoiceOutbox.id == outbox_id)
        ).one_or_none()
        assert found is None


def test_a_rolled_back_attempt_still_forces_the_ask_first_branch(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hàng rào của RT-10 khi **lease không cứu được** (review 7E-1, C-1).

    Trình tự thật: worker gửi tờ hóa đơn, nhà cung cấp nhận, rồi worker mất
    lease **job** ngay trước commit — `_fence_before_commit` ném và cả thân job
    rollback, kéo theo lượt làm mới lease của `load_for_send`. Dòng quay về với
    lease do endpoint đặt, còn nguyên hạn, trông y như chưa ai gửi.

    `jobs.attempt` là thứ duy nhất sống sót qua lượt rollback ấy (hàng đợi tăng
    nó trong transaction của chính nó), nên nó — chứ không phải lease — là hàng
    rào. Bài này mô phỏng đúng lượt chạy thứ hai: lease còn hạn, mà vẫn phải hỏi.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        provider = _ScriptedProvider(
            issue_outcomes=[ACCEPTED],
            query_results=[ProviderStatus(known=True, outcome=ACCEPTED)],
        )

        retry = load_for_send(session, row.id, force_reconcile=True)
        assert retry is not None
        assert retry.must_reconcile is True, (
            "lease còn hạn không được kết luận là chưa gửi ở lượt chạy thứ hai"
        )
        report = transmit(session, retry, provider)

        assert report.sent is False
        assert provider.issue_calls == [], "lượt chạy lại không được gửi mù"
        assert provider.query_calls == [row.client_ref]
        assert row.status == OutboxStatus.DONE

    run(work)


class _Exploding:
    """Adapter ném ở cả hai cửa — thư viện HTTP báo hết giờ bằng cách ném."""

    def issue(self, *, client_ref: UUID, invoice_id: UUID) -> IssueOutcome:
        raise TimeoutError("mạng rớt giữa chừng")

    def query_status(self, *, client_ref: UUID) -> ProviderStatus:
        raise TimeoutError("mạng rớt giữa chừng")


class _LookupBroken:
    """Gửi được, nhưng cửa tra cứu hỏng — nhánh nguy hiểm hơn của cùng vấn đề."""

    def __init__(self) -> None:
        self.issue_calls = 0

    def issue(self, *, client_ref: UUID, invoice_id: UUID) -> IssueOutcome:
        self.issue_calls += 1
        return ACCEPTED

    def query_status(self, *, client_ref: UUID) -> ProviderStatus:
        raise ConnectionError("không gọi được cổng tra cứu")


def test_an_adapter_that_raises_is_not_a_refusal(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Adapter ném ngoại lệ = **không rõ**, không phải bị từ chối.

    Một lượt hết giờ xảy ra *sau* khi yêu cầu đã tới nơi cũng thường như trước,
    nên đánh nó thành từ chối là kết luận một điều không ai biết. Và để ngoại lệ
    bay lên thân job thì cả lô rollback, kể cả những dòng vừa gửi thật.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        claimed = load_for_send(session, row.id, force_reconcile=False)
        assert claimed is not None

        report = transmit(session, claimed, _Exploding())

        assert report.status == OutboxStatus.NEEDS_RECONCILE
        invoice = session.get(EInvoice, row.einvoice_id)
        assert invoice is not None
        assert EInvoiceStatus(invoice.status) == EInvoiceStatus.DANG_PHAT_HANH

    run(work)


def test_a_lookup_that_raises_never_reads_as_never_received(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """`query_status` hỏng mà bị đọc thành "chưa nhận" là đường tới hóa đơn trùng.

    Không biết thì phải nói là không biết — bài này ghim rằng lượt gửi lại
    **không** xảy ra.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        provider = _LookupBroken()
        claimed = load_for_send(session, row.id, force_reconcile=True)
        assert claimed is not None

        report = transmit(session, claimed, provider)

        assert provider.issue_calls == 0, "tra cứu hỏng không được dẫn tới lượt gửi lại"
        assert report.sent is False
        assert row.status == OutboxStatus.NEEDS_RECONCILE

    run(work)


def test_an_invoice_confirmed_by_hand_takes_its_row_out_of_the_queue(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Xác nhận tay đóng dòng hàng đợi, không để bộ bơm gửi tờ ấy đi.

    Người dùng tra cứu trên cổng cơ quan thuế rồi bấm "Xác nhận" là đường có
    thật, và nó để lại một dòng vẫn trỏ tới tờ hóa đơn. Với nhà cung cấp thật,
    một `query_status` trả `known=False` ở lượt bơm kế tiếp sẽ dẫn thẳng tới tờ
    hóa đơn thứ hai.
    """

    def work(session: Session) -> None:
        row = _queued(session, context, accounts)
        EInvoiceService(session).confirm(row.einvoice_id, tax_authority_code="TAY-01")
        provider = _ScriptedProvider(
            issue_outcomes=[ACCEPTED], query_results=[ProviderStatus(known=False)]
        )

        claimed = load_for_send(session, row.id, force_reconcile=True)
        assert claimed is not None
        report = transmit(session, claimed, provider)

        assert provider.issue_calls == [], "hóa đơn đã xác nhận thì hàng đợi không gửi nữa"
        assert report.sent is False
        assert row.status == OutboxStatus.DONE
        assert row.last_error is not None, "lý do đóng phải đọc được ở panel vận hành"

    run(work)
