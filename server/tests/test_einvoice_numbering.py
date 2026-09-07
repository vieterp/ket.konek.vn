"""Dãy số hóa đơn: liên tục, không trùng, không nhảy cóc (FR-EIV-014, BR-EIV-02).

Hai bài, và cả hai đều là bài mà một test tuần tự **không** kiểm được gì:

* **Mười luồng phát hành đồng thời.** Tiêu chí phase 7 nói thẳng "10 luồng phát
  hành hóa đơn đồng thời cho dãy số liên tục, không trùng, không nhảy cóc; độ
  trễ trung bình < 3s". Chạy thật sự song song bằng thread + `Barrier` trên
  engine `NullPool` — không có connection riêng thì SQLAlchemy nối tiếp chúng
  và cuộc đua biến mất, đúng như `test_concurrent_numbering` đã ghi cho chứng
  từ nội bộ.

* **Open question #13** — "ghép cấp số với `idempotency_keys`", hoãn từ phase 3
  tới đây vì "endpoint phát hành hóa đơn mới có ở phase 7". Mặc định thi công
  đã ghi sẵn: `execute_once` bọc trọn giao dịch nên số không bị đốt thêm khi
  client gửi lại, và **nếu phase 7 đo được số bị đốt** thì phải thêm
  `UNIQUE (document_id)` trên `allocated_numbers` cộng một lượt tra theo
  `document_id` trước khi cấp. Bài dưới đây là phép đo ấy. Nó đo cả hai vế —
  số của hóa đơn, và con số kế tiếp của chính dãy — vì chỉ nhìn vế đầu thì một
  lượt cấp số bị bỏ phí vẫn đi lọt: hóa đơn giữ số cũ mà dãy đã nhích.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.numbering.models import AllocatedNumber
from ket.kernel.numbering.service import NumberingService
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import PriceSource
from ket.modules.einvoice.numbering import scope_key_for
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_05 = date(2026, 3, 5)
CUSTOMER_ID = 9711
SALESPERSON_ID = 9712

CONCURRENT_FORM_ID = 8811
CONCURRENT_SERIAL = "C26TCONC"
IDEMPOTENT_FORM_ID = 8812
IDEMPOTENT_SERIAL = "C26TIDEM"
"""Hai ký hiệu tách hẳn nhau: bài đồng thời khẳng định dãy chạy đúng `1..10`,
nên nó không chịu nổi một lượt cấp số nào của bài khác chen vào."""

THREADS = 10
LATENCY_BUDGET_SECONDS = 3.0
"""FR-EIV-014 — độ trễ trung bình một lượt phát hành."""


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
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7D-NUM")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7D-NUM")
    return account_ids


@pytest.fixture
def independent_factory(test_settings: Settings) -> sessionmaker[Session]:
    """Nhà máy session trên engine `NullPool` — mỗi luồng một connection thật."""
    engine: Engine = create_engine(test_settings.database_url, poolclass=NullPool)
    return create_session_factory(engine)


def _sales_voucher_id(session: Session, context: PostingContext, accounts: dict[str, int]) -> UUID:
    payload = SalesInvoiceIn(
        kind=SalesInvoiceKind.GOODS,
        operation_code="ban-hang-hoa",
        customer_id=CUSTOMER_ID,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=MAR_05,
        posting_date=MAR_05,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        description="bán hàng cho bài dãy số",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(1),
                unit_price_fc=Decimal(500_000),
                amount_fc=Decimal(500_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(50_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )
    return SalesInvoiceService(session).create(payload, user_id=ACTOR_ID).id


def _prepare_drafts(
    factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    form_id: int,
    serial: str,
    count: int,
) -> tuple[list[UUID], str]:
    """`count` hóa đơn nháp cùng một ký hiệu; trả id của chúng và khóa phạm vi dãy."""
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(factory, scope) as session:
        form = ensure_invoice_form(session, form_id=form_id, serial=serial)
        ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
        service = EInvoiceService(session)
        drafts = [
            service.create_draft(
                source_voucher_id=_sales_voucher_id(session, context, accounts),
                invoice_form_id=form.id,
            ).id
            for _ in range(count)
        ]
        scope_key = scope_key_for(form_no=form.form_no or "", serial=form.code)
    return drafts, scope_key


def _run_together(job: Callable[[int], object], threads: int) -> list[object]:
    """Chạy `job` trên `threads` luồng, tất cả khởi hành cùng lúc."""
    barrier = threading.Barrier(threads)

    def wrapped(index: int) -> object:
        barrier.wait()
        return job(index)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(wrapped, index) for index in range(threads)]
        return [future.result() for future in futures]


def test_ten_concurrent_issues_produce_an_unbroken_run(
    independent_factory: sessionmaker[Session],
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Tiêu chí phase 7: liên tục, không trùng, không nhảy cóc; trung bình < 3s.

    Kiểm cả bốn mặt riêng biệt vì chúng hỏng theo bốn kiểu khác nhau: trùng số
    là hai tờ hóa đơn cùng số nộp lên cơ quan thuế; thiếu số là một khoảng
    trống phải giải trình; lệch số lượng là có hóa đơn không nhận được số nào;
    và sổ cấp số lệch là dãy `gap_free` mất khả năng truy ngược số về hóa đơn.
    """
    drafts, scope_key = _prepare_drafts(
        session_factory,
        dataset_alpha,
        context,
        accounts,
        form_id=CONCURRENT_FORM_ID,
        serial=CONCURRENT_SERIAL,
        count=THREADS,
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    # Số bắt đầu đọc từ chính dãy, không đóng cứng là 1: khẳng định `00000001`
    # biến bài này thành bài phụ thuộc **lượt chạy** — xanh trên cơ sở dữ liệu
    # vừa dựng, đỏ ở lượt chạy thứ hai trên cùng cơ sở dữ liệu, và đỏ theo một
    # cách chẳng nói gì về thứ nó định đo. Lần thứ sáu của khuôn bẫy ấy (7A,
    # 7C-1, 7C-2, 7C-3, 7C-5), lần này chặn trước khi nó xảy ra. Thứ cần đo là
    # **tính liên tục**, và tính liên tục là một quan hệ, không phải một giá trị.
    with unit_of_work(session_factory, scope) as session:
        first_expected = NumberingService(session).peek(scope_key)
    assert first_expected is not None, "hồ sơ đăng ký đã kích hoạt phải khai sẵn dãy số"

    def issue(index: int) -> tuple[str, float]:
        started = time.perf_counter()
        with unit_of_work(independent_factory, scope) as session:
            invoice = EInvoiceService(session).issue(drafts[index], invoice_date=MAR_05)
            number = invoice.invoice_no
        assert number is not None
        return number, time.perf_counter() - started

    results = list(_run_together(issue, THREADS))
    numbers = [str(number) for number, _ in results]  # type: ignore[misc]
    latencies = [float(elapsed) for _, elapsed in results]  # type: ignore[misc]

    assert len(set(numbers)) == THREADS, f"có số trùng: {sorted(numbers)}"
    expected = [f"{value:08d}" for value in range(first_expected, first_expected + THREADS)]
    assert sorted(numbers) == expected, f"dãy bị thủng hoặc lệch: {sorted(numbers)}"

    average = sum(latencies) / len(latencies)
    assert average < LATENCY_BUDGET_SECONDS, f"độ trễ trung bình {average:.3f}s vượt mốc"

    with unit_of_work(independent_factory, scope) as session:
        ledger = list(
            session.scalars(
                select(AllocatedNumber.number).where(AllocatedNumber.scope_key == scope_key)
            ).all()
        )
    # Sổ cấp số chứa ĐÚNG những số vừa phát ra và không số nào khác của dãy này
    # — `gap_free` mất ý nghĩa nếu một số ra đời mà không để lại dòng truy ngược.
    assert sorted(ledger) == sorted(numbers)


def test_retrying_issue_with_the_same_key_does_not_burn_a_number(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """**Open question #13, phép đo.**

    Lượt gửi lại cùng khóa idempotency không được đốt thêm một số hóa đơn.
    `execute_once` giành khóa **trước** khi làm việc và bọc trọn giao dịch, nên
    lần thứ hai đọc kết quả cũ mà không chạy `work` lần nào nữa — cả `work` lẫn
    lượt cấp số bên trong nó.

    Đo hai vế: hóa đơn giữ nguyên số, **và** `next_value` của dãy chỉ nhích một.
    Vế thứ hai mới là vế bắt được lỗi thật: một lượt cấp số bị rollback nhưng
    dãy đã commit riêng sẽ để hóa đơn giữ số cũ mà dãy vẫn nhích, và chỉ nhìn
    số hóa đơn thì bài này xanh trong khi dãy đã thủng.
    """
    drafts, scope_key = _prepare_drafts(
        session_factory,
        dataset_alpha,
        context,
        accounts,
        form_id=IDEMPOTENT_FORM_ID,
        serial=IDEMPOTENT_SERIAL,
        count=1,
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    einvoice_id = drafts[0]
    route_key = "POST /api/v1/einvoices/{id}/actions/issue"
    key = str(uuid4())
    fingerprint = fingerprint_of(f'{{"invoice_date":"{MAR_05.isoformat()}"}}')

    with unit_of_work(session_factory, scope) as session:
        before = NumberingService(session).peek(scope_key)
    assert before is not None, "hồ sơ đăng ký đã kích hoạt phải khai sẵn dãy số"

    def work(session: Session) -> tuple[str, IdempotentRef]:
        invoice = EInvoiceService(session).issue(einvoice_id, invoice_date=MAR_05)
        assert invoice.invoice_no is not None
        return invoice.invoice_no, IdempotentRef(result_type="einvoices", result_id=str(invoice.id))

    def replay(session: Session, ref: IdempotentRef) -> str:
        number = EInvoiceService(session).require(UUID(ref.result_id)).invoice_no
        assert number is not None
        return number

    first, performed = execute_once(
        session_factory,
        scope,
        route_key=route_key,
        key=key,
        fingerprint=fingerprint,
        work=work,
        replay=replay,
        ttl=timedelta(hours=24),
    )
    assert performed is True

    second, performed_again = execute_once(
        session_factory,
        scope,
        route_key=route_key,
        key=key,
        fingerprint=fingerprint,
        work=work,
        replay=replay,
        ttl=timedelta(hours=24),
    )
    assert performed_again is False, "lượt gửi lại đã chạy `work` một lần nữa"
    assert second == first

    with unit_of_work(session_factory, scope) as session:
        after = NumberingService(session).peek(scope_key)
        allocated = list(
            session.scalars(
                select(AllocatedNumber.number).where(AllocatedNumber.scope_key == scope_key)
            ).all()
        )

    assert after == before + 1, f"dãy nhích {after - before} bậc cho một hóa đơn"
    assert allocated == [first], f"sổ cấp số có số thừa: {allocated}"
