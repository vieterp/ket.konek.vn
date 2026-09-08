"""FR-EIV-035 — chứng từ gốc đứng yên khi hóa đơn của nó đã phát hành (lát 7D).

**Ba cửa, và bài này đi qua đủ ba.** Đó không phải sự cẩn thận thừa: docstring
của `VoucherReferenceGuards` (review 6G-2 M-4) đặt đúng một điều kiện cho việc
mở thêm điểm gọi guard ở đường chứng từ **nháp** — "phải tự đặt điểm gọi trước
`ensure_editable` **và** kèm test chứng minh đường ấy tới được; đừng tin một
lời gọi không có bài kiểm". Tệp này là bài kiểm ấy.

* **Sửa** chứng từ nháp → `EDIT_GUARDS` ở đầu `VoucherService.ensure_editable`.
* **Xóa** chứng từ nháp → cùng điểm gọi ấy (`delete` gọi `ensure_editable`).
* **Bỏ ghi sổ** chứng từ đã ghi sổ → `REFERENCE_GUARDS` trong
  `PostingService.unpost`.

Và một bài thứ tư đi ngược lại: hóa đơn còn **nháp** thì cả ba cửa phải mở.
Không có nó thì một guard `return` sớm sai chỗ — hoặc một guard chặn tất — vẫn
làm ba bài trên xanh, và người dùng mất khả năng sửa chứng từ của mình.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import VoucherHasIssuedInvoiceError
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import PriceSource
from ket.modules.einvoice.models import EInvoice
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.contracts import VoucherService
from ket.posting.documents.models import Voucher, VoucherStatus
from posting_support import PostingContext, posting_scope, seed_posting_context
from sales_support import ensure_customer, ensure_salesperson, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
APR_12 = date(2026, 4, 12)
CUSTOMER_ID = 9721
SALESPERSON_ID = 9722
FORM_ID = 8821
SERIAL = "C26TGRD"


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
        ensure_customer(session, partner_id=CUSTOMER_ID, code="KH-7D-GRD")
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code="NV-7D-GRD")
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
        document_date=APR_12,
        posting_date=APR_12,
        currency_code="VND",
        exchange_rate=Decimal(1),
        salesperson_id=SALESPERSON_ID,
        description="bán hàng cho bài guard",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng A",
                quantity=Decimal(1),
                unit_price_fc=Decimal(200_000),
                amount_fc=Decimal(200_000),
                vat_rate=Decimal(10),
                vat_amount_fc=Decimal(20_000),
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                price_source=PriceSource.ITEM_DEFAULT,
            ),
        ),
    )


def _voucher_with_invoice(
    session: Session, context: PostingContext, accounts: dict[str, int], *, issued: bool
) -> Voucher:
    """Chứng từ bán kèm một hóa đơn điện tử, đã phát hành hoặc còn nháp."""
    voucher = SalesInvoiceService(session).create(_payload(context, accounts), user_id=ACTOR_ID)
    form = ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
    ensure_active_registration(session, branch_id=context.branch_id, invoice_form_id=form.id)
    service = EInvoiceService(session)
    invoice = service.create_draft(source_voucher_id=voucher.id, invoice_form_id=form.id)
    if issued:
        service.issue(invoice.id, invoice_date=APR_12)
    session.flush()
    return voucher


def _edited(context: PostingContext, accounts: dict[str, int]) -> SalesInvoiceIn:
    return _payload(context, accounts).model_copy(
        update={"description": "sửa sau khi đã phát hành hóa đơn"}
    )


def test_editing_a_draft_voucher_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cửa 1 — `EDIT_GUARDS` ở đầu `ensure_editable`.

    Chứng từ vẫn ở trạng thái **Đã cất**: nếu guard chỉ canh chứng từ đã ghi sổ
    (như `REFERENCE_GUARDS` làm) thì lượt sửa này đi lọt, và nội dung tờ hóa
    đơn đã nộp cơ quan thuế lệch khỏi sổ.
    """

    def work(session: Session) -> None:
        voucher = _voucher_with_invoice(session, context, accounts, issued=True)
        assert VoucherStatus(voucher.status) is VoucherStatus.DA_CAT
        with pytest.raises(VoucherHasIssuedInvoiceError) as raised:
            SalesInvoiceService(session).update(
                voucher.id,
                _edited(context, accounts),
                expected_row_version=voucher.row_version,
                user_id=ACTOR_ID,
            )
        # Thông điệp phải mang số hóa đơn — người dùng cần tìm được nó để xử lý.
        assert raised.value.details["invoice_no"] is not None

    run(work)


