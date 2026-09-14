"""Chín báo cáo bán hàng (`docs/srs/06` §5.1, lát 7G-2a).

Tất cả đi qua chính đường preview của report engine — đăng ký hoàn toàn bằng
metadata, không một dòng renderer riêng. Bốn nhóm bất biến, và cả bốn đều là chỗ
một báo cáo có thể ra **số sai mà vẫn ra số**:

* **Chứng từ ghi giảm trừ ra khỏi doanh thu**, không cộng vào. Dấu đến từ SỔ
  (mapper đã đảo chiều ba loại `REVERSING_KINDS`), nên nhân dấu thêm một lần nữa
  ở tầng báo cáo đưa phần trả lại về dương — đúng con số phóng đại mà việc đảo
  dấu sinh ra để tránh. Lát 7G-1 sập đúng bẫy này ở chiều mua.
* **Điều chỉnh TĂNG cộng vào**, vì `kind = 5` không thuộc tập đảo dấu. Hai loại
  điều chỉnh đi hai nhánh, và một bài kiểm chỉ phủ chiều giảm sẽ xanh y như thế
  nếu nhánh dấu bắt cả hai.
* **Tiền trên báo cáo BẰNG tiền trên sổ, từng đồng, kể cả ngoại tệ.** Dòng hàng
  bán chỉ có cột nguyên tệ, nên đường duy nhất không lệch là đọc `gl_postings`
  chứ không nhân lại tỷ giá.
* **Chứng từ mới Cất không lọt vào báo cáo** — và không lọt bằng *cấu trúc*
  (`gl_postings` chỉ chứa chứng từ đã ghi sổ), không bằng một điều kiện phải nhớ.

Sáu bài kiểm bộ lọc dùng **hai** giá trị cho mỗi chiều: bài học 7G-1 là một bộ
dữ liệu chỉ có một nhân viên và một công trình làm bài kiểm không phân biệt được
"lọc theo id" với "bỏ dòng để trống" — đổi dataset thành `IS NOT NULL` thì nó vẫn
xanh.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import MasterDataInUseError, PostingValidationError
from ket.kernel.master_data.item_variant_service import (
    ItemVariantMergeHook,
    ItemVariantService,
)
from ket.kernel.master_data.models.employee import EMPLOYEE_TABLE_NAME
from ket.kernel.master_data.models.item_variant import ITEM_VARIANT_TABLE_NAME, ItemVariant
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SALES_DOCUMENT_TYPE, SalesInvoiceKind
from ket.modules.sales.schemas import (
    SalesInvoiceIn,
    SalesInvoiceLineIn,
    SalesInvoiceUpdate,
    SalesSettlementIn,
)
from ket.modules.sales.service import VARIANT_NOT_OF_ITEM_CODE, SalesInvoiceService
from ket.posting.contracts import Voucher
from ket.posting.engine.models import GlPosting, Ledger
from ket.posting.integrity.checks.registry import check_of
from ket.posting.integrity.runner import run_check
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_project, ensure_unit
from report_preview_support import Preview, PreviewResult, money
from sales_support import (
    ensure_customer,
    ensure_salesperson,
    ensure_variant,
    seed_sales_package_data,
)

pytestmark = pytest.mark.db

ACTOR_ID = 1
REPORT_ROLE = "xem_bao_cao_ban_hang"

# Cửa sổ riêng của tệp này (tháng 6–7/2026) — `dataset_alpha` dùng chung, tách dữ
# liệu bằng THỜI GIAN chứ không bằng thứ tự chạy (bài học 5D). Hai tháng chứ không
# một: "doanh số theo tháng" trên dữ liệu một tháng là một báo cáo có đúng một
# nhóm, và một bài kiểm gộp theo tháng trên một nhóm không kiểm gì.
JUN_01 = date(2026, 6, 1)
JUN_10 = date(2026, 6, 10)
JUN_12 = date(2026, 6, 12)
JUN_15 = date(2026, 6, 15)
JUN_20 = date(2026, 6, 20)
JUL_05 = date(2026, 7, 5)
JUL_06 = date(2026, 7, 6)
JUL_31 = date(2026, 7, 31)

# Khối id 99xx là của tệp này; 98xx là của `test_purchase_reports`. `id` cố định
# là quy ước sẵn của bộ test danh mục (`path` phải là chuỗi id có dấu chấm),
# nhưng nó biến mỗi con số thành một **tài nguyên dùng chung cả phiên** — hai tệp
# lấy trùng một số thì tệp chạy trước tạo bản ghi, tệp sau thấy "đã có" nên không
# tạo nữa, và bài kiểm đỏ ở một tệp thứ ba. Cái bẫy này đã sập hai lần
# (`test_einvoice_flow`, rồi 7G-1 ở số 9401).
CUSTOMER_ID = 9901
OTHER_CUSTOMER_ID = 9902
PLAIN_CUSTOMER_ID = 9903
SALESPERSON_ID = 9911
SECOND_SALESPERSON_ID = 9912
ITEM_A_ID = 9921
ITEM_B_ID = 9922
UNIT_ID = 9931
PROJECT_ID = 9941
SECOND_PROJECT_ID = 9942
VARIANT_A_ID = 9951
VARIANT_B_ID = 9952
# Quy cách CHỈ bài kiểm xóa danh mục dùng. Bốn chứng từ của `books` đều trỏ
# VARIANT_A_ID, nên bộ đếm của nó không bao giờ về 0 trong phiên này — và vế
# "trả bộ đếm xong thì xóa được" cần một dòng danh mục không ai khác dùng.
DISPOSABLE_VARIANT_ID = 9953

CUSTOMER_CODE = "KH-7G2-01"
OTHER_CUSTOMER_CODE = "KH-7G2-02"
PLAIN_CUSTOMER_CODE = "KH-7G2-03"
SALESPERSON_CODE = "NV-7G2-BAN"
SECOND_SALESPERSON_CODE = "NV-7G2-BAN2"
ITEM_A_CODE = "VT-7G2-A"
ITEM_B_CODE = "VT-7G2-B"
UNIT_CODE = "Cai-7G2"
PROJECT_CODE = "CT-7G2"
SECOND_PROJECT_CODE = "CT-7G2-B"

# Mã quy cách DÙNG CHUNG giữa hai mã hàng — hợp lệ (`uq_item_variants_item_code`
# chỉ duy nhất trong một mã hàng), và là dữ liệu duy nhất phân biệt được "gộp
# theo quy cách" hai bậc với một bậc.
SHARED_VARIANT_CODE = "QC-M"
DISPOSABLE_VARIANT_CODE = "QC-XOA"

HA_NOI = "Hà Nội"
DA_NANG = "Đà Nẵng"
NO_PROVINCE_LABEL = "Không khai địa phương"
NO_SALESPERSON_LABEL = "Chưa khai nhân viên bán"

GOODS_AMOUNT = Decimal(2_000_000)
GOODS_VAT = Decimal(200_000)
# Chiết khấu thương mại: đơn giá × số lượng = 2.100.000, giảm 100.000, doanh thu
# ghi 2.000.000. Phần đã giảm giữ trên thân chứng từ và KHÔNG đi vào bút toán —
# nên nó không đối chiếu với tài khoản nào, và không cột tổng nào cộng nó.
GOODS_QUANTITY = Decimal(10)
GOODS_UNIT_PRICE = Decimal(210_000)
GOODS_DISCOUNT = Decimal(100_000)

RETURN_AMOUNT = Decimal(500_000)
RETURN_VAT = Decimal(50_000)
RETURN_QUANTITY = Decimal(2)

INCREASE_AMOUNT = Decimal(100_000)
INCREASE_VAT = Decimal(10_000)

SERVICE_AMOUNT = Decimal(300_000)
SERVICE_VAT = Decimal(30_000)

# Hóa đơn ngoại tệ: tỷ giá cố ý KHÔNG tròn để phép "nhân lại tỷ giá" lệch thấy
# được. 1.234,5 × 24.317 = 30.019.336,5 → sổ làm tròn một lần, ở một chỗ, và
# báo cáo phải đọc lại đúng con số ấy chứ không tự làm tròn lần thứ hai.
FX_AMOUNT_FC = Decimal("1234.50")
FX_RATE = Decimal(24_317)

DRAFT_DESCRIPTION = "hóa đơn bán chưa ghi sổ"

# Doanh thu VND của khách hàng thứ nhất: bán − trả lại + điều chỉnh tăng. Bug
# đảo dấu hai lần cho ra 2.600.000 ở đây (chênh đúng hai lần phần trả lại).
CUSTOMER_ONE_REVENUE = GOODS_AMOUNT - RETURN_AMOUNT + INCREASE_AMOUNT


@pytest.fixture
def client(
    test_settings: Settings, app_engine: Engine, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    assert app_engine is not None and session_factory is not None
    with api_test_client(create_app(test_settings)) as instance:
        yield instance


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    codes = seed_sales_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_customer(session, partner_id=CUSTOMER_ID, code=CUSTOMER_CODE, province=HA_NOI)
        ensure_customer(
            session, partner_id=OTHER_CUSTOMER_ID, code=OTHER_CUSTOMER_CODE, province=DA_NANG
        )
        # Khách hàng KHÔNG khai tỉnh: nhóm "chưa khai" phải có TÊN trên báo cáo
        # theo địa phương, không phải một tiêu đề trống đọc như lỗi hiển thị.
        ensure_customer(session, partner_id=PLAIN_CUSTOMER_ID, code=PLAIN_CUSTOMER_CODE)
        ensure_salesperson(session, employee_id=SALESPERSON_ID, code=SALESPERSON_CODE)
        ensure_salesperson(session, employee_id=SECOND_SALESPERSON_ID, code=SECOND_SALESPERSON_CODE)
        # Vật tư / đơn vị / công trình là danh mục dùng chung, không phải của
        # riêng phân hệ mua: dùng lại helper của `purchase_support` thay vì chép
        # bốn hàm gieo sang đây.
        # Mã đơn vị tính duy nhất TOÀN BẢNG (`uq_units_of_measure_shared_code`),
        # không chỉ trong khối id của tệp này: `test_purchase_reports` đã giữ mã
        # "Cái" ở id 9831, và `ensure_unit` idempotent theo **id** nên nó không
        # thấy trùng — lượt `INSERT` đổ `UniqueViolation`, và chỉ đổ khi chạy cả
        # nhóm `db`. Khối id riêng chưa đủ; mã người dùng đặt cũng phải riêng.
        ensure_unit(session, unit_id=UNIT_ID, code=UNIT_CODE)
        ensure_item(session, item_id=ITEM_A_ID, code=ITEM_A_CODE, unit_id=UNIT_ID)
        ensure_item(session, item_id=ITEM_B_ID, code=ITEM_B_CODE, unit_id=UNIT_ID)
        ensure_variant(
            session, variant_id=VARIANT_A_ID, item_id=ITEM_A_ID, code=SHARED_VARIANT_CODE
        )
        ensure_variant(
            session, variant_id=VARIANT_B_ID, item_id=ITEM_B_ID, code=SHARED_VARIANT_CODE
        )
        ensure_variant(
            session,
            variant_id=DISPOSABLE_VARIANT_ID,
            item_id=ITEM_A_ID,
            code=DISPOSABLE_VARIANT_CODE,
        )
        ensure_project(session, project_id=PROJECT_ID, code=PROJECT_CODE)
        ensure_project(session, project_id=SECOND_PROJECT_ID, code=SECOND_PROJECT_CODE)
    return codes


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    """Năm chứng từ đã ghi sổ và một chứng từ CHỈ cất.

    Một bộ dữ liệu cho cả chín báo cáo: mỗi bài dưới đây đọc phần của nó và
    khẳng định trên những con số biết trước ở đầu tệp.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        service = SalesInvoiceService(session)

        goods = service.create(_invoice(context, accounts), user_id=ACTOR_ID)
        service.post(goods.id, user_id=ACTOR_ID)
        ids["goods"] = goods.id
        # Đích đối trừ là dòng SỔ PHỤ, không phải chứng từ: `target_id` khớp
        # `SettlementTargetSource.target_id` của kernel, và khóa chính của
        # `ar_ap_ledger` là UUID của dòng nợ (quyết định 7A).
        receivable = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == goods.id)
        ).scalar_one()

        # Hàng bán bị trả lại: đối trừ hóa đơn gốc, nên nó KHÔNG sinh dòng sổ phụ
        # mới — nhưng vẫn phải hiện trên báo cáo bán hàng với dấu ÂM.
        returned = service.create(
            _invoice(
                context,
                accounts,
                kind=SalesInvoiceKind.RETURN,
                operation="tra-lai-hang-ban",
                posting_date=JUN_12,
                lines=(
                    SalesInvoiceLineIn(
                        description="Trả lại hàng A",
                        item_id=ITEM_A_ID,
                        unit_id=UNIT_ID,
                        variant_id=VARIANT_A_ID,
                        quantity=RETURN_QUANTITY,
                        unit_price_fc=Decimal(250_000),
                        amount_fc=RETURN_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=RETURN_VAT,
                        account_id=accounts["5111"],
                        vat_account_id=accounts["33311"],
                        project_id=PROJECT_ID,
                    ),
                ),
                settlements=(
                    SalesSettlementIn(
                        target_kind=SettlementTargetKind.SALES_INVOICE,
                        target_id=receivable.id,
                        amount_fc=RETURN_AMOUNT + RETURN_VAT,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(returned.id, user_id=ACTOR_ID)
        ids["return"] = returned.id

        # Điều chỉnh TĂNG: chiều thuận như hóa đơn thường, chỉ mang phần chênh.
        # Nó ở đây để phân biệt `REVERSING_KINDS` với "mọi loại điều chỉnh".
        increase = service.create(
            _invoice(
                context,
                accounts,
                kind=SalesInvoiceKind.ADJUSTMENT_INCREASE,
                operation="dieu-chinh-tang-hoa-don",
                posting_date=JUN_15,
                adjusts_voucher_id=goods.id,
                lines=(
                    SalesInvoiceLineIn(
                        description="Điều chỉnh tăng hàng A",
                        item_id=ITEM_A_ID,
                        unit_id=UNIT_ID,
                        variant_id=VARIANT_A_ID,
                        quantity=Decimal(1),
                        unit_price_fc=INCREASE_AMOUNT,
                        amount_fc=INCREASE_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=INCREASE_VAT,
                        account_id=accounts["5111"],
                        vat_account_id=accounts["33311"],
                        project_id=PROJECT_ID,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(increase.id, user_id=ACTOR_ID)
        ids["increase"] = increase.id

        # Hóa đơn ngoại tệ — nguyên liệu của bài "báo cáo khớp sổ từng đồng", và
        # là chứng từ duy nhất của khách hàng / nhân viên / công trình thứ hai.
        fx = service.create(
            _invoice(
                context,
                accounts,
                customer_id=OTHER_CUSTOMER_ID,
                salesperson_id=SECOND_SALESPERSON_ID,
                currency_code="USD",
                exchange_rate=FX_RATE,
                posting_date=JUN_20,
                lines=(
                    SalesInvoiceLineIn(
                        description="Hàng xuất khẩu",
                        item_id=ITEM_B_ID,
                        unit_id=UNIT_ID,
                        variant_id=VARIANT_B_ID,
                        quantity=Decimal(3),
                        unit_price_fc=Decimal("411.50"),
                        amount_fc=FX_AMOUNT_FC,
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["5111"],
                        vat_account_id=None,
                        project_id=SECOND_PROJECT_ID,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(fx.id, user_id=ACTOR_ID)
        ids["fx"] = fx.id

        # Hóa đơn dịch vụ THÁNG BẢY, không khai nhân viên bán, khách hàng không
        # khai tỉnh, ghi vào TK doanh thu THỨ HAI. Một chứng từ, bốn nhóm thứ hai:
        # tháng, nhân viên "chưa khai", tỉnh "chưa khai", và tài khoản của sổ
        # nhật ký. Không có nó, bốn bài kiểm gộp đều chạy trên một nhóm duy nhất.
        service_invoice = service.create(
            _invoice(
                context,
                accounts,
                kind=SalesInvoiceKind.SERVICE,
                operation="ban-dich-vu",
                customer_id=PLAIN_CUSTOMER_ID,
                salesperson_id=None,
                posting_date=JUL_05,
                lines=(
                    SalesInvoiceLineIn(
                        description="Phí dịch vụ tháng 7",
                        quantity=None,
                        unit_price_fc=None,
                        amount_fc=SERVICE_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=SERVICE_VAT,
                        account_id=accounts["5112"],
                        vat_account_id=accounts["33311"],
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(service_invoice.id, user_id=ACTOR_ID)
        ids["service"] = service_invoice.id

        # Chứng từ CHỈ cất, không ghi sổ — nó không được xuất hiện ở đâu cả.
        draft = service.create(
            _invoice(context, accounts, posting_date=JUL_06, description=DRAFT_DESCRIPTION),
            user_id=ACTOR_ID,
        )
        ids["draft"] = draft.id
    return ids


@pytest.fixture(scope="module")
def fx_revenue(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    books: dict[str, UUID],
) -> Decimal:
    """Doanh thu VND của hóa đơn ngoại tệ, ĐỌC TỪ SỔ.

    Viết sẵn một con số ở đầu tệp là dựng lại luật làm tròn của posting engine
    trong bài kiểm — và khi hai bên lệch, bài kiểm sẽ nói báo cáo sai trong khi
    chỗ sai là kỳ vọng của chính nó.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        total = session.scalar(
            select(func.coalesce(func.sum(GlPosting.credit - GlPosting.debit), 0)).where(
                GlPosting.voucher_id == books["fx"],
                GlPosting.account_id == _revenue_accounts(session, books["fx"]),
                GlPosting.ledger == Ledger.FINANCIAL,
            )
        )
    assert isinstance(total, Decimal)
    return total


def _revenue_accounts(session: Session, voucher_id: UUID) -> int:
    """TK doanh thu của hóa đơn ngoại tệ — nó chỉ có một dòng hàng, một TK."""
    account_id = session.scalar(
        select(GlPosting.account_id).where(GlPosting.voucher_id == voucher_id, GlPosting.credit > 0)
    )
    assert isinstance(account_id, int)
    return account_id


def _variant_uses(session: Session, variant_id: int) -> int:
    """Bộ đếm tham chiếu của một quy cách — BR-SYS-02 trả lời "xóa được chưa"."""
    return usage_count_of(session, entity_type=ITEM_VARIANT_TABLE_NAME, entity_id=variant_id)


def _line(accounts: dict[str, int], *, item_id: int, variant_id: int) -> SalesInvoiceLineIn:
    """Một dòng hàng chỉ khác nhau ở cặp (mã hàng, quy cách)."""
    return SalesInvoiceLineIn(
        description=f"Hàng {item_id} quy cách {variant_id}",
        item_id=item_id,
        unit_id=UNIT_ID,
        variant_id=variant_id,
        quantity=Decimal(1),
        unit_price_fc=GOODS_AMOUNT,
        amount_fc=GOODS_AMOUNT,
        vat_rate=Decimal(0),
        vat_amount_fc=Decimal(0),
        account_id=accounts["5111"],
        vat_account_id=None,
    )


def _invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    operation: str = "ban-hang-hoa",
    kind: int = SalesInvoiceKind.GOODS,
    settlements: tuple[SalesSettlementIn, ...] = (),
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    posting_date: date = JUN_10,
    customer_id: int = CUSTOMER_ID,
    salesperson_id: int | None = SALESPERSON_ID,
    description: str | None = "bán hàng báo cáo 7G-2a",
    adjusts_voucher_id: UUID | None = None,
    lines: tuple[SalesInvoiceLineIn, ...] | None = None,
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=kind,
        operation_code=operation,
        adjusts_voucher_id=adjusts_voucher_id,
        customer_id=customer_id,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        salesperson_id=salesperson_id,
        description=description,
        lines=lines
        or (
            SalesInvoiceLineIn(
                description="Hàng A",
                item_id=ITEM_A_ID,
                unit_id=UNIT_ID,
                variant_id=VARIANT_A_ID,
                quantity=GOODS_QUANTITY,
                unit_price_fc=GOODS_UNIT_PRICE,
                discount_amount_fc=GOODS_DISCOUNT,
                amount_fc=GOODS_AMOUNT,
                vat_rate=Decimal(10),
                vat_amount_fc=GOODS_VAT,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"],
                project_id=PROJECT_ID,
            ),
        ),
        settlements=settlements,
    )


@pytest.fixture(scope="module")
def report_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Quyền báo cáo **và** quyền xem hóa đơn bán.

    `reporting.report.view` một mình không đủ: chín định nghĩa của lát này khai
    `required_permission_module = "sales"`, đúng bản vá H-1b của 6E-1 — một mã
    quyền báo cáo chung không được mở dữ liệu của phân hệ mà người dùng không có
    quyền đọc.
    """
    return ensure_role(
        session_factory,
        dataset_alpha,
        REPORT_ROLE,
        ["reporting.report.view", permission_code("sales", "invoice", Action.VIEW)],
    )


@pytest.fixture
def preview(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    test_password: str,
    context: PostingContext,
    report_role: str,
) -> Preview:
    headers = {
        **actor(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            report_role,
            "bao_cao_ban_hang",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    def run(code: str, **params: object) -> PreviewResult:
        body = {
            "params": {
                "from_date": JUN_01.isoformat(),
                "to_date": JUL_31.isoformat(),
                **params,
            }
        }
        response = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return PreviewResult(response.json())

    return run


class TestSignsComeFromTheBooks:
    def test_a_return_invoice_lowers_the_sales_total_instead_of_raising_it(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Doanh thu của một khách = bán − trả lại + điều chỉnh tăng.

        Bút toán của chứng từ trả lại ĐÃ đảo chiều (`posting_mapper`), nên
        `SUM(credit - debit)` của nó vốn đã âm. Nhân thêm −1 ở dataset đưa phần
        trả lại về dương và con số ở đây thành 2.600.000 — chênh đúng hai lần
        phần đã trả lại, và vẫn là một con số trông hợp lý.
        """
        result = preview("tong-hop-ban-hang-theo-khach-hang", customer_id=CUSTOMER_ID)
        assert result.total("amount") == CUSTOMER_ONE_REVENUE

    def test_only_the_return_shows_up_negative(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Cô lập từng loại: chỉ chứng từ trả lại mang dấu âm."""
        returns = preview("so-chi-tiet-ban-hang", kind=SalesInvoiceKind.RETURN)
        assert returns.total("amount") == -RETURN_AMOUNT

    def test_an_increase_adjustment_adds_to_revenue(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """`kind = 5` KHÔNG thuộc `REVERSING_KINDS` — nó là một khoản nợ mới.

        Một nhánh dấu viết theo "mọi loại điều chỉnh" thay vì theo
        `REVERSING_KINDS` cho ra −100.000 ở đây.
        """
        increases = preview("so-chi-tiet-ban-hang", kind=SalesInvoiceKind.ADJUSTMENT_INCREASE)
        assert increases.total("amount") == INCREASE_AMOUNT

    def test_the_return_row_carries_negative_quantity_too(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Cột lấy từ THÂN chứng từ cũng phải mang dấu.

        `quantity` không có trong `gl_postings`, nên nó là cột duy nhất mà dataset
        tự nhân dấu — bỏ phép nhân ấy thì số lượng trả lại cộng vào số lượng bán.
        """
        returns = preview("so-chi-tiet-ban-hang", kind=SalesInvoiceKind.RETURN)
        (row,) = returns.rows
        assert money(row, "quantity") == -RETURN_QUANTITY
        assert money(row, "amount") == -RETURN_AMOUNT


class TestTheReportMatchesTheBooks:
    def test_the_register_total_equals_the_revenue_movement_on_the_ledger(
        self,
        preview: Preview,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
        books: dict[str, UUID],
    ) -> None:
        """Tổng doanh thu trên báo cáo = phát sinh Có − Nợ của hai TK doanh thu.

        Đây là bài kiểm mà việc "nhân lại tỷ giá" làm đỏ: hóa đơn ngoại tệ
        1.234,50 × 24.317 = 30.019.336,5, sổ làm tròn một lần còn phép nhân lại
        làm tròn lần thứ hai, và hai cách lệch ở đồng cuối.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            # Thu về chứng từ loại `SAL`: `dataset_alpha` dùng chung, và một
            # tệp test khác ghi bút toán tay vào 511 trong cùng cửa sổ sẽ làm bài
            # kiểm này đỏ vì một lý do không phải lỗi của báo cáo — đúng họ bẫy
            # "tài nguyên dùng chung" mà khối id riêng đã phải giải một lần.
            posted = session.scalar(
                select(func.coalesce(func.sum(GlPosting.credit - GlPosting.debit), 0))
                .join(Voucher, Voucher.id == GlPosting.voucher_id)
                .where(
                    GlPosting.account_id.in_([accounts["5111"], accounts["5112"]]),
                    GlPosting.ledger == Ledger.FINANCIAL,
                    GlPosting.posting_date >= JUN_01,
                    GlPosting.posting_date <= JUL_31,
                    Voucher.document_type == SALES_DOCUMENT_TYPE,
                )
            )
        assert preview("so-chi-tiet-ban-hang").total("amount") == posted

    def test_the_foreign_currency_invoice_reports_the_amount_the_books_hold(
        self, preview: Preview, fx_revenue: Decimal
    ) -> None:
        """Cùng bất biến, thu về MỘT chứng từ để chỗ lệch chỉ ra được."""
        result = preview("so-chi-tiet-ban-hang", customer_id=OTHER_CUSTOMER_ID)
        (row,) = result.rows
        assert money(row, "amount") == fx_revenue
        # Cột nguyên tệ là giá trị đã lưu trên dòng, không quy đổi lần nào.
        assert money(row, "goods_amount_fc") == FX_AMOUNT_FC

    def test_a_saved_but_unposted_invoice_never_appears(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Chứng từ mới Cất không lọt vào báo cáo — bằng cấu trúc.

        `gl_postings` chỉ chứa chứng từ đã ghi sổ, và dataset nối `INNER JOIN`
        vào đó, nên phép lọc này không phải một điều kiện ai đó phải nhớ viết.
        """
        assert DRAFT_DESCRIPTION not in preview("so-chi-tiet-ban-hang").all_text()

    def test_the_total_column_is_revenue_plus_output_vat(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """`amount_with_vat` = phần ghi Nợ TK phải thu của dòng.

        Hóa đơn bán không có khoản chi phí phân bổ nào (khác hóa đơn mua), nên
        cột này thật sự là tổng tiền thanh toán — nhãn nói đúng điều nó cộng.
        """
        result = preview("so-chi-tiet-ban-hang", kind=SalesInvoiceKind.GOODS, item_id=ITEM_A_ID)
        (row,) = result.rows
        assert money(row, "amount") == GOODS_AMOUNT
        assert money(row, "vat_amount") == GOODS_VAT
        assert money(row, "amount_with_vat") == GOODS_AMOUNT + GOODS_VAT
        # Chiết khấu đọc được nhưng KHÔNG nằm trong doanh thu: mapper trừ thẳng.
        assert money(row, "discount_amount_fc") == GOODS_DISCOUNT


class TestFiltersActuallyFilter:
    """Mỗi chiều lọc có HAI giá trị trong bộ dữ liệu, và hai tổng khác nhau.

    Bài học 7G-1: hai bài kiểm bộ lọc chạy trên một nhân viên và một công trình
    duy nhất vẫn xanh khi dataset đổi phép lọc thành `IS NOT NULL` — chúng không
    phân biệt được "lọc theo id" với "bỏ dòng để trống".
    """

    def test_each_filter_narrows_to_the_row_it_names(
        self, preview: Preview, fx_revenue: Decimal, books: dict[str, UUID]
    ) -> None:
        pairs: tuple[tuple[str, object, object], ...] = (
            ("customer_id", CUSTOMER_ID, OTHER_CUSTOMER_ID),
            ("item_id", ITEM_A_ID, ITEM_B_ID),
            ("variant_id", VARIANT_A_ID, VARIANT_B_ID),
            ("salesperson_id", SALESPERSON_ID, SECOND_SALESPERSON_ID),
            ("project_id", PROJECT_ID, SECOND_PROJECT_ID),
        )
        for name, first, second in pairs:
            assert (
                preview("so-chi-tiet-ban-hang", **{name: first}).total("amount")
                == CUSTOMER_ONE_REVENUE
            ), name
            assert (
                preview("so-chi-tiet-ban-hang", **{name: second}).total("amount") == fx_revenue
            ), name

    def test_the_kind_filter_separates_the_four_document_kinds(
        self, preview: Preview, fx_revenue: Decimal, books: dict[str, UUID]
    ) -> None:
        expected = {
            SalesInvoiceKind.GOODS: GOODS_AMOUNT + fx_revenue,
            SalesInvoiceKind.SERVICE: SERVICE_AMOUNT,
            SalesInvoiceKind.RETURN: -RETURN_AMOUNT,
            SalesInvoiceKind.ADJUSTMENT_INCREASE: INCREASE_AMOUNT,
        }
        for kind, amount in expected.items():
            assert preview("so-chi-tiet-ban-hang", kind=kind).total("amount") == amount, kind


class TestGroupings:
    def test_unset_dimensions_group_under_a_named_bucket(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """ "Chưa khai" là một câu trả lời, không phải một tiêu đề trống.

        Hóa đơn dịch vụ tháng 7 không khai nhân viên bán, và khách hàng của nó
        không khai tỉnh — hai nhóm phải có tên đọc được.
        """
        by_salesperson = preview("tong-hop-ban-hang-theo-nhan-vien")
        assert any(NO_SALESPERSON_LABEL in heading for heading in by_salesperson.headings)
        by_province = preview("tong-hop-ban-hang-theo-dia-phuong")
        headings = "\n".join(by_province.headings)
        assert HA_NOI in headings and DA_NANG in headings and NO_PROVINCE_LABEL in headings

    def test_the_branch_summary_names_the_branch_it_groups_by(
        self, preview: Preview, context: PostingContext, books: dict[str, UUID]
    ) -> None:
        """Chiều "theo đơn vị" nối `branches` — dataset đầu tiên làm việc ấy.

        Bộ dữ liệu của tệp này nằm trong MỘT chi nhánh, nên bài kiểm khẳng định
        phép nối trả đúng mã và tên chi nhánh; cơ chế gộp nhiều nhóm đã được bốn
        báo cáo tổng hợp còn lại phủ trên cùng một engine, và phạm vi nhiều chi
        nhánh là việc của bộ test RLS.
        """
        result = preview("tong-hop-ban-hang-theo-don-vi")
        assert any(context.branch_code in heading for heading in result.headings)

    def test_the_variant_book_keeps_the_shared_variant_code_in_two_item_groups(
        self, preview: Preview, fx_revenue: Decimal, books: dict[str, UUID]
    ) -> None:
        """Hai mã hàng dùng chung mã quy cách "QC-M" phải ra HAI nhóm.

        Gộp theo quy cách một bậc trộn doanh thu của hai mặt hàng vào một nhóm
        mang một cái tên đúng — sai mà không có gì trên tờ giấy chỉ ra.
        """
        result = preview("so-chi-tiet-ban-hang-theo-quy-cach")
        headings = result.headings
        assert any(ITEM_A_CODE in heading for heading in headings)
        assert any(ITEM_B_CODE in heading for heading in headings)
        assert sum(SHARED_VARIANT_CODE in heading for heading in headings) >= 2
        # Đếm tiêu đề bắt được lượt gộp một bậc, nhưng nó vẫn xanh nếu phép nối
        # quy cách trả sai dòng. Tiền của từng nhóm mới nói được điều đó: doanh
        # thu của quy cách A là của hàng A, của quy cách B là hóa đơn ngoại tệ.
        for variant_id, item_code, amount in (
            (VARIANT_A_ID, ITEM_A_CODE, CUSTOMER_ONE_REVENUE),
            (VARIANT_B_ID, ITEM_B_CODE, fx_revenue),
        ):
            narrowed = preview("so-chi-tiet-ban-hang-theo-quy-cach", variant_id=variant_id)
            assert narrowed.total("amount") == amount, item_code
            # Quy cách nào thì nhóm mã hàng ấy: `item_code` là khóa gộp nên nó
            # nằm trên TIÊU ĐỀ nhóm, không lặp lại ở từng dòng.
            assert any(item_code in heading for heading in narrowed.headings), item_code
            assert not any(
                other in heading
                for heading in narrowed.headings
                for other in (ITEM_A_CODE, ITEM_B_CODE)
                if other != item_code
            ), item_code

    def test_the_monthly_report_has_one_group_per_posting_month(
        self, preview: Preview, fx_revenue: Decimal, books: dict[str, UUID]
    ) -> None:
        """Gộp theo THÁNG GHI SỔ, cùng mốc với cột tiền.

        Gộp theo ngày chứng từ cho ra một tổng tháng không khớp phát sinh TK 511
        của chính tháng ấy.
        """
        result = preview("doanh-so-ban-hang-theo-thang")
        headings = "\n".join(result.headings)
        assert "2026-06" in headings and "2026-07" in headings
        assert result.total("amount") == CUSTOMER_ONE_REVENUE + fx_revenue + SERVICE_AMOUNT

    def test_the_sales_journal_groups_by_revenue_account(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Sổ nhật ký bán hàng gộp theo TK doanh thu — hai TK, hai nhóm."""
        result = preview("so-nhat-ky-ban-hang")
        headings = "\n".join(result.headings)
        assert "5111" in headings and "5112" in headings


class TestUsageCounterReconciles:
    """Bộ đếm tham chiếu khớp phía tham chiếu — trên dữ liệu ĐÚNG.

    Bài kiểm này tồn tại vì lát 7G-2a thêm một đường `record_use` mới
    (`item_variants`) và **quên nhánh đối chiếu** của nó ở
    `usage_counter_accurate.sql` — đúng lỗi mà 7G-1 đã mắc với
    `purchase_invoices.buyer_id` và không ai phát hiện trong hai lát. Hậu quả
    không phải một con số sai: nó là check FR-NFR-007 **đỏ vĩnh viễn trên dữ liệu
    đúng**, tức tiếng chuông duy nhất báo "một đường ghi nào đó quên `record_use`"
    chìm trong tiếng ồn đã biết.

    Bộ test cũ chỉ canh chiều ngược (bộ đếm lạc → bị gắn cờ,
    `test_stray_usage_counter_is_flagged_...`) và khẳng định `>= 1` discrepancy,
    nên nó xanh y như thế khi mọi hóa đơn bán đều sinh một dòng lệch.
    """

    def test_the_usage_check_is_clean_on_correct_books(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        books: dict[str, UUID],
    ) -> None:
        """Không dòng lệch nào mang danh mục của TỆP NÀY.

        Thu hẹp về đúng những `entity_id` tệp này gieo, không đòi cả bảng sạch:
        `dataset_alpha` dùng chung cả phiên và bộ đếm là dữ liệu **toàn dataset**
        (chính vì thế `test_clean_books_pass_the_branch_scoped_checks` cũng không
        assert check này). Bản đầu của bài kiểm này lọc theo `entity_type` và đỏ
        khi chạy cả nhóm vì một khoản lệch **có trước lát này**:
        `test_einvoice_source_guard` xóa chứng từ bán bằng `VoucherService.delete`
        thẳng — cửa duy nhất KHÔNG chạy hook `before_delete` (hook ấy chỉ được gọi
        ở router `api/routers/vouchers.py` và ở `SalesInvoiceService.delete`), nên
        bộ đếm khách hàng và nhân viên của chứng từ ấy không được trả. Mọi đường
        ghi THẬT đều trả đúng một lần, nên đó là nợ của bộ test, không phải lỗi sản
        phẩm — và không phải việc của lát này sửa.

        Thu hẹp như vậy KHÔNG làm bài kiểm mất sức: thiếu nhánh `variant_refs` thì
        chính `9951`/`9952` hiện ra (đã kiểm bằng đột biến).
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            outcome = run_check(
                session,
                check_of("usage_counter_accurate"),
                branch_id=context.branch_id,
                # Mẫu mặc định cắt ở 50 dòng và câu check KHÔNG có `ORDER BY`,
                # nên với đủ khoản lệch không liên quan, dòng của tệp này có thể
                # rơi khỏi mẫu và bài kiểm xanh vì không thấy gì — xanh vacuous.
                # Nới hạn mẫu, rồi khẳng định mẫu đã chứa TRỌN tập lệch.
                sample_limit=1_000,
            )
        assert outcome.total == len(outcome.sample), (
            "mẫu bị cắt: bài kiểm không còn thấy đủ tập lệch để kết luận"
        )
        mine = {
            (ITEM_VARIANT_TABLE_NAME, VARIANT_A_ID),
            (ITEM_VARIANT_TABLE_NAME, VARIANT_B_ID),
            (ITEM_VARIANT_TABLE_NAME, DISPOSABLE_VARIANT_ID),
            (EMPLOYEE_TABLE_NAME, SALESPERSON_ID),
            (EMPLOYEE_TABLE_NAME, SECOND_SALESPERSON_ID),
            (PARTNER_TABLE_NAME, CUSTOMER_ID),
            (PARTNER_TABLE_NAME, OTHER_CUSTOMER_ID),
            (PARTNER_TABLE_NAME, PLAIN_CUSTOMER_ID),
        }
        offenders = [
            row for row in outcome.sample if (row["entity_type"], row["entity_id"]) in mine
        ]
        assert offenders == []


class TestMergingItemsCannotDropAReferencedVariant:
    """Gộp hai mã hàng trùng nhau KHÔNG được xóa quy cách đang có chứng từ dùng.

    `ItemVariantMergeHook.before_move` bỏ dòng quy cách của bản nguồn khi bản đích
    đã có **cùng mã** — và docstring của chính nó gọi đó là ca thường gặp, vì hai
    bản ghi trùng thường là cùng một mặt hàng nên có cùng bộ màu/size. Từ 7G-2a
    dòng hóa đơn bán trỏ tới `item_variants.id`, nên lượt bỏ ấy để lại một
    `variant_id` trỏ vào hư không: `LEFT JOIN` cho `NULL`, doanh thu rơi vào nhóm
    "Không khai quy cách", và sổ cái vẫn đúng từng đồng nên không có gì đối chiếu ra.

    Bộ dữ liệu của tệp này dựng sẵn đúng hình dạng ấy: `ITEM_A` và `ITEM_B` mỗi bên
    một quy cách **cùng mã** `QC-M`, và `books` có chứng từ trỏ cả hai.
    """

    def test_the_merge_hook_refuses_instead_of_deleting(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
        books: dict[str, UUID],
    ) -> None:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            assert _variant_uses(session, VARIANT_A_ID) > 0
            with pytest.raises(MasterDataInUseError):
                ItemVariantMergeHook().before_move(
                    session, source_id=ITEM_A_ID, target_id=ITEM_B_ID
                )
            # Chặn TRƯỚC khi xóa, không phải sau: dòng quy cách còn nguyên, nên
            # không có lượt gộp nào dở dang kể cả khi người gọi bắt lỗi này.
            session.rollback()
        with unit_of_work(session_factory, scope) as session:
            assert session.get(ItemVariant, VARIANT_A_ID) is not None


class TestVariantBelongsToItem:
    def test_a_variant_of_another_item_is_rejected(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Quy cách của mã hàng KHÁC là một id tồn tại mà vẫn sai.

        Không chặn thì sổ chi tiết theo mã quy cách xếp doanh thu của hàng A vào
        nhóm quy cách của hàng B, và cặp (mã hàng, quy cách) không diễn đạt được
        bằng một khóa ngoại một cột nên phép kiểm phải ở service.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            service = SalesInvoiceService(session)
            payload = _invoice(
                context,
                accounts,
                lines=(
                    SalesInvoiceLineIn(
                        description="Hàng A gắn quy cách của hàng B",
                        item_id=ITEM_A_ID,
                        unit_id=UNIT_ID,
                        variant_id=VARIANT_B_ID,
                        quantity=Decimal(1),
                        unit_price_fc=GOODS_AMOUNT,
                        amount_fc=GOODS_AMOUNT,
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["5111"],
                        vat_account_id=None,
                    ),
                ),
            )
            with pytest.raises(PostingValidationError) as raised:
                service.create(payload, user_id=ACTOR_ID)
        assert [violation.code for violation in raised.value.violations] == [
            VARIANT_NOT_OF_ITEM_CODE
        ]

    def test_a_mismatched_line_is_caught_even_when_a_later_line_uses_that_variant_right(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Phép kiểm so từng CẶP (mã hàng, quy cách), không tra quy cách → mã hàng.

        Bản đầu dựng một `dict` khóa `variant_id`, nên hai dòng khai CÙNG quy cách
        với hai mã hàng khác nhau gộp thành một mục và **dòng cuối thắng**: hóa
        đơn dưới đây — dòng sai đứng trước, dòng đúng đứng sau — đi qua phép kiểm
        trót lọt, rồi sổ chi tiết theo quy cách xếp doanh thu của hàng A vào nhóm
        quy cách của hàng B. Đảo thứ tự hai dòng thì bản cũ lại chặn đúng, nên chỉ
        thứ tự này bắt được.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            service = SalesInvoiceService(session)
            payload = _invoice(
                context,
                accounts,
                lines=(
                    _line(accounts, item_id=ITEM_A_ID, variant_id=VARIANT_B_ID),
                    _line(accounts, item_id=ITEM_B_ID, variant_id=VARIANT_B_ID),
                ),
            )
            with pytest.raises(PostingValidationError) as raised:
                service.create(payload, user_id=ACTOR_ID)
        assert [violation.code for violation in raised.value.violations] == [
            VARIANT_NOT_OF_ITEM_CODE
        ]

    def test_the_update_path_checks_the_variant_too(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Sửa hóa đơn đi qua cùng phép kiểm.

        Hai đường ghi cùng gọi `_verify_variants_belong_to_items`, và một phép
        kiểm chỉ gắn ở đường tạo là một phép kiểm sửa-là-lách-được.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            service = SalesInvoiceService(session)
            saved = service.create(
                _invoice(
                    context,
                    accounts,
                    lines=(_line(accounts, item_id=ITEM_A_ID, variant_id=VARIANT_A_ID),),
                ),
                user_id=ACTOR_ID,
            )
            payload = _invoice(
                context,
                accounts,
                lines=(_line(accounts, item_id=ITEM_A_ID, variant_id=VARIANT_B_ID),),
            )
            with pytest.raises(PostingValidationError) as raised:
                service.update(
                    saved.id,
                    SalesInvoiceUpdate(  # type: ignore[arg-type]
                        **payload.model_dump(), row_version=saved.row_version
                    ),
                    expected_row_version=saved.row_version,
                    user_id=ACTOR_ID,
                )
            service.delete(saved.id)
        assert [violation.code for violation in raised.value.violations] == [
            VARIANT_NOT_OF_ITEM_CODE
        ]

    def test_a_variant_in_use_cannot_be_deleted_from_the_catalog(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Quy cách đang nằm trên một dòng hóa đơn thì không xóa được (BR-SYS-02).

        Không có phép canh này, đường xóa danh mục — và đường **gộp hai mã hàng
        trùng nhau**, vốn XÓA quy cách của bản nguồn khi bản đích đã có cùng mã —
        để lại `variant_id` trỏ vào hư không: `LEFT JOIN` của sổ chi tiết theo mã
        quy cách cho `NULL`, doanh thu của dòng rơi vào nhóm "Không khai quy cách",
        và sổ cái vẫn đúng từng đồng nên không có gì đối chiếu ra được.

        Bộ đếm tham chiếu là phép canh duy nhất mở cho kernel:
        `item_variant_service` không được phép đọc bảng của module (luật C1).

        Quy cách riêng của bài kiểm này, vì bốn chứng từ của `books` đều trỏ
        `VARIANT_A_ID` — bộ đếm của nó không bao giờ về 0 trong phiên.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            service = SalesInvoiceService(session)
            variants = ItemVariantService(session)
            assert _variant_uses(session, DISPOSABLE_VARIANT_ID) == 0

            saved = service.create(
                _invoice(
                    context,
                    accounts,
                    lines=(_line(accounts, item_id=ITEM_A_ID, variant_id=DISPOSABLE_VARIANT_ID),),
                ),
                user_id=ACTOR_ID,
            )
            assert _variant_uses(session, DISPOSABLE_VARIANT_ID) == 1
            with pytest.raises(MasterDataInUseError):
                variants.delete(DISPOSABLE_VARIANT_ID, item_id=ITEM_A_ID)

            # Trả bộ đếm rồi xóa ĐƯỢC — vế thứ hai này là thứ phân biệt "đếm
            # đúng" với "luôn luôn chặn". Không có nó, một phép canh chặn mọi lượt
            # xóa cũng xanh y như thế.
            service.delete(saved.id)
            assert _variant_uses(session, DISPOSABLE_VARIANT_ID) == 0
            variants.delete(DISPOSABLE_VARIANT_ID, item_id=ITEM_A_ID)

    def test_a_line_may_not_name_a_variant_without_naming_the_item(
        self, accounts: dict[str, int]
    ) -> None:
        """Quy cách chỉ duy nhất TRONG một mã hàng, nên nó vô nghĩa khi dòng
        không khai vật tư — chặn ở schema, trước khi tới DB."""
        with pytest.raises(ValidationError, match="quy cách phải có vật tư"):
            SalesInvoiceLineIn(
                description="Dịch vụ khai quy cách",
                variant_id=VARIANT_A_ID,
                amount_fc=SERVICE_AMOUNT,
                vat_amount_fc=Decimal(0),
                account_id=accounts["5112"],
            )
