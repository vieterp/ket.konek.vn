"""Bảy báo cáo công nợ phải thu (`docs/srs/06` §5.2, lát 7G-2b).

Cả bảy chạy trên **một** dataset — `ar_ap_open_items`, chính câu SQL mà lát
7G-2b rút `ar_ap_aging` vào. Chúng khác nhau ở layout và ở bộ tham số ghim, nên
những chỗ có thể sai không nằm trong SQL (7G-1 đã trả giá cho phần ấy ở chiều
phải trả) mà nằm ở ba chỗ mới của lát này:

* **`:due_state` chia đôi TRỌN VẸN.** SRS tách "phân tích quá hạn" và "phân tích
  trước hạn" thành hai tờ. Nếu phép chia để rơi một loại dòng — mà ứng viên rơi
  là khoản **không ghi hạn**, vì `NULL` trượt khỏi cả `<` lẫn `>=` — thì tổng hai
  tờ nhỏ hơn tổng nợ và không tờ nào nói ra phần thiếu.
* **Cột nhóm khách hàng** đi `parent_id`, và khách đứng ở gốc phải rơi vào một
  nhóm CÓ TÊN chứ không một tiêu đề trống.
* **`open_only` ghim trên ba tờ** (hai tờ phân tích + chi tiết theo tuổi nợ) và
  KHÔNG ghim trên bốn tờ còn lại: "chi tiết công nợ theo hóa đơn" nêu cả ba con
  số nên hóa đơn đã thu đủ vẫn là một dòng có nghĩa.

**Mọi khẳng định thu về đúng khách hàng của tệp này.** `dataset_alpha` dùng
chung cả phiên, và `test_purchase_reports` gieo một khoản ứng trước đầu kỳ nằm ở
chiều **phải thu** — một bài kiểm cộng tổng cả báo cáo sẽ xanh một mình và đỏ khi
chạy cả nhóm `db`. Bẫy ấy đã sập ba lần trong phase này (7G-1 hai lần, 7G-2a một
lần), nên ở đây mỗi phép cộng đều đi qua bộ lọc `partner_id`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_cash_book_package_data, seed_open_invoice
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.reports.loader import load_builtin_reports
from ket.kernel.contracts import PartnerKind
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.cash_book.models import CashVoucherKind
from ket.modules.cash_book.schemas import CashSettlementIn, CashVoucherIn, CashVoucherLineIn
from ket.modules.cash_book.service import CashVoucherService
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.models import OpeningDetailKind
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_unit, ensure_vendor
from report_preview_support import Preview, PreviewResult, money
from sales_support import ensure_customer, ensure_customer_group, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
REPORT_ROLE = "xem_bao_cao_cong_no_phai_thu"

# Cửa sổ riêng của tệp này (tháng 8–9/2026): 7G-1 dùng tháng 4–5, 7G-2a dùng
# tháng 6–7. `dataset_alpha` dùng chung, nên hai tệp tách dữ liệu bằng THỜI GIAN
# chứ không bằng thứ tự chạy (bài học 5D).
AUG_01 = date(2026, 8, 1)
AUG_05 = date(2026, 8, 5)
AUG_10 = date(2026, 8, 10)
AUG_20 = date(2026, 8, 20)
SEP_10 = date(2026, 9, 10)
SEP_15 = date(2026, 9, 15)
SEP_20 = date(2026, 9, 20)
SEP_30 = date(2026, 9, 30)
OCT_15 = date(2026, 10, 15)

# Khối id 96xx là của tệp này (98xx của `test_purchase_reports`, 99xx của
# `test_sales_reports`). `id` cố định là quy ước sẵn của bộ test danh mục, nhưng
# nó biến mỗi con số thành tài nguyên dùng chung cả phiên — và khối id riêng
# CHƯA ĐỦ: mã người dùng đặt (`units_of_measure.code` duy nhất toàn bảng) cũng
# phải riêng, đúng chỗ 7G-2a đỏ khi chạy cả nhóm.
GROUP_NORTH_ID = 9620
GROUP_DEALER_ID = 9628
GROUP_SOUTH_ID = 9629
CUSTOMER_ID = 9621
OTHER_CUSTOMER_ID = 9622
UNGROUPED_CUSTOMER_ID = 9623
FX_CUSTOMER_ID = 9624
VENDOR_ID = 9625
UNIT_ID = 9631
ITEM_ID = 9641
SECOND_ITEM_ID = 9642

GROUP_NORTH_CODE = "NHOM-7G2B-BAC"
GROUP_DEALER_CODE = "NHOM-7G2B-DAI-LY"
GROUP_SOUTH_CODE = "NHOM-7G2B-NAM"
CUSTOMER_CODE = "KH-7G2B-01"
OTHER_CUSTOMER_CODE = "KH-7G2B-02"
UNGROUPED_CUSTOMER_CODE = "KH-7G2B-03"
FX_CUSTOMER_CODE = "KH-7G2B-04"
VENDOR_CODE = "NCC-7G2B-01"
UNIT_CODE = "Cai-7G2B"
ITEM_CODE = "VT-7G2B"
SECOND_ITEM_CODE = "VT-7G2B-B"

UNGROUPED_LABEL = "Chưa phân nhóm"
OPENING_INVOICE_NO = "HD-DAU-KY-7G2B"
OPENING_PAYABLE_NO = "HD-DAU-KY-7G2B-NCC"

# Nợ PHẢI TRẢ đầu kỳ, trả một phần bằng phiếu chi. Nó ở đây vì một lý do duy
# nhất: chiều của khoản nợ mang sang KHÔNG suy được từ `target_kind` — số dư đầu
# kỳ mang `target_kind = 2` ở cả hai chiều, và chiều thật nằm ở `detail_kind` của
# dòng cha. Không có dòng phải trả nào thì một bản đọc chiều từ `target_kind` vẫn
# xanh, và tờ "thanh toán công nợ KHÁCH HÀNG" sẽ lặng lẽ liệt kê cả những lượt ta
# trả tiền cho nhà cung cấp.
OPENING_PAYABLE = Decimal(800_000)
PAYABLE_PAID = Decimal(300_000)

# Bốn khoản của khách hàng thứ nhất, chọn hạn để rơi vào bốn nhóm tuổi nợ KHÁC
# NHAU tại mốc chốt 30/09 — một bộ dữ liệu một nhóm không phân biệt được "lọc
# theo tình trạng hạn" với "không lọc gì".
NEAR_AMOUNT = Decimal(1_000_000)  # hạn 20/09 → quá hạn 10 ngày → nhóm '1-30'
NEAR_VAT = Decimal(100_000)
MID_AMOUNT = Decimal(2_000_000)  # hạn 20/08 → quá hạn 41 ngày → nhóm '31-60'
MID_VAT = Decimal(200_000)
# Hóa đơn "chưa đến hạn" tách HAI dòng hàng: trên hóa đơn một dòng, "không phân
# bổ phần còn nợ cho dòng hàng" và "phân bổ" cho ra cùng một con số, nên bài kiểm
# không phân biệt được hai cách làm.
FUTURE_GOODS_A = Decimal(2_000_000)
FUTURE_VAT_A = Decimal(200_000)
FUTURE_GOODS_B = Decimal(1_000_000)
FUTURE_VAT_B = Decimal(100_000)
FUTURE_AMOUNT = FUTURE_GOODS_A + FUTURE_GOODS_B  # hạn 15/10 → chưa đến hạn
FUTURE_VAT = FUTURE_VAT_A + FUTURE_VAT_B
OPENING_AMOUNT = Decimal(1_000_000)  # nợ mang sang, hạn 20/02 → nhóm 'tren-90'

# Ghi giảm một phần khoản '31-60', ghi sổ 10/09 — TRƯỚC mốc chốt, nên nó phải trừ.
PART_GOODS = Decimal(200_000)
PART_VAT = Decimal(20_000)
PART_SETTLED = PART_GOODS + PART_VAT

# Một lượt thu SỚM (20/08) của khoản '1-30'. Nó ở đây để cửa sổ kỳ của tờ thanh
# toán có cận DƯỚI kiểm được: không có lượt thu nào trước cửa sổ thì bỏ hẳn
# `>= :from_date` cũng không đổi kết quả, và bài kiểm không phân biệt được
# "cắt theo kỳ" với "cắt theo mốc chốt".
EARLY_GOODS = Decimal(150_000)
EARLY_VAT = Decimal(15_000)
EARLY_SETTLED = EARLY_GOODS + EARLY_VAT

# Khoản của khách hàng thứ hai: KHÔNG ghi hạn. Nó là dòng mà phép chia quá hạn /
# trước hạn dễ để rơi nhất, vì `NULL` trượt khỏi cả hai vế của một phép so ngày.
# Nó cũng được thu một phần, để tờ "ngày thanh toán" có một lượt thu của khoản
# KHÔNG có hạn — cột trễ hạn của dòng ấy phải để TRỐNG, không phải 0.
UNDATED_AMOUNT = Decimal(400_000)
UNDATED_VAT = Decimal(40_000)
UNDATED_PART_GOODS = Decimal(100_000)
UNDATED_PART_VAT = Decimal(10_000)
UNDATED_SETTLED = UNDATED_PART_GOODS + UNDATED_PART_VAT
UNDATED_REMAINING = UNDATED_AMOUNT + UNDATED_VAT - UNDATED_SETTLED
UNDATED_CUSTOMER_PAYER = OTHER_CUSTOMER_ID

# Lượt đối trừ của một chứng từ CHỈ CẤT, chưa ghi sổ: dòng đối trừ đã nằm trong
# bảng (`_write_settlements` chạy lúc cất) nhưng chưa đồng nào lên sổ, nên nó
# không được có mặt ở bất kỳ tờ nào.
DRAFT_GOODS = Decimal(300_000)
DRAFT_VAT = Decimal(30_000)
DRAFT_SETTLEMENT = DRAFT_GOODS + DRAFT_VAT

# Hóa đơn NGOẠI TỆ thu một phần ở tỷ giá KHÁC tỷ giá ghi nhận — khách hàng riêng
# để nó không xê dịch con số của ba khách kia. Đây là ca duy nhất phân biệt được
# `amount` với `amount - fx_diff`: cột `amount` của bảng đối trừ là VND theo tỷ
# giá THANH TOÁN, còn phần thật sự giải phóng trên sổ là `amount - fx_diff` (phần
# chênh đi vào 515/635). Trên hóa đơn VND hai cách cho cùng một con số, nên một
# bộ gieo toàn VND để lọt đúng lỗi mà vòng review 7G-1 đã bắt ở chiều phải trả.
FX_RATE = Decimal(24_317)
FX_SETTLE_RATE = Decimal(25_110)
FX_GOODS_FC = Decimal(1_000)
FX_SETTLE_FC = Decimal(400)

# Khoản của khách hàng không thuộc nhóm nào, THU ĐỦ trước mốc chốt: nó phải biến
# mất khỏi ba tờ ghim `open_only` và ở lại trên bốn tờ còn lại.
CLEARED_AMOUNT = Decimal(500_000)
CLEARED_VAT = Decimal(50_000)

NEAR_REMAINING = NEAR_AMOUNT + NEAR_VAT - EARLY_SETTLED
MID_REMAINING = MID_AMOUNT + MID_VAT - PART_SETTLED
FUTURE_REMAINING = FUTURE_AMOUNT + FUTURE_VAT
OVERDUE_TOTAL = NEAR_REMAINING + MID_REMAINING + OPENING_AMOUNT
NOT_OVERDUE_TOTAL = FUTURE_REMAINING
CUSTOMER_OPEN_TOTAL = OVERDUE_TOTAL + NOT_OVERDUE_TOTAL

AR_REPORTS = (
    "tong-hop-cong-no-phai-thu",
    "chi-tiet-cong-no-phai-thu",
    "chi-tiet-cong-no-phai-thu-theo-hoa-don",
    "phan-tich-cong-no-phai-thu-qua-han",
    "phan-tich-cong-no-phai-thu-truoc-han",
    "chi-tiet-tuoi-no-phai-thu",
    "tong-hop-cong-no-phai-thu-theo-nhom-khach-hang",
)
OPEN_ONLY_REPORTS = (
    "phan-tich-cong-no-phai-thu-qua-han",
    "phan-tich-cong-no-phai-thu-truoc-han",
    "chi-tiet-tuoi-no-phai-thu",
)


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
    # Hai bước gieo gói đều idempotent theo khóa tự nhiên trên cùng một gói, nên
    # gọi cả hai là hợp lệ: tệp này cần TK doanh thu của phân hệ bán VÀ TK quỹ
    # của phiếu chi trả nợ nhà cung cấp.
    codes |= seed_cash_book_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        # Cây SÂU HAI CẤP cho khách hàng thứ nhất — "Miền Bắc > Đại lý > khách".
        # Một cấp thôi thì nút cha trực tiếp và nút gốc là MỘT, và bài kiểm nhóm
        # không phân biệt được hai cách đi cây: đổi phép nối sang `split_part`
        # của `path` vẫn xanh. Đây là chỗ quyết định "nhóm = cha trực tiếp" thật
        # sự được canh.
        ensure_customer_group(session, partner_id=GROUP_NORTH_ID, code=GROUP_NORTH_CODE)
        ensure_customer_group(
            session,
            partner_id=GROUP_DEALER_ID,
            code=GROUP_DEALER_CODE,
            parent_id=GROUP_NORTH_ID,
        )
        ensure_customer_group(session, partner_id=GROUP_SOUTH_ID, code=GROUP_SOUTH_CODE)
        ensure_customer(
            session, partner_id=CUSTOMER_ID, code=CUSTOMER_CODE, group_id=GROUP_DEALER_ID
        )
        ensure_customer(
            session,
            partner_id=OTHER_CUSTOMER_ID,
            code=OTHER_CUSTOMER_CODE,
            group_id=GROUP_SOUTH_ID,
        )
        # Khách hàng đứng ở GỐC cây: nhóm của nó là nhóm "chưa phân nhóm", và
        # tiêu đề nhóm ấy phải có chữ chứ không được để trống.
        ensure_customer(session, partner_id=UNGROUPED_CUSTOMER_ID, code=UNGROUPED_CUSTOMER_CODE)
        ensure_customer(session, partner_id=FX_CUSTOMER_ID, code=FX_CUSTOMER_CODE)
        ensure_vendor(session, partner_id=VENDOR_ID, code=VENDOR_CODE)
        ensure_unit(session, unit_id=UNIT_ID, code=UNIT_CODE)
        ensure_item(session, item_id=ITEM_ID, code=ITEM_CODE, unit_id=UNIT_ID)
        ensure_item(session, item_id=SECOND_ITEM_ID, code=SECOND_ITEM_CODE, unit_id=UNIT_ID)
    return codes


def _line(
    accounts: dict[str, int], *, item_id: int, amount: Decimal, vat: Decimal
) -> SalesInvoiceLineIn:
    return SalesInvoiceLineIn(
        description=f"Hàng {item_id}",
        item_id=item_id,
        unit_id=UNIT_ID,
        quantity=Decimal(1),
        unit_price_fc=amount,
        amount_fc=amount,
        vat_rate=Decimal(10) if vat else Decimal(0),
        vat_amount_fc=vat,
        account_id=accounts["5111"],
        vat_account_id=accounts["33311"] if vat else None,
    )


def _invoice(
    context: PostingContext,
    accounts: dict[str, int],
    *,
    amount: Decimal,
    vat: Decimal,
    posting_date: date,
    due_date: date | None,
    customer_id: int = CUSTOMER_ID,
    kind: int = SalesInvoiceKind.GOODS,
    operation: str = "ban-hang-hoa",
    settlements: tuple[SalesSettlementIn, ...] = (),
    lines: tuple[SalesInvoiceLineIn, ...] | None = None,
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
) -> SalesInvoiceIn:
    return SalesInvoiceIn(
        kind=kind,
        operation_code=operation,
        customer_id=customer_id,
        receivable_account_id=accounts["131"],
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        due_date=due_date,
        currency_code=currency_code,
        exchange_rate=exchange_rate,
        description="công nợ phải thu 7G-2b",
        lines=lines or (_line(accounts, item_id=ITEM_ID, amount=amount, vat=vat),),
        settlements=settlements,
    )


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    """Năm hóa đơn bán + hai chứng từ ghi giảm, phủ bốn nhóm tuổi nợ và cả hai
    vế của phép chia quá hạn / trước hạn."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        service = SalesInvoiceService(session)

        for key, amount, vat, due in (
            ("near", NEAR_AMOUNT, NEAR_VAT, SEP_20),
            ("mid", MID_AMOUNT, MID_VAT, AUG_20),
        ):
            invoice = service.create(
                _invoice(
                    context, accounts, amount=amount, vat=vat, posting_date=AUG_05, due_date=due
                ),
                user_id=ACTOR_ID,
            )
            service.post(invoice.id, user_id=ACTOR_ID)
            ids[key] = invoice.id

        future = service.create(
            _invoice(
                context,
                accounts,
                amount=FUTURE_AMOUNT,
                vat=FUTURE_VAT,
                posting_date=AUG_10,
                due_date=OCT_15,
                lines=(
                    _line(accounts, item_id=ITEM_ID, amount=FUTURE_GOODS_A, vat=FUTURE_VAT_A),
                    _line(
                        accounts, item_id=SECOND_ITEM_ID, amount=FUTURE_GOODS_B, vat=FUTURE_VAT_B
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        service.post(future.id, user_id=ACTOR_ID)
        ids["future"] = future.id

        # Khoản KHÔNG ghi hạn, của khách hàng thứ hai.
        undated = service.create(
            _invoice(
                context,
                accounts,
                amount=UNDATED_AMOUNT,
                vat=UNDATED_VAT,
                posting_date=AUG_10,
                due_date=None,
                customer_id=OTHER_CUSTOMER_ID,
            ),
            user_id=ACTOR_ID,
        )
        service.post(undated.id, user_id=ACTOR_ID)
        ids["undated"] = undated.id

        # Khoản của khách không thuộc nhóm nào — sẽ được thu đủ ngay bên dưới.
        cleared = service.create(
            _invoice(
                context,
                accounts,
                amount=CLEARED_AMOUNT,
                vat=CLEARED_VAT,
                posting_date=AUG_10,
                due_date=SEP_20,
                customer_id=UNGROUPED_CUSTOMER_ID,
            ),
            user_id=ACTOR_ID,
        )
        service.post(cleared.id, user_id=ACTOR_ID)
        ids["cleared"] = cleared.id

        ids["early_return"] = _settle(
            session, service, context, accounts, ids["near"], EARLY_GOODS, EARLY_VAT, AUG_20
        )
        ids["mid_return"] = _settle(
            session, service, context, accounts, ids["mid"], PART_GOODS, PART_VAT, SEP_10
        )
        ids["cleared_return"] = _settle(
            session,
            service,
            context,
            accounts,
            ids["cleared"],
            CLEARED_AMOUNT,
            CLEARED_VAT,
            SEP_15,
            customer_id=UNGROUPED_CUSTOMER_ID,
        )
        ids["undated_return"] = _settle(
            session,
            service,
            context,
            accounts,
            ids["undated"],
            UNDATED_PART_GOODS,
            UNDATED_PART_VAT,
            SEP_15,
            customer_id=OTHER_CUSTOMER_ID,
        )
        fx = service.create(
            _invoice(
                context,
                accounts,
                amount=FX_GOODS_FC,
                vat=Decimal(0),
                posting_date=AUG_10,
                due_date=SEP_20,
                customer_id=FX_CUSTOMER_ID,
                currency_code="USD",
                exchange_rate=FX_RATE,
            ),
            user_id=ACTOR_ID,
        )
        service.post(fx.id, user_id=ACTOR_ID)
        ids["fx"] = fx.id
        ids["fx_return"] = _settle(
            session,
            service,
            context,
            accounts,
            ids["fx"],
            FX_SETTLE_FC,
            Decimal(0),
            SEP_15,
            customer_id=FX_CUSTOMER_ID,
            currency_code="USD",
            exchange_rate=FX_SETTLE_RATE,
        )

        # Chỉ CẤT, không ghi sổ.
        ids["draft_return"] = _settle(
            session,
            service,
            context,
            accounts,
            ids["near"],
            DRAFT_GOODS,
            DRAFT_VAT,
            SEP_15,
            post=False,
        )

    seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER_ID,
        amount_fc=OPENING_AMOUNT,
        invoice_no=OPENING_INVOICE_NO,
    )
    payable_id = seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        detail_kind=OpeningDetailKind.PAYABLE,
        account_code="331",
        partner_kind=PartnerKind.VENDOR,
        partner_id=VENDOR_ID,
        amount_fc=OPENING_PAYABLE,
        invoice_no=OPENING_PAYABLE_NO,
    )
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        cash = CashVoucherService(session)
        payment = cash.create(
            CashVoucherIn(
                kind=CashVoucherKind.PAYMENT,
                operation_code="tra-no-ncc",
                cash_account_id=accounts["111"],
                branch_id=context.branch_id,
                document_date=SEP_15,
                posting_date=SEP_15,
                currency_code="VND",
                exchange_rate=Decimal(1),
                partner_kind=PartnerKind.VENDOR,
                partner_id=VENDOR_ID,
                lines=(
                    CashVoucherLineIn(
                        debit_account_id=accounts["331"],
                        credit_account_id=accounts["111"],
                        amount_fc=PAYABLE_PAID,
                        partner_kind=PartnerKind.VENDOR,
                        partner_id=VENDOR_ID,
                    ),
                ),
                settlements=(
                    CashSettlementIn(
                        target_kind=SettlementTargetKind.OPENING_BALANCE,
                        target_id=payable_id,
                        amount_fc=PAYABLE_PAID,
                    ),
                ),
            ),
            user_id=ACTOR_ID,
        )
        cash.post(payment.id, user_id=ACTOR_ID)
        ids["payable_payment"] = payment.id
    return ids


def _settle(
    session: Session,
    service: SalesInvoiceService,
    context: PostingContext,
    accounts: dict[str, int],
    target_voucher: UUID,
    goods: Decimal,
    vat: Decimal,
    posting_date: date,
    *,
    customer_id: int = CUSTOMER_ID,
    post: bool = True,
    currency_code: str = "VND",
    exchange_rate: Decimal = Decimal(1),
) -> UUID:
    """Một chứng từ ghi giảm đối trừ khoản nợ của hóa đơn gốc.

    Đích đối trừ là dòng SỔ PHỤ, không phải chứng từ: `target_id` khớp
    `SettlementTargetSource.target_id` của kernel, và khóa chính của
    `ar_ap_ledger` là UUID của dòng nợ (quyết định 7A).
    """
    entry = session.execute(
        select(ArApLedgerEntry).where(ArApLedgerEntry.document_id == target_voucher)
    ).scalar_one()
    voucher = service.create(
        _invoice(
            context,
            accounts,
            amount=goods,
            vat=vat,
            posting_date=posting_date,
            due_date=None,
            customer_id=customer_id,
            kind=SalesInvoiceKind.RETURN,
            operation="tra-lai-hang-ban",
            currency_code=currency_code,
            exchange_rate=exchange_rate,
            settlements=(
                SalesSettlementIn(
                    target_kind=SettlementTargetKind.SALES_INVOICE,
                    target_id=entry.id,
                    amount_fc=goods + vat,
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    if post:
        service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


@pytest.fixture(scope="module")
def report_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Quyền báo cáo **và** quyền xem hóa đơn bán — bảy định nghĩa của lát này
    khai `required_permission_module = "sales"` (bản vá H-1b của 6E-1)."""
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
            "bao_cao_cong_no_phai_thu",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    def run(code: str, **params: object) -> PreviewResult:
        body = {
            "params": {
                "from_date": AUG_01.isoformat(),
                "to_date": SEP_30.isoformat(),
                **params,
            }
        }
        response = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return PreviewResult(response.json())

    return run


class TestTheDueStateSplitsTheDebtWholly:
    """`:due_state` phải chia trọn vẹn: tổng hai tờ bằng tổng nợ còn treo.

    Định nghĩa hẹp hơn cho vế "trước hạn" — đúng những khoản CÓ hạn nằm sau mốc
    chốt — làm khoản **không ghi hạn** rơi khỏi cả hai tờ, im lặng. Đó không phải
    một khả năng lý thuyết: `NULL` trượt khỏi cả `<` lẫn `>=`, nên bản viết vội
    nào cũng để nó rơi, và trên tờ giấy không có gì chỉ ra phần thiếu.
    """

    def test_the_two_analyses_add_up_to_the_whole_open_debt(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        overdue = preview("phan-tich-cong-no-phai-thu-qua-han", partner_id=CUSTOMER_ID)
        not_overdue = preview("phan-tich-cong-no-phai-thu-truoc-han", partner_id=CUSTOMER_ID)
        assert overdue.sum_of("remaining") == OVERDUE_TOTAL
        assert not_overdue.sum_of("remaining") == NOT_OVERDUE_TOTAL
        assert overdue.sum_of("remaining") + not_overdue.sum_of("remaining") == CUSTOMER_OPEN_TOTAL

    def test_a_debt_without_a_due_date_lands_on_the_not_overdue_sheet(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Khoản không ghi hạn thuộc vế "chưa quá hạn", và tờ giấy nói ra điều đó.

        Nó KHÔNG được xếp vào "quá hạn" — không ai đòi được một khoản chưa hẹn
        ngày — và cũng không được rơi khỏi cả hai tờ. Cột nhóm tuổi nợ giữ nó
        tách khỏi nhóm "chưa đến hạn" thật, nên người đọc không bị gộp nhầm hai
        tình trạng khác nhau.
        """
        overdue = preview("phan-tich-cong-no-phai-thu-qua-han", partner_id=OTHER_CUSTOMER_ID)
        not_overdue = preview("phan-tich-cong-no-phai-thu-truoc-han", partner_id=OTHER_CUSTOMER_ID)
        assert overdue.rows == []
        assert not_overdue.sum_of("remaining") == UNDATED_REMAINING
        assert [row["bucket"] for row in not_overdue.rows] == ["khong-han"]

    def test_the_overdue_sheet_carries_only_overdue_buckets(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Nhãn "quá hạn" phải đúng với từng dòng, không chỉ đúng với tổng.

        Tờ này gộp theo NHÓM TUỔI NỢ trước rồi mới tới khách hàng — đảo đúng hai
        bậc của "chi tiết theo tuổi nợ", và đó là lý do nó là một tờ riêng chứ
        không phải cùng một tờ đọc khác đi.
        """
        overdue = preview("phan-tich-cong-no-phai-thu-qua-han", partner_id=CUSTOMER_ID)
        # Nhóm tuổi nợ nằm trên TIÊU ĐỀ nhóm, không lặp lại ở từng dòng — đó là
        # hình dạng của một tờ gộp theo nhóm tuổi nợ. Vế dòng canh bằng số ngày.
        assert overdue.rows
        assert all(int(row["days_overdue"]) > 0 for row in overdue.rows)
        # Thứ tự ĐỌC của nhóm tuổi nợ: gần hạn nhất trước, không theo chữ cái —
        # thứ tự chữ cái xếp 'tren-90' xuống cuối một cách tình cờ nhưng xếp
        # 'chua-den-han' vào GIỮA các nhóm quá hạn, nên phép sắp phải đi cột số.
        reading_order = ("1-30", "31-60", "61-90", "tren-90")
        buckets = [head for head in overdue.headings if head in reading_order]
        assert buckets == ["1-30", "31-60", "tren-90"]
        assert len(set(buckets)) >= 2, "một nhóm thì không so được thứ tự"
        assert [reading_order.index(bucket) for bucket in buckets] == sorted(
            reading_order.index(bucket) for bucket in buckets
        )


class TestOpenOnlyIsPinnedOnTheRightSheets:
    def test_a_fully_settled_invoice_leaves_the_three_open_only_sheets(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        for code in OPEN_ONLY_REPORTS:
            report = preview(code, partner_id=UNGROUPED_CUSTOMER_ID)
            assert report.rows == [], code

    def test_a_fully_settled_invoice_stays_on_the_by_invoice_sheet(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """ "Chi tiết công nợ theo hóa đơn" nêu cả ba con số (giá trị / đã thu /
        còn lại), nên một hóa đơn đã thu đủ vẫn là một dòng có nghĩa trong kỳ."""
        report = preview("chi-tiet-cong-no-phai-thu-theo-hoa-don", partner_id=UNGROUPED_CUSTOMER_ID)
        gross = CLEARED_AMOUNT + CLEARED_VAT
        rows = [row for row in report.rows if money(row, "amount") == gross]
        assert len(rows) == 1
        assert money(rows[0], "settled") == gross
        assert money(rows[0], "remaining") == 0


class TestTheCustomerGroupDimension:
    def test_the_group_column_follows_the_immediate_parent(
        self,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        books: dict[str, UUID],
    ) -> None:
        """Nhóm của một khách hàng là NÚT CHA TRỰC TIẾP của nó trên cây đối tác.

        Kiểm trên chính cột của dataset chứ không qua tiêu đề nhóm của báo cáo:
        tiêu đề chỉ nói nhóm nào có mặt, còn thứ sai được là khách hàng nào rơi
        vào nhóm nào — và một phép nối sai cha vẫn sinh ra đúng số nhóm.
        """
        rows = _dataset_rows(session_factory, dataset_alpha, context)
        groups = {row["partner_code"]: row["partner_group_name"] for row in rows}
        # Cha trực tiếp là "Đại lý", nút gốc là "Miền Bắc" — báo cáo phải nói
        # "Đại lý". Lấy nút gốc thì mọi đại lý và mọi cửa hàng dưới "Miền Bắc"
        # gộp thành một dòng mang tên vùng, tức một báo cáo "theo nhóm khách
        # hàng" không còn nói về nhóm nào cả.
        assert groups[CUSTOMER_CODE] == f"Nhóm {GROUP_DEALER_CODE}"
        assert groups[CUSTOMER_CODE] != f"Nhóm {GROUP_NORTH_CODE}"
        assert groups[OTHER_CUSTOMER_CODE] == f"Nhóm {GROUP_SOUTH_CODE}"
        # Khách đứng ở gốc: nhóm CÓ TÊN, không phải ô trống.
        assert groups[UNGROUPED_CUSTOMER_CODE] == UNGROUPED_LABEL

    def test_the_group_report_totals_each_group_on_its_own(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Lọc về một khách hàng thì tờ theo nhóm chỉ còn nhóm của khách ấy.

        Bài này đi qua báo cáo (không qua dataset) vì thứ nó canh là KHÓA GỘP:
        một layout gộp nhầm cột vẫn ra đúng cột dữ liệu.
        """
        report = preview("tong-hop-cong-no-phai-thu-theo-nhom-khach-hang", partner_id=CUSTOMER_ID)
        assert f"Nhóm {GROUP_DEALER_CODE}" in report.headings
        assert f"Nhóm {GROUP_NORTH_CODE}" not in report.headings
        assert f"Nhóm {GROUP_SOUTH_CODE}" not in report.headings
        assert report.total("remaining") == CUSTOMER_OPEN_TOTAL

    def test_an_ungrouped_customer_gets_a_named_heading(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        report = preview(
            "tong-hop-cong-no-phai-thu-theo-nhom-khach-hang",
            partner_id=UNGROUPED_CUSTOMER_ID,
        )
        assert UNGROUPED_LABEL in report.headings


class TestTheSheetsAgreeWithTheSubledger:
    def test_the_summary_matches_the_receivable_subledger(
        self,
        preview: Preview,
        session_factory: sessionmaker[Session],
        dataset_alpha: DatasetRef,
        context: PostingContext,
        books: dict[str, UUID],
    ) -> None:
        """Tổng "còn lại" của tờ tổng hợp bằng đúng số sổ phụ đang treo.

        So với SỔ, không với một con số chép tay: viết sẵn kỳ vọng là dựng lại
        luật đối trừ trong bài kiểm, và khi hai bên lệch thì bài kiểm sẽ nói báo
        cáo sai trong khi chỗ sai là kỳ vọng của chính nó.
        """
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            entries = session.scalars(
                select(ArApLedgerEntry).where(
                    ArApLedgerEntry.partner_id == CUSTOMER_ID,
                    ArApLedgerEntry.ledger == Ledger.FINANCIAL,
                )
            ).all()
            booked = sum((entry.amount - entry.settled for entry in entries), Decimal(0))
        report = preview("tong-hop-cong-no-phai-thu", partner_id=CUSTOMER_ID)
        # Sổ phụ không mang nợ đầu kỳ — nó ở `opening_balance_invoices`.
        assert report.total("remaining") - OPENING_AMOUNT == booked

    def test_no_sheet_prints_a_negative_remaining(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Không khoản nợ nào được in ra số còn lại ÂM.

        Một bảng công nợ có dòng âm là một bảng không ai cộng lại được, nên phép
        kiểm này rẻ mà chặn cả họ lỗi cơ sở tiền — nó đã bắt được một lỗi thật ở
        chiều phải trả (7G-1 vòng review thứ hai).
        """
        for code in AR_REPORTS:
            report = preview(code, partner_id=CUSTOMER_ID)
            assert all(money(row, "remaining") >= 0 for row in report.rows), code

    def test_no_payable_leaks_onto_a_receivable_sheet(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Chiều liệt kê theo `target_kind`, và mỗi loại đích phải có mặt ở CẢ
        HAI chỗ của dataset: `CASE` sinh cột chiều và `WHERE` lọc loại đích.

        Khách hàng của tệp này không có khoản phải trả nào, nên mọi dòng hiện ra
        phải là khoản của chính họ — một nhánh `ELSE` nới tay sẽ kéo nợ phải trả
        của tệp khác sang đây.
        """
        report = preview("chi-tiet-cong-no-phai-thu", partner_id=CUSTOMER_ID)
        assert report.rows
        assert {row["partner_code"] for row in report.rows} == {CUSTOMER_CODE}


class TestTheItemSheetDoesNotAllocate:
    """SRS 06 §5.2 #4 — công nợ xem theo dòng hàng, KHÔNG chia phần còn nợ.

    Sổ đối trừ theo khoản, không theo dòng hóa đơn. Chia phần còn nợ cho các
    dòng theo tỷ lệ giá trị là dựng một phép chia mà không sổ nào ghi, và nó vẫn
    cộng ra đúng tổng nên không có gì đối chiếu ra.
    """

    def test_each_line_carries_its_own_value_and_the_invoice_debt_unchanged(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hóa đơn HAI dòng: mỗi dòng mang giá trị của chính nó, cột nợ mang số
        của CẢ hóa đơn — lặp lại, không chia đôi.

        Trên hóa đơn một dòng, "chia" và "không chia" cho ra cùng con số, nên bài
        kiểm này đứng trên hóa đơn hai dòng của bộ gieo.
        """
        report = preview(
            "chi-tiet-cong-no-phai-thu-theo-mat-hang",
            customer_id=CUSTOMER_ID,
            item_id=SECOND_ITEM_ID,
        )
        assert len(report.rows) == 1
        row = report.rows[0]
        assert money(row, "goods_amount") == FUTURE_GOODS_B
        # Phần còn nợ của hóa đơn, NGUYÊN VẸN — không phải 1/3 theo tỷ lệ dòng.
        assert money(row, "invoice_remaining") == FUTURE_REMAINING

    def test_the_lines_of_one_invoice_add_up_to_its_revenue_not_its_debt(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Cộng cột giá trị dòng ra DOANH THU của hóa đơn, không ra phần còn nợ.

        Hai con số ấy khác nhau (doanh thu chưa gồm thuế, phần còn nợ thì có),
        nên đây là phép so phân biệt được "cột giá trị dòng" với "một phép chia
        phần nợ đội lốt giá trị dòng".
        """
        report = preview("chi-tiet-cong-no-phai-thu-theo-mat-hang", customer_id=CUSTOMER_ID)
        # Nhận dạng hóa đơn bằng cột nợ của nó, KHÔNG bằng giá trị dòng: hai hóa
        # đơn khác của bộ gieo có giá trị trùng với hai dòng của hóa đơn này, và
        # một bộ lọc theo giá trị dòng gom cả bốn.
        future_lines = [
            row for row in report.rows if money(row, "invoice_remaining") == FUTURE_REMAINING
        ]
        assert len(future_lines) == 2
        assert (
            sum((money(row, "goods_amount") for row in future_lines), Decimal(0)) == FUTURE_AMOUNT
        )
        assert FUTURE_AMOUNT != FUTURE_REMAINING

    def test_a_settled_invoice_has_no_lines_here(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """`open_only` ghim trên tờ này: nó nói về hàng đứng sau tiền CÒN NỢ."""
        report = preview(
            "chi-tiet-cong-no-phai-thu-theo-mat-hang", customer_id=UNGROUPED_CUSTOMER_ID
        )
        assert report.rows == []

    def test_debts_without_invoice_lines_are_absent_and_the_total_is_smaller(
        self,
        preview: Preview,
        books: dict[str, UUID],
    ) -> None:
        """Nợ mang sang không có dòng hàng nào, nên tờ này NHỎ HƠN tờ tổng hợp.

        Đó là giới hạn có chủ đích, không phải bỏ sót — và nó phải được canh, vì
        một người đọc mặc nhiên tin hai tờ công nợ cộng ra cùng một số. Ai cần
        con số đối chiếu TK 131 thì đọc tờ tổng hợp.
        """
        by_item = preview("chi-tiet-cong-no-phai-thu-theo-mat-hang", customer_id=CUSTOMER_ID)
        invoice_debts = {money(row, "invoice_remaining") for row in by_item.rows}
        assert OPENING_AMOUNT not in invoice_debts
        assert sum(invoice_debts, Decimal(0)) == CUSTOMER_OPEN_TOTAL - OPENING_AMOUNT


CUSTOMER_SETTLED_IN_WINDOW = EARLY_SETTLED + PART_SETTLED


class TestTheSettlementHistorySheets:
    """SRS 06 §5.2 #10/#11 — từng lượt thu, không phải số dư.

    Dataset này cắt theo CẢ `:from_date` lẫn `:to_date` (phát sinh trong kỳ),
    khác hẳn dataset công nợ vốn chỉ có mốc chốt. Nó dùng chung với dataset công
    nợ đúng một thứ: khối nguồn năm bảng đối trừ.
    """

    def test_a_partial_receipt_shows_up_as_one_line_at_its_posting_date(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        report = preview("tong-hop-thanh-toan-cong-no-khach-hang", partner_id=CUSTOMER_ID)
        rows = [row for row in report.rows if money(row, "amount") == PART_SETTLED]
        assert len(rows) == 1
        assert rows[0]["settled_on"] == SEP_10.strftime("%d/%m/%Y")

    def test_the_sheet_total_matches_what_the_debt_sheet_says_was_settled(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Tổng của tờ thanh toán bằng đúng cột "đã thu" của tờ công nợ.

        Hai tờ đọc hai câu SQL khác nhau trên cùng một khối nguồn, nên đây là
        phép so bắt được lệch cơ sở tiền: cộng `amount` trần thay vì
        `amount - fx_diff` sẽ làm hai vế rời nhau trên hóa đơn ngoại tệ.

        Cửa sổ của bộ gieo chứa mọi lượt thu của khách hàng này, nên hai vế phải
        bằng nhau tuyệt đối chứ không chỉ "xấp xỉ".
        """
        settlements = preview("tong-hop-thanh-toan-cong-no-khach-hang", partner_id=CUSTOMER_ID)
        debts = preview("chi-tiet-cong-no-phai-thu-theo-hoa-don", partner_id=CUSTOMER_ID)
        assert settlements.rows
        assert settlements.total("amount") == debts.sum_of("settled")
        assert settlements.total("amount") == CUSTOMER_SETTLED_IN_WINDOW

    def test_a_settlement_on_an_unposted_voucher_is_absent(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Dòng đối trừ ghi xuống bảng lúc CẤT, phần nhích `settled` lúc GHI SỔ.

        Liệt kê mọi dòng trong bảng vì thế in ra những lượt thu chưa có đồng nào
        lên sổ. Bộ gieo có đúng một chứng từ chỉ-cất; nó không được có mặt.
        """
        report = preview("tong-hop-thanh-toan-cong-no-khach-hang", partner_id=CUSTOMER_ID)
        assert DRAFT_SETTLEMENT not in {money(row, "amount") for row in report.rows}

    def test_the_period_window_cuts_by_the_settlement_date_not_the_invoice_date(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Cửa sổ kỳ cắt theo ngày THU, và cắt ở CẢ HAI đầu.

        Khách hàng này có hai lượt thu: 20/08 và 10/09. Đọc kỳ tháng 8 phải thấy
        đúng lượt đầu, đọc kỳ từ 10/09 phải thấy đúng lượt sau. Một bản thiếu cận
        DƯỚI xanh ở vế tháng 8 (mọi thứ vẫn ≤ 20/08) và chỉ đỏ ở vế sau — nên bài
        kiểm phải có cả hai vế, và cả hai phải khác nhau.
        """
        august = preview(
            "tong-hop-thanh-toan-cong-no-khach-hang",
            partner_id=CUSTOMER_ID,
            from_date=AUG_01.isoformat(),
            to_date=AUG_20.isoformat(),
        )
        assert [money(row, "amount") for row in august.rows] == [EARLY_SETTLED]
        september = preview(
            "tong-hop-thanh-toan-cong-no-khach-hang",
            partner_id=CUSTOMER_ID,
            from_date=SEP_10.isoformat(),
            to_date=SEP_30.isoformat(),
        )
        assert [money(row, "amount") for row in september.rows] == [PART_SETTLED]

    def test_the_payment_days_sheet_counts_from_the_document_and_the_due_date(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hai cột ngày đếm từ hai mốc khác nhau, và cả hai đều kiểm được.

        Khoản '31-60': chứng từ 05/08, hạn 20/08, thu 10/09 ⇒ 36 ngày kể từ
        chứng từ và trễ 21 ngày so với hạn. Một bản nhầm hai mốc cho ra cùng một
        con số ở cả hai cột, nên bài kiểm đòi chúng KHÁC nhau.
        """
        report = preview("bao-cao-ngay-thanh-toan-theo-khach-hang", partner_id=CUSTOMER_ID)
        rows = [row for row in report.rows if money(row, "amount") == PART_SETTLED]
        assert len(rows) == 1
        assert int(rows[0]["days_to_pay"]) == (SEP_10 - AUG_05).days
        assert int(rows[0]["days_late"]) == (SEP_10 - AUG_20).days
        assert rows[0]["days_to_pay"] != rows[0]["days_late"]

    def test_a_debt_without_a_due_date_leaves_days_late_empty(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """ "Trả đúng hạn" và "không có hạn để mà trễ" là hai câu khác nhau.

        Điền 0 vào cột trễ hạn của một khoản không ghi hạn là nói dối theo hướng
        dễ chịu — cùng lý do bảng tuổi nợ tách nhóm `khong-han`.
        """
        report = preview(
            "bao-cao-ngay-thanh-toan-theo-khach-hang", partner_id=UNDATED_CUSTOMER_PAYER
        )
        rows = [row for row in report.rows if money(row, "amount") == UNDATED_SETTLED]
        assert len(rows) == 1
        assert rows[0]["days_late"].strip() == ""
        assert int(rows[0]["days_to_pay"]) >= 0


class TestTheOpeningBalanceDirection:
    """Chiều của nợ MANG SANG nằm ở `detail_kind`, không ở `target_kind`.

    Số dư đầu kỳ mang `target_kind = 2` ở **cả hai** chiều — phải thu và phải
    trả — nên một bản đọc chiều từ loại đích sẽ dán nhãn "phải thu" cho mọi khoản
    nợ mang sang. Hậu quả im lặng: tờ "tổng hợp thanh toán công nợ khách hàng"
    liệt kê cả những lượt ta TRẢ TIỀN cho nhà cung cấp, và tổng của nó phình lên
    đúng phần ấy trong khi mọi dòng vẫn trông hợp lệ.
    """

    def test_paying_a_carried_payable_stays_off_the_receivable_sheet(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        report = preview("tong-hop-thanh-toan-cong-no-khach-hang")
        assert PAYABLE_PAID not in {money(row, "amount") for row in report.rows}
        assert OPENING_PAYABLE_NO not in report.all_text()


class TestTheForeignCurrencyBasis:
    """Cột tiền của lượt thu đọc theo tỷ giá GHI NHẬN, không theo tỷ giá thu.

    `amount` của bảng đối trừ là VND theo tỷ giá **thanh toán**; phần VND thật sự
    giải phóng trên sổ là `amount - fx_diff`, và đó đúng là con số
    `apply_settlement_rows` cộng vào khoản đích — phần chênh đi vào 515/635
    (FR-SYS-066). Vòng review 7G-1 bắt được đúng lỗi này ở chiều phải trả, và nó
    hỏng theo hướng tệ nhất: `settled` vượt `amount` thì phần còn lại ra số ÂM.

    Chỉ hóa đơn ngoại tệ trả TỪNG PHẦN bắt được: trả trọn vẹn thì dòng rơi khỏi
    tờ công nợ, còn hóa đơn VND thì `fx_diff` luôn bằng 0.
    """

    def test_the_settlement_sheet_uses_the_recognition_rate(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        recognised = FX_SETTLE_FC * FX_RATE
        paid_at_settlement_rate = FX_SETTLE_FC * FX_SETTLE_RATE
        # Nếu hai con số này bằng nhau thì bài kiểm không kiểm gì.
        assert recognised != paid_at_settlement_rate

        report = preview("tong-hop-thanh-toan-cong-no-khach-hang", partner_id=FX_CUSTOMER_ID)
        assert [money(row, "amount") for row in report.rows] == [recognised]

    def test_the_settlement_sheet_and_the_debt_sheet_agree_on_the_fx_invoice(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hai tờ đọc hai câu SQL khác nhau — chúng phải nói cùng một con số.

        Đây là phép so bắt được lệch cơ sở tiền giữa hai dataset: một bên cộng
        `amount` trần, một bên cộng `amount - fx_diff`, và không con số nào trên
        hai tờ giấy chỉ ra chỗ lệch.
        """
        settlements = preview("tong-hop-thanh-toan-cong-no-khach-hang", partner_id=FX_CUSTOMER_ID)
        debts = preview("chi-tiet-cong-no-phai-thu-theo-hoa-don", partner_id=FX_CUSTOMER_ID)
        assert settlements.total("amount") == debts.sum_of("settled")
        # Và phần còn lại không bao giờ âm — hướng hỏng của lỗi cơ sở tiền.
        assert all(money(row, "remaining") >= 0 for row in debts.rows)


class TestTheMetadataContract:
    def test_all_seven_sheets_read_the_one_debt_dataset(self) -> None:
        """Bảy tờ, một dataset — cùng dataset mà chiều phải trả đọc.

        Một dataset thứ hai cho chiều phải thu sẽ là bản chép của khối cộng số đã
        đối trừ năm bảng, đúng thứ lát này vừa rút đi.
        """
        by_code = {d.code: d for d in load_builtin_reports().manifest.definitions}
        assert {by_code[code].dataset_code for code in AR_REPORTS} == {"ar_ap_open_items"}
        assert {by_code[code].required_permission_module for code in AR_REPORTS} == {"sales"}

    def test_only_the_analysis_and_aging_sheets_pin_open_only(self) -> None:
        """`open_only` ghim ở ba tờ, KHÔNG ghim ở bốn tờ còn lại.

        Ghim nhầm nó lên tờ "chi tiết theo hóa đơn" là giấu mất mọi hóa đơn đã
        thu đủ khỏi một tờ mà mục đích là đối chiếu cả kỳ; quên ghim nó trên tờ
        "phân tích quá hạn" là in cả khoản đã thu xong vào một danh sách đi đòi.
        """
        by_code = {d.code: d for d in load_builtin_reports().manifest.definitions}
        pinned = {code for code in AR_REPORTS if by_code[code].fixed_params.get("open_only")}
        assert pinned == set(OPEN_ONLY_REPORTS)
        for code in AR_REPORTS:
            assert by_code[code].fixed_params["direction"] == "thu", code


def _dataset_rows(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> list[dict[str, str]]:
    """Dòng thô của dataset ở chiều phải thu, thu về ba khách hàng của tệp này."""
    # Qua LOADER, không đọc thẳng tệp: từ lát 7G-2b tệp dataset nhúng mảnh dùng
    # chung bằng `-- #include:`, và chỉ loader bung nó. Đọc thẳng tệp cho ra một
    # câu SQL còn nguyên dòng chỉ thị — PostgreSQL đổ ở đúng chỗ CTE rỗng.
    sql = load_builtin_reports().sql_by_dataset["ar_ap_open_items"]
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        rows = session.execute(
            text(sql),
            {
                "from_date": AUG_01,
                "to_date": SEP_30,
                "ledger": int(Ledger.FINANCIAL),
                "branch_ids": [context.branch_id],
                "direction": "thu",
                "partner_id": None,
                "partner_kind": None,
                "open_only": None,
                "due_state": None,
            },
        ).mappings()
        ours = (CUSTOMER_CODE, OTHER_CUSTOMER_CODE, UNGROUPED_CUSTOMER_CODE)
        return [dict(row) for row in rows if row["partner_code"] in ours]