def test_deleting_a_draft_voucher_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cửa 2 — cùng điểm gọi, vì `VoucherService.delete` gọi `ensure_editable`."""

    def work(session: Session) -> None:
        voucher = _voucher_with_invoice(session, context, accounts, issued=True)
        with pytest.raises(VoucherHasIssuedInvoiceError):
            VoucherService(session).delete(voucher.id)

    run(work)


def test_unposting_a_posted_voucher_is_refused(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Cửa 3 — `REFERENCE_GUARDS` trong `PostingService.unpost`."""

    def work(session: Session) -> None:
        voucher = _voucher_with_invoice(session, context, accounts, issued=True)
        SalesInvoiceService(session).post(voucher.id, user_id=ACTOR_ID)
        session.flush()
        with pytest.raises(VoucherHasIssuedInvoiceError):
            SalesInvoiceService(session).unpost(voucher.id, user_id=ACTOR_ID)

    run(work)


def test_a_draft_invoice_leaves_the_guard_open(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Hóa đơn **chưa phát hành** không giữ chứng từ lại.

    Bài đối chứng, và nó là bài duy nhất bắt được một guard chặn tất: ba bài
    trên vẫn xanh nếu `refuse_when_invoice_issued` từ chối mọi chứng từ có hóa
    đơn, kể cả hóa đơn nháp — và lúc ấy người dùng lập nhầm một hóa đơn nháp là
    mất luôn quyền sửa chứng từ của mình.

    Sửa, ghi sổ và bỏ ghi sổ đều đi qua. **Xóa thì không** — nhưng vì một lý do
    khác hẳn, và bài dưới nói ra lý do ấy.
    """

    def work(session: Session) -> None:
        voucher = _voucher_with_invoice(session, context, accounts, issued=False)
        service = SalesInvoiceService(session)
        service.update(
            voucher.id,
            _edited(context, accounts),
            expected_row_version=voucher.row_version,
            user_id=ACTOR_ID,
        )
        service.post(voucher.id, user_id=ACTOR_ID)
        session.flush()
        service.unpost(voucher.id, user_id=ACTOR_ID)
        session.flush()

    run(work)


def test_a_draft_invoice_still_holds_the_voucher_against_deletion(
    run: Runner, context: PostingContext, accounts: dict[str, int]
) -> None:
    """Xóa chứng từ khi còn một hóa đơn nháp trỏ vào: **khóa ngoại** từ chối.

    Không phải guard — guard cho qua, đúng như bài trên đo. Chặn ở đây là
    `RESTRICT` của `einvoices.source_voucher_id`, và đó là lựa chọn có chủ đích
    thay vì `CASCADE`: một hóa đơn **đã phát hành** có thể treo trên chứng từ
    vẫn ở trạng thái Đã cất (FR-EIV-011 cho phát hành ngay sau khi lập chứng
    từ), nên `CASCADE` sẽ để một lượt xóa chứng từ cuốn theo hóa đơn thật, và
    lớp duy nhất ngăn nó lại là chính cái guard mà khóa ngoại sinh ra để không
    phải tin.

    Cái giá: xóa chứng từ có hóa đơn nháp đòi **xóa hóa đơn nháp trước**. Đường
    ấy tồn tại (`DELETE /api/v1/einvoices/{id}`), và bài kiểm nó thông suốt.
    """

    def work(session: Session) -> None:
        voucher = _voucher_with_invoice(session, context, accounts, issued=False)
        invoice = session.scalars(
            select(EInvoice).where(EInvoice.source_voucher_id == voucher.id)
        ).one()
        with pytest.raises(IntegrityError, match="fk_einvoices_source_voucher"):
            with session.begin_nested() as savepoint:
                VoucherService(session).delete(voucher.id)
                session.flush()
                savepoint.rollback()

        EInvoiceService(session).delete(invoice.id)
        VoucherService(session).delete(voucher.id)

    run(work)
