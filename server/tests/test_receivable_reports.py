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
from importlib import resources
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from cash_book_support import seed_open_invoice
from catalog_api_support import UserFactory, actor, ensure_role
from conftest import api_test_client
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.reports.loader import load_builtin_reports
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.protocols import SettlementTargetKind
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.receivables.models import ArApLedgerEntry
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn, SalesSettlementIn
from ket.modules.sales.service import SalesInvoiceService
from ket.posting.engine.models import Ledger
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_item, ensure_unit
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
UNIT_ID = 9631
ITEM_ID = 9641

GROUP_NORTH_CODE = "NHOM-7G2B-BAC"
GROUP_DEALER_CODE = "NHOM-7G2B-DAI-LY"
GROUP_SOUTH_CODE = "NHOM-7G2B-NAM"
CUSTOMER_CODE = "KH-7G2B-01"
OTHER_CUSTOMER_CODE = "KH-7G2B-02"
UNGROUPED_CUSTOMER_CODE = "KH-7G2B-03"
UNIT_CODE = "Cai-7G2B"
ITEM_CODE = "VT-7G2B"

UNGROUPED_LABEL = "Chưa phân nhóm"
OPENING_INVOICE_NO = "HD-DAU-KY-7G2B"

# Bốn khoản của khách hàng thứ nhất, chọn hạn để rơi vào bốn nhóm tuổi nợ KHÁC
# NHAU tại mốc chốt 30/09 — một bộ dữ liệu một nhóm không phân biệt được "lọc
# theo tình trạng hạn" với "không lọc gì".
NEAR_AMOUNT = Decimal(1_000_000)  # hạn 20/09 → quá hạn 10 ngày → nhóm '1-30'
NEAR_VAT = Decimal(100_000)
MID_AMOUNT = Decimal(2_000_000)  # hạn 20/08 → quá hạn 41 ngày → nhóm '31-60'
MID_VAT = Decimal(200_000)
FUTURE_AMOUNT = Decimal(3_000_000)  # hạn 15/10 → chưa đến hạn
FUTURE_VAT = Decimal(300_000)
OPENING_AMOUNT = Decimal(1_000_000)  # nợ mang sang, hạn 20/02 → nhóm 'tren-90'

# Ghi giảm một phần khoản '31-60', ghi sổ 10/09 — TRƯỚC mốc chốt, nên nó phải trừ.
PART_GOODS = Decimal(200_000)
PART_VAT = Decimal(20_000)
PART_SETTLED = PART_GOODS + PART_VAT

# Khoản của khách hàng thứ hai: KHÔNG ghi hạn. Nó là dòng mà phép chia quá hạn /
# trước hạn dễ để rơi nhất, vì `NULL` trượt khỏi cả hai vế của một phép so ngày.
UNDATED_AMOUNT = Decimal(400_000)
UNDATED_VAT = Decimal(40_000)

# Khoản của khách hàng không thuộc nhóm nào, THU ĐỦ trước mốc chốt: nó phải biến
# mất khỏi ba tờ ghim `open_only` và ở lại trên bốn tờ còn lại.
CLEARED_AMOUNT = Decimal(500_000)
CLEARED_VAT = Decimal(50_000)

NEAR_REMAINING = NEAR_AMOUNT + NEAR_VAT
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
        ensure_unit(session, unit_id=UNIT_ID, code=UNIT_CODE)
        ensure_item(session, item_id=ITEM_ID, code=ITEM_CODE, unit_id=UNIT_ID)
    return codes


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
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="công nợ phải thu 7G-2b",
        lines=(
            SalesInvoiceLineIn(
                description="Hàng 7G-2b",
                item_id=ITEM_ID,
                unit_id=UNIT_ID,
                quantity=Decimal(1),
                unit_price_fc=amount,
                amount_fc=amount,
                vat_rate=Decimal(10) if vat else Decimal(0),
                vat_amount_fc=vat,
                account_id=accounts["5111"],
                vat_account_id=accounts["33311"] if vat else None,
            ),
        ),
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
            ("future", FUTURE_AMOUNT, FUTURE_VAT, OCT_15),
        ):
            invoice = service.create(
                _invoice(
                    context,
                    accounts,
                    amount=amount,
                    vat=vat,
                    posting_date=AUG_05 if due is not OCT_15 else AUG_10,
                    due_date=due,
                ),
                user_id=ACTOR_ID,
            )
            service.post(invoice.id, user_id=ACTOR_ID)
            ids[key] = invoice.id

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

    seed_open_invoice(
        session_factory,
        dataset_alpha,
        context,
        partner_id=CUSTOMER_ID,
        amount_fc=OPENING_AMOUNT,
        invoice_no=OPENING_INVOICE_NO,
    )
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
        assert not_overdue.sum_of("remaining") == UNDATED_AMOUNT + UNDATED_VAT
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
    sql = (
        resources.files("ket.kernel.config.reports.data.datasets")
        .joinpath("ar_ap_open_items.sql")
        .read_text("utf-8")
    )
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
