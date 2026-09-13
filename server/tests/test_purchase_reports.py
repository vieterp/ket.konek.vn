"""Mười báo cáo mua hàng & công nợ phải trả (`docs/srs/05` §5, lát 7G-1).

Tất cả đi qua chính đường preview của report engine — đăng ký hoàn toàn bằng
metadata, không một dòng renderer riêng. Bốn nhóm bất biến, và cả bốn đều là
chỗ một báo cáo có thể ra **số sai mà vẫn ra số**:

* **Trả lại hàng mua trừ ra khỏi tổng mua**, không cộng vào. SRS §5 #1 nêu ba
  thứ trong một báo cáo ("Mua hàng, trả lại, giảm giá"); cộng dương thì con số
  "đã mua" phóng đại đúng hai lần phần đã trả lại.
* **Tiền trên báo cáo BẰNG tiền trên sổ, từng đồng, kể cả ngoại tệ.** Dòng hàng
  chỉ có cột nguyên tệ, nên đường duy nhất không lệch là đọc `gl_postings` chứ
  không nhân lại tỷ giá.
* **Chứng từ mới Cất không lọt vào báo cáo** — và không lọt bằng *cấu trúc*
  (`gl_postings` chỉ chứa chứng từ đã ghi sổ), không bằng một điều kiện phải nhớ.
* **Công nợ đọc sổ phụ, không đọc bảng hóa đơn mua.** Nợ ghi tay bằng bút toán
  (7C-3) và khoản ứng trước (7C-4) cũng là công nợ; đọc `purchase_invoices` là
  bỏ sót chúng. Cộng một bài ghim hai dataset công nợ **cùng** ánh xạ chiều: lệch
  một giá trị thì bảng tuổi nợ và bảng chi tiết nói hai điều khác nhau về cùng
  một khoản nợ.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_open_invoice, seed_opening_advance
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.employee import EMPLOYEE_TABLE_NAME
from ket.kernel.master_data.usage import usage_count_of
from ket.kernel.periods.models import (
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.modules.purchase.models import LandedCostAllocation, PurchaseInvoiceKind
from ket.modules.purchase.schemas import (
    LandedCostIn,
    PurchaseInvoiceIn,
    PurchaseInvoiceLineIn,
    PurchaseSettlementIn,
)
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.engine.models import GlPosting, Ledger
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)
from ket.settings import Settings
from posting_support import (
    PostingContext,
    ensure_second_branch,
    posting_scope,
    seed_posting_context,
)
from purchase_support import (
    ensure_buyer,
    ensure_item,
    ensure_payment_term,
    ensure_project,
    ensure_unit,
    ensure_vendor,
    seed_purchase_package_data,
)

pytestmark = pytest.mark.db

ACTOR_ID = 1
REPORT_ROLE = "xem_bao_cao_mua_hang"

# Cửa sổ riêng của tệp này (tháng 4/2026) — `dataset_alpha` dùng chung, tách dữ
# liệu bằng THỜI GIAN chứ không bằng thứ tự chạy (bài học 5D).
APR_01 = date(2026, 4, 1)
APR_10 = date(2026, 4, 10)
APR_12 = date(2026, 4, 12)
APR_30 = date(2026, 4, 30)
# Lượt trả nợ SAU mốc chốt — nguyên liệu của bài kiểm "số đã trả tính tại ngày".
MAY_20 = date(2026, 5, 20)
MAY_31 = date(2026, 5, 31)
# Mốc chốt ngoài mọi niên độ đã khai — nguyên liệu của bài kiểm CTE `fy`.
BEYOND_FY = date(2027, 3, 31)

# Khối id 98xx là của tệp này. `id` cố định là quy ước sẵn của bộ test danh mục
# (`path` phải là chuỗi id có dấu chấm), nhưng nó biến mỗi con số thành một
# **tài nguyên dùng chung cả phiên**: bản đầu của lát này lấy 9401, đúng số
# `test_voucher_print_api` dùng cho khách hàng CÓ ĐỊA CHỈ — `ensure_vendor` ở
# đây chạy trước (thứ tự tệp theo chữ cái), tạo bản ghi không địa chỉ, rồi
# fixture của bài in thấy "đã có" nên không tạo nữa, và bài in phiếu thu đỏ vì
# ô "Địa chỉ" trống. Cùng cái bẫy mà `test_einvoice_flow` đã sập và đã ghi lại;
# khối riêng là cách duy nhất không phải dò lại ở lát sau.
VENDOR_ID = 9801
OTHER_VENDOR_ID = 9802
OPENING_VENDOR_ID = 9803
BUYER_ID = 9811
SECOND_BUYER_ID = 9812
ITEM_A_ID = 9821
ITEM_B_ID = 9822
UNIT_ID = 9831
PROJECT_ID = 9841
SECOND_PROJECT_ID = 9842
PAYMENT_TERM_ID = 9851

VENDOR_CODE = "NCC-7G1-01"
OTHER_VENDOR_CODE = "NCC-7G1-02"
OPENING_VENDOR_CODE = "NCC-7G1-03"
BUYER_CODE = "NV-7G1-MUA"
SECOND_BUYER_CODE = "NV-7G1-MUA2"
ITEM_A_CODE = "VT-7G1-A"
ITEM_B_CODE = "VT-7G1-B"
PROJECT_CODE = "CT-7G1"
SECOND_PROJECT_CODE = "CT-7G1-B"

GOODS_AMOUNT = Decimal(2_000_000)
GOODS_VAT = Decimal(200_000)
RETURN_AMOUNT = Decimal(500_000)
RETURN_VAT = Decimal(50_000)
FREIGHT_AMOUNT = Decimal(120_000)

# Hóa đơn nhỏ bị trả lại TRỌN VẸN — khoản nợ duy nhất trong tệp đã tất toán,
# tức điều kiện để bài kiểm `open_only` có gì mà bỏ. Đặt trên NCC thứ hai để
# không xô lệch những tổng theo NCC thứ nhất mà các bài khác khẳng định.
CLOSED_AMOUNT = Decimal(300_000)
CLOSED_VAT = Decimal(30_000)

# Lượt trả lại thứ hai của hóa đơn tháng 4, ghi sổ tháng 5.
LATE_RETURN_AMOUNT = Decimal(200_000)
LATE_RETURN_VAT = Decimal(20_000)

# Trả lại MỘT PHẦN hóa đơn ngoại tệ, ở TỶ GIÁ KHÁC tỷ giá ghi nhận nợ. Đây là
# nguyên liệu duy nhất bắt được lỗi cơ sở tiền: cột `amount` của bảng đối trừ là
# VND theo tỷ giá THANH TOÁN, còn số VND giải phóng trên sổ là `amount - fx_diff`.
# Trả TRỌN VẸN không bắt được (phần còn treo nguyên tệ về 0 nên dòng rơi khỏi
# bảng tuổi nợ), và hóa đơn VND cũng không (`fx_diff` luôn 0 ở tỷ giá 1).
FX_RETURN_AMOUNT_FC = Decimal("400.00")
FX_RETURN_RATE = Decimal(25_110)

# Niên độ 2027 + số dư đầu kỳ của một chi nhánh KHÁC: hình dạng "chi nhánh A đã
# chuyển số dư sang năm sau, chi nhánh B chưa". `run_carry_forward` là job
# per-branch, nên đây là trạng thái vận hành thường, không phải ca hiếm.
FY_LATER_CODE = "NIEN-DO-SAU-7G1"
CARRIED_DEBT = Decimal(444_000)
CARRIED_VENDOR_ID = 9804
CARRIED_VENDOR_CODE = "NCC-7G1-04"

# Số dư đầu kỳ TK 331 của NCC thứ ba: một hóa đơn còn nợ và một khoản ta đã trả
# trước. Khoản trả trước phải đi NGƯỢC chiều nợ của dòng cha (luật kernel ở
# `SettlementTargetKind.OPENING_ADVANCE`), nên nó thuộc phía PHẢI THU.
OPENING_DEBT = Decimal(1_000_000)
OPENING_ADVANCE = Decimal(300_000)

# Hóa đơn ngoại tệ: tỷ giá cố ý KHÔNG tròn để phép "nhân lại tỷ giá" lệch thấy
# được. 1.234,5 × 24.317 = 30.019.336,5 → sổ làm tròn một lần, ở một chỗ.
FX_AMOUNT_FC = Decimal("1234.50")
FX_RATE = Decimal(24_317)

# Khoản nợ ghi tay (bút toán nghiệp vụ gõ thẳng vào TK 331 — đường ghi sổ phụ
# thứ ba từ 7C-3). Số lẻ để nó không trùng bất kỳ tổng nào khác trong tệp.
MANUAL_DEBT = Decimal(777_000)

# Điều khoản thanh toán 30 ngày, gán cho NCC thứ hai. Nó tồn tại để bảng tuổi nợ
# có **hai** nhóm khác nhau: khoản của NCC thứ nhất không có hạn ('khong-han'),
# khoản ghi tay của NCC thứ hai đáo hạn sau kỳ ('chua-den-han'). Một bài kiểm
# thứ tự nhóm trên dữ liệu chỉ có một nhóm là bài kiểm không kiểm gì.
DUE_DAYS = 30


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
    codes = seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        ensure_payment_term(session, term_id=PAYMENT_TERM_ID, code="NO30-7G1", due_days=DUE_DAYS)
        ensure_vendor(session, partner_id=VENDOR_ID, code=VENDOR_CODE)
        ensure_vendor(
            session,
            partner_id=OTHER_VENDOR_ID,
            code=OTHER_VENDOR_CODE,
            payment_term_id=PAYMENT_TERM_ID,
        )
        ensure_vendor(session, partner_id=OPENING_VENDOR_ID, code=OPENING_VENDOR_CODE)
        ensure_buyer(session, employee_id=BUYER_ID, code=BUYER_CODE)
        # Nhân viên và công trình THỨ HAI: không có chúng, hai bài kiểm bộ lọc
        # không phân biệt được "lọc theo id" với "bỏ dòng để trống" — đổi dataset
        # thành `IS NOT NULL` thì cả hai vẫn xanh.
        ensure_buyer(session, employee_id=SECOND_BUYER_ID, code=SECOND_BUYER_CODE)
        ensure_unit(session, unit_id=UNIT_ID, code="Cái")
        ensure_item(session, item_id=ITEM_A_ID, code=ITEM_A_CODE, unit_id=UNIT_ID)
        ensure_item(session, item_id=ITEM_B_ID, code=ITEM_B_CODE, unit_id=UNIT_ID)
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
    """Bốn chứng từ đã ghi sổ, một chứng từ CHỈ cất, và một khoản nợ ghi tay.

    Một bộ dữ liệu cho cả mười báo cáo: mỗi bài dưới đây đọc phần của nó và
    khẳng định trên những con số biết trước ở đầu tệp.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        service = PurchaseInvoiceService(session)

        goods = service.create(
            _invoice(
                context,
                accounts,
                buyer_id=BUYER_ID,
                landed_costs=(_freight(accounts),),
            ),
            user_id=ACTOR_ID,
        )
        service.post(goods.id, user_id=ACTOR_ID)
        ids["goods"] = goods.id
        # Đích đối trừ là dòng SỔ PHỤ, không phải chứng từ: `target_id` khớp
        # `SettlementTargetSource.target_id` của kernel, và khóa chính của
        # `ar_ap_ledger` là UUID của dòng nợ (quyết định 7A).
        payable = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == goods.id)
        ).scalar_one()

        # Trả lại hàng: đối trừ hóa đơn gốc (hình dạng D1 của 7B), nên nó KHÔNG
        # sinh dòng sổ phụ mới — nhưng vẫn phải hiện trên báo cáo mua hàng với
        # dấu âm.
        returned = service.create(
            _invoice(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                buyer_id=BUYER_ID,
                posting_date=APR_12,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Trả lại hàng A",
                        item_id=ITEM_A_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(2),
                        unit_price_fc=Decimal(250_000),
                        amount_fc=RETURN_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=RETURN_VAT,
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                        project_id=PROJECT_ID,
                    ),
                ),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=payable.id,
                        amount_fc=RETURN_AMOUNT + RETURN_VAT,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(returned.id, user_id=ACTOR_ID)
        ids["return"] = returned.id

        # Hóa đơn ngoại tệ — nguyên liệu của bài "báo cáo khớp sổ từng đồng".
        fx = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=OTHER_VENDOR_ID,
                currency_code="USD",
                exchange_rate=FX_RATE,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Hàng nhập khẩu",
                        item_id=ITEM_B_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(3),
                        unit_price_fc=Decimal("411.50"),
                        amount_fc=FX_AMOUNT_FC,
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["156"],
                        vat_account_id=None,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(fx.id, user_id=ACTOR_ID)
        ids["fx"] = fx.id

        # Chứng từ CHỈ cất, không ghi sổ — nó không được xuất hiện ở đâu cả.
        draft = service.create(
            _invoice(context, accounts, posting_date=APR_12, description="chứng từ chưa ghi sổ"),
            user_id=ACTOR_ID,
        )
        ids["draft"] = draft.id

        # Hóa đơn nhỏ bị trả lại TRỌN VẸN: khoản nợ duy nhất đã tất toán trong
        # tệp. Không có nó, `open_only` không có gì mà bỏ và bài kiểm của nó xanh
        # vì tập rỗng — xanh mà không kiểm gì.
        closed = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=OTHER_VENDOR_ID,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Hàng trả lại hết",
                        item_id=ITEM_B_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=CLOSED_AMOUNT,
                        amount_fc=CLOSED_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=CLOSED_VAT,
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(closed.id, user_id=ACTOR_ID)
        ids["closed"] = closed.id
        closed_payable = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == closed.id)
        ).scalar_one()
        closed_return = service.create(
            _invoice(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                vendor_id=OTHER_VENDOR_ID,
                posting_date=APR_12,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Trả lại hết",
                        item_id=ITEM_B_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=CLOSED_AMOUNT,
                        amount_fc=CLOSED_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=CLOSED_VAT,
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                    ),
                ),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=closed_payable.id,
                        amount_fc=CLOSED_AMOUNT + CLOSED_VAT,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(closed_return.id, user_id=ACTOR_ID)
        ids["closed_return"] = closed_return.id

        # Lượt trả lại thứ hai của hóa đơn tháng 4, GHI SỔ THÁNG 5. Đọc báo cáo
        # với mốc chốt 30/04 thì lượt này chưa được tính — đó là cả nội dung của
        # bài kiểm "số đã trả tính tại ngày", và là lỗi CHẶN mà vòng review tìm ra.
        late_return = service.create(
            _invoice(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                buyer_id=BUYER_ID,
                posting_date=MAY_20,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Trả lại thêm tháng 5",
                        item_id=ITEM_A_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=LATE_RETURN_AMOUNT,
                        amount_fc=LATE_RETURN_AMOUNT,
                        vat_rate=Decimal(10),
                        vat_amount_fc=LATE_RETURN_VAT,
                        account_id=accounts["156"],
                        vat_account_id=accounts["1331"],
                        project_id=PROJECT_ID,
                    ),
                ),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=payable.id,
                        amount_fc=LATE_RETURN_AMOUNT + LATE_RETURN_VAT,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(late_return.id, user_id=ACTOR_ID)
        ids["late_return"] = late_return.id

        # Hóa đơn của nhân viên mua và công trình THỨ HAI — để bộ lọc có gì mà
        # loại ra, chứ không chỉ có gì để giữ lại.
        other_dims = service.create(
            _invoice(
                context,
                accounts,
                vendor_id=OTHER_VENDOR_ID,
                buyer_id=SECOND_BUYER_ID,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Hàng của nhân viên thứ hai",
                        item_id=ITEM_B_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=Decimal(150_000),
                        amount_fc=Decimal(150_000),
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["156"],
                        vat_account_id=None,
                        project_id=SECOND_PROJECT_ID,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(other_dims.id, user_id=ACTOR_ID)
        ids["other_dims"] = other_dims.id

        # Trả lại MỘT PHẦN hóa đơn ngoại tệ, ở tỷ giá khác — xem ghi chú của
        # FX_RETURN_AMOUNT_FC về vì sao chỉ hình dạng này bắt được lỗi cơ sở tiền.
        fx_payable = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == fx.id)
        ).scalar_one()
        fx_return = service.create(
            _invoice(
                context,
                accounts,
                kind=PurchaseInvoiceKind.RETURN,
                operation="tra-lai-hang-mua",
                vendor_id=OTHER_VENDOR_ID,
                posting_date=APR_12,
                currency_code="USD",
                exchange_rate=FX_RETURN_RATE,
                lines=(
                    PurchaseInvoiceLineIn(
                        description="Trả lại một phần hàng nhập khẩu",
                        item_id=ITEM_B_ID,
                        unit_id=UNIT_ID,
                        quantity=Decimal(1),
                        unit_price_fc=FX_RETURN_AMOUNT_FC,
                        amount_fc=FX_RETURN_AMOUNT_FC,
                        vat_rate=Decimal(0),
                        vat_amount_fc=Decimal(0),
                        account_id=accounts["156"],
                        vat_account_id=None,
                    ),
                ),
                settlements=(
                    PurchaseSettlementIn(
                        target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                        target_id=fx_payable.id,
                        amount_fc=FX_RETURN_AMOUNT_FC,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(fx_return.id, user_id=ACTOR_ID)
        ids["fx_return"] = fx_return.id

        # Khoản phải trả ghi tay — đi qua ĐƯỜNG GHI THẬT (`general_ledger.
        # journal`, nguồn sổ phụ thứ ba từ 7C-3), không chèn tay dòng
        # `ar_ap_ledger`: bảng ấy đòi khoản nợ phải trỏ về một chứng từ nguồn
        # (`ck_ar_ap_ledger_has_a_source_document`), và một dòng chèn tay để lách
        # ràng buộc đó sẽ chứng minh báo cáo đọc được một hình dạng dữ liệu mà
        # hệ thật không bao giờ sinh ra.
        journal = JournalVoucherService(session)
        manual = journal.create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=APR_10,
                posting_date=APR_10,
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="Nợ ghi tay bằng bút toán",
                lines=(
                    JournalLineIn(
                        account_id=accounts["642"],
                        debit_fc=MANUAL_DEBT,
                        credit_fc=Decimal(0),
                    ),
                    JournalLineIn(
                        account_id=accounts["331"],
                        debit_fc=Decimal(0),
                        credit_fc=MANUAL_DEBT,
                        partner_id=OTHER_VENDOR_ID,
                        partner_kind=PartnerKind.VENDOR,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        journal.post(manual.id, user_id=ACTOR_ID)
        ids["manual_debt"] = manual.id
    return ids


@pytest.fixture(scope="module")
def manual_debt_no(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    books: dict[str, UUID],
) -> str:
    """Số chứng từ của bút toán ghi nợ tay — do bộ cấp số đặt, không chép tay.

    Ghim một chuỗi `GLE26-00001` vào bài kiểm là ghim thứ tự chạy của cả bộ
    test: dãy số dùng chung dataset, nên số thật phụ thuộc tệp nào cấp trước.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        entry = session.execute(
            select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == books["manual_debt"])
        ).scalar_one()
        return entry.document_no


@pytest.fixture(scope="module")
def opening_rows(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    """Số dư đầu kỳ TK 331: một hóa đơn còn nợ + một khoản ta đã trả trước.

    Không có dòng nào như thế, nhánh thứ hai của khối UNION trong cả hai dataset
    công nợ **chưa bao giờ có dữ liệu** — và bài kiểm "hai dataset đồng ý về
    chiều nợ" xanh vacuous đúng ở nhánh nó tuyên bố ghim. Đây là lỗ phủ mà vòng
    review tìm ra, và nó che một lỗi số tiền thật.
    """
    debt = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=OPENING_VENDOR_ID,
        amount_fc=OPENING_DEBT,
        invoice_no="HD-DAU-KY-7G1",
        invoice_date=date(2025, 12, 20),
        due_date=date(2026, 2, 20),
    )
    advance = seed_opening_advance(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=OPENING_VENDOR_ID,
        amount=OPENING_ADVANCE,
    )
    return {"debt": debt, "advance": advance}


def _invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    kind: int = PurchaseInvoiceKind.GOODS,
    operation: str = "mua-hang-hoa",
    vendor_id: int = VENDOR_ID,
    buyer_id: int | None = None,
    posting_date: date = APR_10,
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
    description: str = "mua hàng 7G-1",
    lines: tuple[PurchaseInvoiceLineIn, ...] | None = None,
    landed_costs: tuple[LandedCostIn, ...] = (),
    settlements: tuple[PurchaseSettlementIn, ...] = (),
) -> PurchaseInvoiceIn:
    return PurchaseInvoiceIn(
        kind=kind,
        operation_code=operation,
        vendor_id=vendor_id,
        buyer_id=buyer_id,
        payable_account_id=accounts["331"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        vendor_invoice_no="0000777",
        landed_cost_allocation=LandedCostAllocation.BY_VALUE,
        description=description,
        lines=lines
        or (
            PurchaseInvoiceLineIn(
                description="Hàng A",
                item_id=ITEM_A_ID,
                unit_id=UNIT_ID,
                quantity=Decimal(8),
                unit_price_fc=Decimal(250_000),
                amount_fc=GOODS_AMOUNT,
                vat_rate=Decimal(10),
                vat_amount_fc=GOODS_VAT,
                account_id=accounts["156"],
                vat_account_id=accounts["1331"],
                project_id=PROJECT_ID,
            ),
        ),
        landed_costs=landed_costs,
        settlements=settlements,
    )


def _freight(accounts: dict[str, int]) -> LandedCostIn:
    return LandedCostIn(
        description="Vận chuyển",
        vendor_id=None,
        credit_account_id=accounts["111"],
        amount_fc=FREIGHT_AMOUNT,
        vat_rate=Decimal(0),
        vat_amount_fc=Decimal(0),
    )


class PreviewResult:
    """Thân trả về của `/preview` → dòng dữ liệu + tiêu đề nhóm.

    Cùng hình dạng với `test_cash_bank_book_reports.PreviewResult`; giữ bản
    riêng ở đây thay vì tách ra module dùng chung cho tới khi có tệp thứ ba cần
    nó (một lớp dùng chung cho hai tệp là một lớp phải đoán nhu cầu của tệp thứ
    ba).
    """

    def __init__(self, body: dict[str, object]) -> None:
        columns = body["columns"]
        assert isinstance(columns, list)
        self.column_keys = [str(column["key"]) for column in columns]
        rows = body["rows"]
        assert isinstance(rows, list)
        self.rows: list[dict[str, str]] = []
        self.headings: list[str] = []
        self.totals: list[dict[str, str]] = []
        for row in rows:
            cells = row.get("cells")
            if row["kind"] in ("group_header", "group_footer") and row.get("heading"):
                self.headings.append(str(row["heading"]))
            if row["kind"] in ("group_footer", "grand_total") and cells:
                self.totals.append(self._total_cells(row))
            if row["kind"] != "data" or not cells:
                continue
            self.rows.append(self._cells(cells))

    def _cells(self, cells: object) -> dict[str, str]:
        assert isinstance(cells, list)
        return {key: str(cell["text"]) for key, cell in zip(self.column_keys, cells, strict=True)}

    def _total_cells(self, row: dict[str, object]) -> dict[str, str]:
        """Dòng tổng KHÔNG thẳng cột với dòng dữ liệu.

        `presentation.total_cells` dựng một ô nhãn gộp `label_span` cột đầu, rồi
        mỗi cột từ `label_span` trở đi một ô. Zip nó với cả danh sách cột sẽ gán
        con số của cột tiền cho cột đầu tiên của layout — bài kiểm khi ấy đọc
        `KeyError`, hoặc tệ hơn, đọc con số của một cột khác.
        """
        cells = row["cells"]
        assert isinstance(cells, list)
        span = int(row.get("label_span", 0) or 0)
        return {
            key: str(cell["text"])
            for key, cell in zip(self.column_keys[span:], cells[1:], strict=True)
        }

    def texts(self) -> list[str]:
        return ["|".join(row.values()) for row in self.rows]

    def all_text(self) -> str:
        return "\n".join([*self.headings, *self.texts()])

    def total(self, key: str) -> Decimal:
        """Dòng tổng CUỐI của báo cáo — tổng từng nhóm nằm ở các dòng trước."""
        assert self.totals, "báo cáo không có dòng tổng nào"
        return money(self.totals[-1], key)

    def sum_of(self, key: str) -> Decimal:
        return sum((money(row, key) for row in self.rows), Decimal(0))


def money(row: dict[str, str], key: str) -> Decimal:
    """Ô tiền đã định dạng → `Decimal`. Ô rỗng = 0 (renderer bỏ trắng số 0).

    Định dạng của `kernel.formatting.format_money` là kiểu Việt Nam: dấu chấm
    phân cách nghìn, dấu **phẩy** phân cách thập phân, dấu trừ đứng trước. Bỏ cả
    hai dấu phân cách (cách viết đầu tiên của hàm này) biến `30.019.336,5` thành
    `300193365` — gấp mười lần, trên một cột tiền, và bài kiểm "báo cáo khớp sổ"
    đỏ vì chính bộ đọc của nó chứ không vì báo cáo.
    """
    text = row[key].strip().replace("\u00a0", "").replace(" ", "")
    if text in ("", "-"):
        return Decimal(0)
    return Decimal(text.replace(".", "").replace(",", "."))


def _branch_scope(dataset: DatasetRef, branch_id: int) -> RequestScope:
    """Phạm vi của một người dùng đứng ở ĐÚNG `branch_id`.

    Bước gieo của bài "chuyển số dư per-branch" ghi vào một chi nhánh KHÁC chi
    nhánh của `context`, và RLS canh lượt GHI theo phạm vi của người ghi: dùng
    `posting_scope` thì `INSERT` bị chính RLS từ chối, còn `branch_ids` trống
    nghĩa là "không chi nhánh nào" chứ không phải "mọi chi nhánh".
    """
    return RequestScope(
        dataset_schema=dataset.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(branch_id,),
        acting_branch_id=branch_id,
    )


Preview = Callable[..., PreviewResult]


@pytest.fixture(scope="module")
def report_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Quyền báo cáo **và** quyền xem hóa đơn mua.

    `reporting.report.view` một mình không đủ: mười định nghĩa của lát này khai
    `required_permission_module = "purchase"`, đúng bản vá H-1b của 6E-1 — một
    mã quyền báo cáo chung không được mở dữ liệu của phân hệ mà người dùng không
    có quyền đọc.
    """
    return ensure_role(
        session_factory,
        dataset_alpha,
        REPORT_ROLE,
        [
            "reporting.report.view",
            permission_code("purchase", "invoice", Action.VIEW),
            # Chiều phải thu có mặt vì một bài kiểm đọc `tuoi-no-phai-thu` để
            # khẳng định khoản phải TRẢ không lọt sang đó. Khẳng định "không lọt"
            # chỉ có nghĩa khi lượt đọc ấy đi qua được cổng quyền — nếu không nó
            # xanh vì 403, tức xanh vì một lý do khác lý do nó viết ra.
            permission_code("sales", "invoice", Action.VIEW),
        ],
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
            "bao_cao_mua_hang",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    def run(code: str, **params: object) -> PreviewResult:
        body = {
            "params": {
                "from_date": APR_01.isoformat(),
                "to_date": APR_30.isoformat(),
                **params,
            }
        }
        response = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return PreviewResult(response.json())

    return run


class TestReturnsSubtract:
    def test_a_return_invoice_lowers_the_purchase_total_instead_of_raising_it(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Tổng mua của một NCC = giá trị nhập − phần trả lại.

        Bài này là phép trừ duy nhất ngăn "tổng hợp mua hàng" phóng đại: nếu
        dòng trả lại cộng dương thì tổng sẽ là `goods + freight + return` chứ
        không phải `goods + freight − return`, tức lệch đúng hai lần phần trả
        lại — và không có gì trên tờ báo cáo nói rằng nó lệch.
        """
        report = preview("tong-hop-mua-hang-theo-nha-cung-cap", vendor_id=VENDOR_ID)
        expected = GOODS_AMOUNT + FREIGHT_AMOUNT - RETURN_AMOUNT
        assert report.sum_of("amount") == expected
        assert report.total("amount") == expected
        # Và phần trả lại thật sự có mặt (một báo cáo bỏ hẳn dòng trả lại cũng
        # cho ra đúng tổng nếu ai đó lọc `kind = 4` đi).
        assert any(money(row, "amount") < 0 for row in report.rows)

    def test_the_return_line_carries_a_negative_quantity_too(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Dấu áp cho MỌI cột lượng và tiền cùng lúc.

        Nhân dấu ở cột tiền mà quên cột lượng là tờ báo cáo có số lượng dương
        với tiền âm — và cột "số lượng mua trong kỳ" của nó nói dối.
        """
        report = preview("tong-hop-mua-hang-theo-mat-hang", item_id=ITEM_A_ID)
        negatives = [row for row in report.rows if money(row, "amount") < 0]
        assert negatives, "không thấy dòng trả lại"
        assert all(money(row, "quantity") < 0 for row in negatives)


class TestMoneyEqualsTheLedger:
    def test_the_register_totals_match_the_postings_on_the_goods_account(
        self,
        preview: Preview,
        books: dict[str, UUID],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Tiền trên báo cáo = tiền trên sổ, từng đồng, kể cả hóa đơn ngoại tệ.

        Đây là lý do dataset đọc `gl_postings` thay vì nhân lại tỷ giá: hóa đơn
        USD ở fixture có tỷ giá cố ý không tròn, nên một phép quy đổi thứ hai sẽ
        lệch. Bài kiểm so trực tiếp với chính bảng phát sinh, không so với một
        con số chép tay.
        """
        report = preview("so-chi-tiet-mua-hang")
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            booked = session.execute(
                select(func.coalesce(func.sum(GlPosting.debit - GlPosting.credit), 0)).where(
                    GlPosting.account_id == accounts["156"],
                    GlPosting.ledger == Ledger.FINANCIAL,
                    GlPosting.posting_date >= APR_01,
                    GlPosting.posting_date <= APR_30,
                )
            ).scalar_one()
        assert report.sum_of("amount") == Decimal(booked)

    def test_the_value_column_includes_allocated_purchase_cost(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """`amount` là GIÁ TRỊ NHẬP KHO (BR-PUR-01), không phải tiền hàng thuần.

        Chi phí mua phân bổ ghi vào CHÍNH tài khoản hàng với CHÍNH
        `source_line_id` của dòng hàng, nên nó nằm trong cùng một tổng và không
        tách ra được. Ghim điều đó ở đây để lát sau không "sửa" thành tiền hàng
        thuần rồi làm báo cáo thôi khớp sổ.
        """
        report = preview(
            "so-chi-tiet-mua-hang", vendor_id=VENDOR_ID, kind=PurchaseInvoiceKind.GOODS
        )
        assert report.sum_of("amount") == GOODS_AMOUNT + FREIGHT_AMOUNT
        assert report.sum_of("goods_amount_fc") == GOODS_AMOUNT
        assert report.sum_of("landed_cost_fc") == FREIGHT_AMOUNT


class TestOnlyPostedDocuments:
    def test_a_saved_but_unposted_invoice_reaches_no_purchase_report(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Chứng từ mới Cất không lọt vào báo cáo (BR-RPT-01).

        Nó không lọt vì `gl_postings` chưa có dòng nào của nó — tức bằng cấu
        trúc, không bằng một điều kiện `status = 2` mà người viết dataset sau
        phải nhớ chép lại.
        """
        for code in ("so-chi-tiet-mua-hang", "so-nhat-ky-mua-hang"):
            assert "chứng từ chưa ghi sổ" not in preview(code).all_text()


class TestPayablesReadTheSubledger:
    def test_debt_written_by_a_journal_voucher_appears_in_the_payable_detail(
        self, preview: Preview, books: dict[str, UUID], manual_debt_no: str
    ) -> None:
        """Nợ không có hóa đơn mua vẫn là nợ.

        Từ 7C-3 `general_ledger.journal` là nguồn ghi `ar_ap_ledger` thứ ba, và
        từ 7C-4 khoản ứng trước cũng vào đó. Báo cáo công nợ đọc bảng hóa đơn
        mua sẽ bỏ sót cả hai — và bỏ sót im lặng, vì nó vẫn ra số cho phần còn
        lại.
        """
        report = preview("chi-tiet-cong-no-phai-tra", partner_id=OTHER_VENDOR_ID)
        text = report.all_text()
        assert manual_debt_no in text
        assert "Phải trả ghi tay" in text
        assert MANUAL_DEBT in [money(row, "remaining") for row in report.rows]

    def test_the_invoice_view_shows_value_settled_and_remaining(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """SRS §5 #6 nêu đúng ba con số, và phần trả lại hàng là phần "đã trả".

        Hóa đơn gốc bị chứng từ trả lại đối trừ `RETURN_AMOUNT + RETURN_VAT`, nên
        khoản nợ của nó còn lại đúng phần chênh.
        """
        report = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=VENDOR_ID)
        invoice_rows = [row for row in report.rows if row["source_label"] == "Hóa đơn mua"]
        assert len(invoice_rows) == 1
        row = invoice_rows[0]
        gross = GOODS_AMOUNT + GOODS_VAT
        assert money(row, "amount") == gross
        assert money(row, "settled") == RETURN_AMOUNT + RETURN_VAT
        assert money(row, "remaining") == gross - (RETURN_AMOUNT + RETURN_VAT)

    def test_open_only_drops_the_fully_settled_items(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """`open_only` bỏ khoản đã trả hết; mặc định KHÔNG lọc.

        Mặc định phải là "hiện cả khoản đã trả hết": báo cáo chi tiết theo hóa
        đơn nêu cả ba con số, nên một hóa đơn đã trả hết vẫn là một dòng có nghĩa
        trong kỳ.

        Bài kiểm khẳng định trên một khoản **đã tất toán có thật** (hóa đơn nhỏ
        bị trả lại trọn vẹn). Bản đầu chỉ so `len(open) <= len(everything)` và
        `all(remaining != 0)` trên một tập **không có** dòng nào đã tất toán —
        hai khẳng định ấy đúng tầm thường, và chúng xanh y như thế nếu `:open_only`
        bị nối sai, bị bỏ, hay lọc ngược chiều.
        """
        everything = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=OTHER_VENDOR_ID)
        open_items = preview(
            "chi-tiet-cong-no-phai-tra-theo-hoa-don",
            partner_id=OTHER_VENDOR_ID,
            open_only=True,
        )
        closed_rows = [row for row in everything.rows if money(row, "remaining") == 0]
        assert closed_rows, "fixture không có khoản nào đã tất toán"
        assert len(open_items.rows) == len(everything.rows) - len(closed_rows)
        assert all(money(row, "remaining_fc") > 0 for row in open_items.rows)


class TestTheTwoDebtDatasetsAgree:
    def test_every_target_kind_lands_on_the_same_side_in_both_datasets(
        self, preview: Preview, books: dict[str, UUID], manual_debt_no: str
    ) -> None:
        """Ánh xạ `target_kind` → chiều phải TRÙNG giữa hai dataset công nợ.

        `ar_ap_aging` (7A) và `ar_ap_open_items` (7G-1) chia nhau cùng một câu
        hỏi "khoản này là phải thu hay phải trả". Lệch một giá trị thì bảng tuổi
        nợ và bảng chi tiết nói hai điều khác nhau về cùng một khoản nợ, và
        không có con số nào trên hai tờ giấy ấy chỉ ra chỗ lệch.

        So bằng **chứng từ có mặt**, không bằng văn bản SQL: một bài so chuỗi
        SQL sẽ xanh khi hai tệp cùng sai giống nhau.
        """
        payable_detail = preview("chi-tiet-cong-no-phai-tra")
        payable_aging = preview("chi-tiet-tuoi-no-phai-tra")
        assert manual_debt_no in payable_detail.all_text()
        assert manual_debt_no in payable_aging.all_text()
        # Chiều ngược: khoản phải trả KHÔNG được lọt sang phía phải thu.
        assert manual_debt_no not in preview("tuoi-no-phai-thu").all_text()

    def test_the_aging_detail_orders_buckets_the_way_a_reader_expects(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Nhóm tuổi nợ sắp bằng `bucket_seq`, không bằng chữ cái của `bucket`.

        Thứ tự chữ cái là '1-30' < '31-60' < '61-90' < 'chua-den-han' <
        'khong-han' < 'tren-90' — nó xếp nhóm **chưa đến hạn** vào giữa các nhóm
        quá hạn, và một bảng tuổi nợ như thế đọc ngược hẳn ý nghĩa.

        Bài kiểm so trên MỌI nhóm có mặt chứ không hai nhóm đoán trước, và đòi
        có **ít nhất hai** nhóm: một bài kiểm thứ tự trên dữ liệu một nhóm là bài
        kiểm xanh mà không kiểm gì. Fixture bảo đảm hai nhóm bằng cách cho NCC
        thứ hai một điều khoản thanh toán và NCC thứ nhất không có.
        """
        reading_order = ("khong-han", "chua-den-han", "1-30", "31-60", "61-90", "tren-90")
        report = preview("chi-tiet-tuoi-no-phai-tra")
        seen = [heading for heading in report.headings if heading in reading_order]
        assert len(set(seen)) >= 2, f"chỉ thấy {set(seen)} — dữ liệu không đủ để so thứ tự"
        ranks = [reading_order.index(heading) for heading in seen]
        assert ranks == sorted(ranks)


class TestSettledAsOfTheCutOffDate:
    """Lỗi CHẶN của vòng review: số "đã thanh toán" phải là số TẠI `:to_date`.

    `ar_ap_ledger.settled` là scalar **chạy** và bảng không có cột ngày đối trừ,
    nên đọc thẳng cột ấy cho ra một báo cáo mà kỳ đã khóa vẫn tự đổi số khi có
    lượt trả nợ ở kỳ sau. Đây là con số kế toán mang đi đối chiếu với nhà cung
    cấp, và đối chiếu với TK 331 trên bảng cân đối.
    """

    def test_a_settlement_posted_after_the_cut_off_does_not_lower_the_debt(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hóa đơn tháng 4, trả một phần tháng 4 và một phần tháng 5.

        Đọc với mốc 30/04 phải thấy **chỉ** phần tháng 4; đọc với mốc 31/05 thấy
        cả hai. Cùng một hóa đơn, hai mốc, hai con số — và cả hai đều đúng.
        """
        april = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=VENDOR_ID)
        may = preview(
            "chi-tiet-cong-no-phai-tra-theo-hoa-don",
            partner_id=VENDOR_ID,
            to_date=MAY_31.isoformat(),
        )
        gross = GOODS_AMOUNT + GOODS_VAT
        april_row = next(row for row in april.rows if row["source_label"] == "Hóa đơn mua")
        may_row = next(row for row in may.rows if row["source_label"] == "Hóa đơn mua")

        assert money(april_row, "settled") == RETURN_AMOUNT + RETURN_VAT
        assert money(april_row, "remaining") == gross - (RETURN_AMOUNT + RETURN_VAT)
        assert money(may_row, "settled") == (
            RETURN_AMOUNT + RETURN_VAT + LATE_RETURN_AMOUNT + LATE_RETURN_VAT
        )
        assert money(may_row, "remaining") < money(april_row, "remaining")

    def test_the_aging_report_honours_the_same_cut_off(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Bảng tuổi nợ của 7A mang cùng khiếm khuyết, nên nó cũng phải hết.

        Nếu hai dataset công nợ trả lời khác nhau về cùng một hóa đơn tại cùng
        một mốc thì người dùng không có cách nào biết tờ nào đúng.
        """
        detail = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=VENDOR_ID)
        aging = preview("chi-tiet-tuoi-no-phai-tra")
        detail_row = next(row for row in detail.rows if row["source_label"] == "Hóa đơn mua")
        aging_rows = [row for row in aging.rows if row["invoice_no"] == detail_row["document_no"]]
        assert len(aging_rows) == 1
        assert money(aging_rows[0], "remaining") == money(detail_row, "remaining")

    def test_a_settlement_on_an_unposted_voucher_is_not_counted(
        self,
        preview: Preview,
        books: dict[str, UUID],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Dòng đối trừ có mặt từ lúc CẤT, nhưng chỉ tính khi đã GHI SỔ.

        `_write_settlements` ghi dòng lúc cất, còn phần nhích `settled` của khoản
        đích chỉ xảy ra ở `apply_settlements` lúc ghi sổ. Một khối `settled_as_of`
        cộng mọi dòng trong bảng vì thế làm một khoản nợ trông như đã trả trong
        khi chưa có đồng nào lên sổ — và người dùng sẽ thôi đi đòi nó.
        """
        before = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=VENDOR_ID)
        before_row = next(row for row in before.rows if row["source_label"] == "Hóa đơn mua")

        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            payable = session.execute(
                select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == books["goods"])
            ).scalar_one()
            service = PurchaseInvoiceService(session)
            draft = service.create(
                _invoice(
                    context,
                    accounts,
                    kind=PurchaseInvoiceKind.RETURN,
                    operation="tra-lai-hang-mua",
                    posting_date=APR_12,
                    description="trả lại chưa ghi sổ",
                    lines=(
                        PurchaseInvoiceLineIn(
                            description="Trả lại nháp",
                            item_id=ITEM_A_ID,
                            unit_id=UNIT_ID,
                            quantity=Decimal(1),
                            unit_price_fc=Decimal(100_000),
                            amount_fc=Decimal(100_000),
                            vat_rate=Decimal(10),
                            vat_amount_fc=Decimal(10_000),
                            account_id=accounts["156"],
                            vat_account_id=accounts["1331"],
                        ),
                    ),
                    settlements=(
                        PurchaseSettlementIn(
                            target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                            target_id=payable.id,
                            amount_fc=Decimal(110_000),
                        ),
                    ),
                ),
                user_id=ACTOR_ID,
            )
        try:
            after = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=VENDOR_ID)
            after_row = next(row for row in after.rows if row["source_label"] == "Hóa đơn mua")
            assert money(after_row, "settled") == money(before_row, "settled")
            assert money(after_row, "remaining") == money(before_row, "remaining")
        finally:
            with unit_of_work(session_factory, scope) as session:
                PurchaseInvoiceService(session).delete(draft.id)


class TestTheVndBasisMatchesTheLedger:
    """Cột VND của báo cáo công nợ phải cùng CƠ SỞ với sổ phụ.

    Bảng đối trừ có hai con số VND cho một lượt trả nợ: `amount` theo tỷ giá
    **thanh toán**, và `amount - fx_diff` theo tỷ giá **ghi nhận nợ** — con số
    thứ hai là thứ `apply_settlement_rows` cộng vào khoản đích, phần chênh đi vào
    515/635 (FR-SYS-066). Cộng `amount` trần làm cột VND lệch trên **mọi** khoản
    nợ ngoại tệ trả từng phần, và lệch theo hướng tệ nhất: `settled` vượt `amount`
    thì `remaining` ra số ÂM trên một khoản đã tất toán.
    """

    def test_remaining_equals_the_subledger_on_a_partly_settled_foreign_invoice(
        self,
        preview: Preview,
        books: dict[str, UUID],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        """So với CHÍNH sổ phụ, không với một con số chép tay.

        Mọi lượt đối trừ của hóa đơn này đều trước mốc chốt, nên "đã trả tại
        `:to_date`" phải trùng `ar_ap_ledger.settled` từng đồng. Một bài chép tay
        con số kỳ vọng sẽ chép luôn cơ sở tiền sai nếu người viết chọn sai.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            entry = session.execute(
                select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == books["fx"])
            ).scalar_one()
            booked_settled = entry.settled
            booked_remaining = entry.amount - entry.settled
            booked_remaining_fc = entry.amount_fc - entry.settled_fc

        # Tỷ giá trả lại khác tỷ giá ghi nhận, nếu không bài kiểm không kiểm gì.
        assert FX_RETURN_RATE != FX_RATE
        assert booked_settled > 0

        report = preview("chi-tiet-cong-no-phai-tra-theo-hoa-don", partner_id=OTHER_VENDOR_ID)
        rows = [row for row in report.rows if money(row, "remaining_fc") == booked_remaining_fc]
        assert len(rows) == 1, "không tìm thấy đúng một dòng của hóa đơn ngoại tệ"
        assert money(rows[0], "settled") == booked_settled
        assert money(rows[0], "remaining") == booked_remaining

    def test_the_aging_report_shares_the_same_vnd_basis(
        self,
        preview: Preview,
        books: dict[str, UUID],
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
    ) -> None:
        """`ar_ap_aging` là dataset ĐÃ GIAO — nó phải giữ đúng cơ sở ấy.

        Bản 7A đúng cơ sở bằng cách đọc thẳng `amount - settled` của bảng. Lượt
        sửa mốc chốt thay phép trừ ấy bằng một khối cộng lại, nên nó có thể **lấy
        đi** tính đúng đã có — đây là bài canh chỗ đó.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            entry = session.execute(
                select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == books["fx"])
            ).scalar_one()
            booked_remaining = entry.amount - entry.settled

        aging = preview("chi-tiet-tuoi-no-phai-tra")
        assert booked_remaining in [money(row, "remaining") for row in aging.rows]

    def test_no_debt_reports_a_negative_remaining(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Không khoản nợ nào được in ra số còn lại ÂM.

        `settled` cộng sai cơ sở vượt `amount` trên khoản đã tất toán bằng ngoại
        tệ, và `remaining` ra số âm. Một bảng công nợ có dòng âm là một bảng không
        ai cộng lại được, nên phép kiểm này rẻ mà chặn cả họ lỗi cơ sở tiền.
        """
        for code in (
            "chi-tiet-cong-no-phai-tra",
            "chi-tiet-cong-no-phai-tra-theo-hoa-don",
            "tong-hop-cong-no-phai-tra",
            "chi-tiet-tuoi-no-phai-tra",
        ):
            report = preview(code)
            assert all(money(row, "remaining") >= 0 for row in report.rows), code

    def test_the_two_debt_datasets_report_the_same_remaining(
        self, preview: Preview, books: dict[str, UUID], opening_rows: dict[str, UUID]
    ) -> None:
        """Chuông báo lệch giữa hai bản chép của `settled_as_of`.

        Khối cộng số đã trả và khối UNION hai nguồn có mặt ở CẢ HAI dataset công
        nợ (tệp SQL dataset không include được nhau). Hai bản chép sẽ lệch ở lần
        sửa đầu tiên chỉ chạm một tệp, và không con số nào trên hai tờ giấy chỉ ra
        chỗ lệch — nên phép so đứng ở đây.

        So tổng phần còn treo: bảng tuổi nợ chỉ liệt kê khoản còn treo, nên vế
        kia phải bật `open_only`.
        """
        detail = preview("chi-tiet-cong-no-phai-tra", open_only=True)
        aging = preview("chi-tiet-tuoi-no-phai-tra")
        assert detail.rows and aging.rows
        # So cột VND: layout chi tiết theo TK công nợ không hiện cột nguyên tệ,
        # và VND mới là cột mà lỗi cơ sở tiền làm lệch.
        assert detail.sum_of("remaining") == aging.sum_of("remaining")


class TestOpeningBalanceRows:
    """Lỗi CHẶN thứ hai: khoản ứng trước ĐẦU KỲ và mốc chốt ngoài niên độ."""

    def test_an_opening_advance_sits_opposite_the_parent_debt(
        self, preview: Preview, books: dict[str, UUID], opening_rows: dict[str, UUID]
    ) -> None:
        """Luật của kernel: ứng trước đi NGƯỢC chiều nợ của dòng cha.

        Dòng cha nhóm PHẢI TRẢ, nên khoản ta trả trước người bán là **quyền** của
        ta và thuộc phía phải thu. Bản đầu của `ar_ap_aging` xếp chiều chỉ theo
        `detail_kind`, nên nó in khoản ấy thành một khoản ta còn NỢ: trên dữ liệu
        này bảng tuổi nợ phải trả nói 1.300.000 trong khi TK 331 ròng là 700.000.
        """
        payable = preview("chi-tiet-cong-no-phai-tra", partner_id=OPENING_VENDOR_ID)
        payable_amounts = [money(row, "remaining") for row in payable.rows]
        assert OPENING_DEBT in payable_amounts
        assert OPENING_ADVANCE not in payable_amounts
        assert sum(payable_amounts, Decimal(0)) == OPENING_DEBT

        labels = [row["source_label"] for row in payable.rows]
        assert "Số dư đầu kỳ" in labels
        assert "Ứng trước đầu kỳ" not in labels

    def test_an_opening_advance_is_present_on_the_receivable_side(
        self, preview: Preview, books: dict[str, UUID], opening_rows: dict[str, UUID]
    ) -> None:
        """Nửa DƯƠNG của bài trên: khoản ứng trước phải CÓ MẶT ở chiều phải thu.

        Hai khẳng định phủ định ("không nằm ở phía phải trả") vẫn xanh khi khoản
        ứng trước biến mất khỏi **mọi** chiều — đúng hành vi hỏng mà điều kiện
        `invoice_date <= :to_date` gây ra, và đúng một trong hai lỗi lát này
        tuyên bố sửa. Không có khẳng định dương thì lượt sửa ấy không có bài nào
        canh.
        """
        receivable = preview("tuoi-no-phai-thu")
        assert OPENING_ADVANCE in [money(row, "remaining") for row in receivable.rows]

    def test_both_debt_datasets_place_the_opening_advance_on_the_same_side(
        self, preview: Preview, books: dict[str, UUID], opening_rows: dict[str, UUID]
    ) -> None:
        """Chuông báo lệch giữa hai dataset công nợ, trên NHÁNH SỐ DƯ ĐẦU KỲ.

        Bài "hai dataset đồng ý" có trước chỉ so được nhánh `ar_ap_ledger`, vì
        fixture chưa có dòng số dư đầu kỳ nào. Nhánh thứ hai là đúng chỗ hai
        dataset đã lệch thật.
        """
        aging_amounts = [
            money(row, "remaining") for row in preview("chi-tiet-tuoi-no-phai-tra").rows
        ]
        assert OPENING_DEBT in aging_amounts
        assert OPENING_ADVANCE not in aging_amounts

    def test_carried_debt_survives_a_cut_off_beyond_the_fiscal_year(
        self, preview: Preview, books: dict[str, UUID], opening_rows: dict[str, UUID]
    ) -> None:
        """Mốc chốt ngoài mọi niên độ không được làm nợ mang sang biến mất.

        CTE `fy` của bản đầu lấy niên độ **chứa** `:to_date`, và `validate_params`
        không ràng `:to_date` phải nằm trong một niên độ nào — nên đọc với mốc
        31/03/2027 trả 0 dòng, im lặng, không cảnh báo. Trả 0 dòng là cách hỏng
        tệ nhất của một báo cáo công nợ: nó trông như "không còn nợ ai".
        """
        for code in ("chi-tiet-cong-no-phai-tra", "chi-tiet-tuoi-no-phai-tra"):
            report = preview(code, to_date=BEYOND_FY.isoformat())
            amounts = [money(row, "remaining") for row in report.rows]
            assert OPENING_DEBT in amounts, code


class TestCarryForwardIsPerBranch:
    """Niên độ của số dư đầu kỳ phải chọn theo TỪNG CHI NHÁNH.

    `run_carry_forward` là job per-branch ("chuyển số dư của chi nhánh nào là
    việc của người đứng ở chi nhánh đó"), nên "chi nhánh A đã chuyển, B chưa" là
    trạng thái vận hành **thường**. Một niên độ chọn cho cả sổ làm nợ đầu kỳ của
    mọi chi nhánh chưa chuyển **biến mất im lặng** — và lượt đọc gộp nhiều chi
    nhánh là lượt đọc mặc định.

    Bài này không cần chạy job chuyển số dư thật: nó chỉ cần hình dạng dữ liệu
    mà job ấy để lại — một chi nhánh có dòng ở niên độ sau, một chi nhánh không.
    """

    def test_a_branch_that_has_not_carried_forward_keeps_its_opening_debt(
        self,
        client: TestClient,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        user_factory: UserFactory,
        test_password: str,
        context: PostingContext,
        report_role: str,
        accounts: dict[str, int],
        books: dict[str, UUID],
        opening_rows: dict[str, UUID],
    ) -> None:
        # `ensure_second_branch` trả về chi nhánh MỚI NHẤT, và khi chạy cả bộ thì
        # chi nhánh mới nhất có thể chính là chi nhánh của tệp này — lúc ấy "hai
        # chi nhánh" là một, cả hai dòng số dư rơi vào cùng chi nhánh, và bài
        # kiểm đỏ vì luật per-branch làm **đúng** việc của nó. Phải chọn tường
        # minh một chi nhánh KHÁC.
        ensure_second_branch(session_factory, dataset_alpha)
        first_scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, first_scope) as session:
            ensure_vendor(session, partner_id=CARRIED_VENDOR_ID, code=CARRIED_VENDOR_CODE)
            # Dùng lại NIÊN ĐỘ SAU nào đã có, chỉ tạo khi chưa có cái nào.
            # `fiscal_years` không mang chi nhánh và không bị RLS, nên nó là tài
            # nguyên dùng chung cả phiên: tạo thẳng một niên độ 2027 thì tệp test
            # nào đã tạo niên độ ấy trước sẽ làm lượt tạo này đổ
            # `FiscalYearOverlapError` — và chỉ đổ khi chạy cả bộ.
            this_year_start = session.scalar(
                select(FiscalYear.start_date).where(FiscalYear.id == context.fiscal_year_id)
            )
            assert this_year_start is not None
            later_year = session.scalar(
                select(FiscalYear)
                .where(FiscalYear.start_date > this_year_start)
                .order_by(FiscalYear.start_date)
                .limit(1)
            )
            if later_year is None:
                later_year = PeriodService(session).create_fiscal_year(
                    code=FY_LATER_CODE,
                    start_date=date(this_year_start.year + 1, 1, 1),
                    accounting_scheme=AccountingScheme.TT99,
                    base_currency="VND",
                    inventory_valuation_method=InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING,
                    vat_method=VatMethod.DEDUCTION,
                )
            year_id = later_year.id
            cut_off = later_year.start_date + timedelta(days=30)
            second_branch_id = session.scalar(
                select(Branch.id).where(Branch.id != context.branch_id).order_by(Branch.id).limit(1)
            )
            assert second_branch_id is not None, "cần ít nhất hai chi nhánh"
            second_branch_code = session.scalar(
                select(Branch.code).where(Branch.id == second_branch_id)
            )
            assert second_branch_code is not None

        # Gieo dưới phạm vi của CHÍNH chi nhánh thứ hai: RLS canh lượt ghi theo
        # phạm vi người ghi, nên một scope không chứa chi nhánh ấy bị từ chối.
        with unit_of_work(
            session_factory, _branch_scope(dataset_alpha, second_branch_id)
        ) as session:
            existing = session.scalar(
                select(OpeningBalance.id).where(
                    OpeningBalance.fiscal_year_id == year_id,
                    OpeningBalance.branch_id == second_branch_id,
                    OpeningBalance.partner_id == CARRIED_VENDOR_ID,
                )
            )
            if existing is None:
                parent = OpeningBalance(
                    fiscal_year_id=year_id,
                    ledger=0,
                    branch_id=second_branch_id,
                    account_id=context.accounts["331"],
                    currency_code="VND",
                    exchange_rate=Decimal(1),
                    partner_id=CARRIED_VENDOR_ID,
                    partner_kind=PartnerKind.VENDOR.value,
                    detail_kind=OpeningDetailKind.PAYABLE,
                    debit_fc=Decimal(0),
                    credit_fc=CARRIED_DEBT,
                    debit=Decimal(0),
                    credit=CARRIED_DEBT,
                )
                session.add(parent)
                session.flush()
                session.add(
                    OpeningBalanceInvoice(
                        opening_balance_id=parent.id,
                        branch_id=parent.branch_id,
                        invoice_no="HD-CF-7G1",
                        invoice_date=later_year.start_date - timedelta(days=2),
                        due_date=later_year.start_date + timedelta(days=58),
                        amount_fc=CARRIED_DEBT,
                        amount=CARRIED_DEBT,
                    )
                )
                session.flush()

        # Người đọc thấy CẢ HAI chi nhánh — đó là lượt đọc mặc định, và cũng là
        # lượt đọc duy nhất mà lỗi này lộ ra.
        headers = {
            **actor(
                client,
                session_factory,
                dataset_alpha,
                user_factory,
                report_role,
                "bao_cao_hai_chi_nhanh",
                test_password,
                branch_codes=[context.branch_code, second_branch_code],
            ),
            BRANCH_HEADER: str(context.branch_id),
        }
        response = client.post(
            "/api/v1/reports/chi-tiet-cong-no-phai-tra/preview",
            json={"params": {"from_date": APR_01.isoformat(), "to_date": cut_off.isoformat()}},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        amounts = [money(row, "remaining") for row in PreviewResult(response.json()).rows]

        # Chi nhánh đã chuyển: khoản của niên độ 2027.
        assert CARRIED_DEBT in amounts
        # Chi nhánh CHƯA chuyển: khoản của niên độ 2026 vẫn phải còn — nó là một
        # khoản nợ chưa ai trả đồng nào.
        assert OPENING_DEBT in amounts


class TestTheFilterDimensions:
    """FR-PUR-040: năm chiều lọc có sẵn cột được giao (user chốt 2026-09-13)."""

    def test_the_buyer_filter_narrows_to_one_employees_purchases(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Lọc theo id, không phải "bỏ dòng để trống".

        Fixture có nhân viên mua THỨ HAI đúng vì bài này: chỉ khẳng định "không
        có nhóm Chưa khai" thì một dataset đổi thành `buyer_id IS NOT NULL` vẫn
        xanh, trong khi báo cáo lọc theo nhân viên A in cả hàng của nhân viên B.
        """
        # Khẳng định trên báo cáo GỘP theo nhân viên: sổ chi tiết mua hàng không
        # có cột nhân viên nào, nên chữ `BUYER_CODE` không bao giờ xuất hiện ở đó
        # và một bài kiểm đọc nó chỉ kiểm được rằng báo cáo có dòng.
        code = "tong-hop-mua-hang-theo-nhan-vien"
        everything = preview(code)
        assert SECOND_BUYER_CODE in everything.all_text()

        filtered = preview(code, buyer_id=BUYER_ID)
        assert filtered.rows
        assert BUYER_CODE in filtered.all_text()
        assert "Chưa khai nhân viên mua" not in filtered.all_text()
        assert SECOND_BUYER_CODE not in filtered.all_text()
        assert len(filtered.rows) < len(everything.rows)

    def test_the_project_filter_narrows_to_one_site(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        code = "tong-hop-mua-hang-theo-cong-trinh"
        everything = preview(code)
        assert SECOND_PROJECT_CODE in everything.all_text()

        filtered = preview(code, project_id=PROJECT_ID)
        assert filtered.rows
        assert PROJECT_CODE in filtered.all_text()
        assert "Không thuộc công trình" not in filtered.all_text()
        assert SECOND_PROJECT_CODE not in filtered.all_text()
        assert len(filtered.rows) < len(everything.rows)


class TestTheBuyerDimension:
    def test_invoices_group_under_the_buyer_and_the_unfilled_group_has_a_name(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Chiều gộp "theo nhân viên" của FR-PUR-040 có nguồn thật.

        Và chứng từ không khai người mua gộp vào một nhóm **có tên**: ô trống
        trên tiêu đề nhóm đọc như lỗi hiển thị, không như một câu trả lời.
        """
        report = preview("tong-hop-mua-hang-theo-nhan-vien")
        text = report.all_text()
        assert BUYER_CODE in text
        assert "Chưa khai nhân viên mua" in text

    def test_deleting_an_invoice_releases_the_buyer_reference(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        accounts: dict[str, int],
    ) -> None:
        """Bộ đếm tham chiếu nhích/lùi theo `buyer_id` (BR-SYS-02).

        Không có bước này thì nhân viên mua hàng xóa được trong khi chứng từ còn
        trỏ vào id của họ — và chiều gộp của báo cáo sẽ trỏ vào một dòng danh mục
        không còn nữa.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            before = usage_count_of(session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=BUYER_ID)
            service = PurchaseInvoiceService(session)
            invoice = service.create(
                _invoice(context, accounts, buyer_id=BUYER_ID, posting_date=APR_12),
                user_id=ACTOR_ID,
            )
            during = usage_count_of(session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=BUYER_ID)
            service.delete(invoice.id)
            after = usage_count_of(session, entity_type=EMPLOYEE_TABLE_NAME, entity_id=BUYER_ID)
        assert during == before + 1
        assert after == before


class TestGroupingDimensions:
    def test_the_four_summaries_read_one_dataset_and_still_group_differently(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Bốn chiều gộp của SRS §5 #1 dùng CHUNG `purchase_register`.

        Bằng chứng là bốn tờ báo cáo có bốn bộ tiêu đề nhóm khác nhau trên cùng
        một tập dòng: nếu layout không thật sự đổi chiều gộp thì bốn mã báo cáo
        khác nhau in ra cùng một tờ giấy.
        """
        by_item = preview("tong-hop-mua-hang-theo-mat-hang")
        by_vendor = preview("tong-hop-mua-hang-theo-nha-cung-cap")
        by_buyer = preview("tong-hop-mua-hang-theo-nhan-vien")
        by_project = preview("tong-hop-mua-hang-theo-cong-trinh")

        assert ITEM_A_CODE in "\n".join(by_item.headings)
        assert VENDOR_CODE in "\n".join(by_vendor.headings)
        assert BUYER_CODE in "\n".join(by_buyer.headings)
        assert PROJECT_CODE in "\n".join(by_project.headings)
        # Cùng dataset ⇒ cùng tổng cuối; chỉ cách chia nhóm là khác.
        assert by_item.total("amount") == by_vendor.total("amount")
        assert by_buyer.total("amount") == by_project.total("amount")

    def test_the_journal_groups_by_the_inventory_account(
        self, preview: Preview, books: dict[str, UUID], accounts: dict[str, int]
    ) -> None:
        """SRS §5 #3: "theo thời gian, chi tiết theo từng loại hàng tồn kho" —
        loại hàng tồn kho ở sổ là **tài khoản** hàng của dòng."""
        report = preview("so-nhat-ky-mua-hang")
        assert "156" in "\n".join(report.headings)
