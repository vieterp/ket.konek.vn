"""Bốn báo cáo hóa đơn điện tử (`docs/srs/07` §5 #1/#3/#4, FR-INV-002) — lát 7G-3.

Cả bốn chạy qua chính đường preview của report engine, đăng ký hoàn toàn bằng
metadata. Bốn chỗ có thể ra **số sai mà vẫn ra số**, và cả bốn có bài riêng:

* **Hóa đơn CHƯA PHÁT HÀNH không có ngày.** `invoice_date` chỉ tới lúc cấp số,
  mà SRS 07 §5 #3 tồn tại chính để liệt kê nhóm ấy. Một cửa sổ kỳ cắt thẳng theo
  `invoice_date` loại sạch đúng nhóm tờ giấy sinh ra để cho thấy — cùng hình dạng
  lỗi NULL mà 7G-1 trả giá ở khoản ứng trước đầu kỳ.
* **"Đã phát hành" là một NGƯỠNG, không phải một trạng thái.** Số tiêu từ
  `DANG_PHAT_HANH` và không nhả ra: `PHAT_HANH_LOI` giữ số, `DA_HUY` cũng giữ
  (BR-INV-04). Đếm từ `DA_PHAT_HANH` báo "còn trống" những số đã tiêu thật.
* **Tiền đọc từ SỔ, không nhân lại tỷ giá** — kỷ luật 7G-1/7G-2a. Và đọc vế
  phải thu (tiền hàng + thuế) chứ không cộng hai vế doanh thu và thuế lại.
* **Tờ đối chiếu tồn tại vì các dòng LỆCH**, nên nó phải in được dòng lệch.

Khối id **95xx** là của tệp này (96xx của 7G-2b, 98xx của 7G-1, 99xx của 7G-2a);
mã người dùng đặt cũng riêng, vì `units_of_measure.code` duy nhất toàn bảng.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from catalog_api_support import UserFactory, actor_with_totp, ensure_role
from conftest import api_test_client
from einvoice_support import ensure_active_registration, ensure_invoice_form
from ket.api.dependencies import BRANCH_HEADER
from ket.kernel.config.reports.loader import load_builtin_reports
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.keystore import SecretBox
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.einvoice.error_flow import ErrorFlowService
from ket.modules.einvoice.models import EInvoiceStatus, ErrorKind, ErrorNoticeKind
from ket.modules.einvoice.service import EInvoiceService
from ket.modules.sales.models import SalesInvoiceKind
from ket.modules.sales.schemas import SalesInvoiceIn, SalesInvoiceLineIn
from ket.modules.sales.service import SalesInvoiceService
from ket.settings import Settings
from posting_support import PostingContext, posting_scope, seed_posting_context
from report_preview_support import Preview, PreviewResult, money
from sales_support import ensure_customer, seed_sales_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
REPORT_ROLE = "xem_bao_cao_hoa_don_dien_tu"

# Cửa sổ riêng của tệp này (tháng 10–11/2026): 7G-1 tháng 4–5, 7G-2a tháng 6–7,
# 7G-2b tháng 8–9. `dataset_alpha` dùng chung, nên tách bằng THỜI GIAN.
OCT_01 = date(2026, 10, 1)
OCT_05 = date(2026, 10, 5)
OCT_10 = date(2026, 10, 10)
OCT_20 = date(2026, 10, 20)
OCT_31 = date(2026, 10, 31)
NOV_30 = date(2026, 11, 30)

FORM_ID = 9501
SECOND_FORM_ID = 9502
PROVIDER_FORM_ID = 9503
CUSTOMER_ID = 9511
SERIAL = "C26TGA"
SECOND_SERIAL = "C26TGB"
PROVIDER_SERIAL = "C26TGC"
CUSTOMER_CODE = "KH-7G3-01"

GOODS_AMOUNT = Decimal(1_000_000)
GOODS_VAT = Decimal(100_000)
INVOICE_TOTAL = GOODS_AMOUNT + GOODS_VAT
"""Tổng tiền thanh toán của tờ hóa đơn — phần ghi Nợ TK phải thu."""

RANGE_FROM = 1
RANGE_TO = 100
RANGE_QUANTITY = RANGE_TO - RANGE_FROM + 1

# Hồ sơ THỨ HAI trên cùng ký hiệu, dải kế tiếp. Một ký hiệu được thông báo phát
# hành nhiều lần (xem `registration_service.effective_for`; không có `UNIQUE` trên
# cặp (ký hiệu, chi nhánh)), và bộ gieo một-hồ-sơ không phân biệt được "đếm số của
# chính hồ sơ này" với "đếm mọi số của cặp ấy" — vòng review chứng minh bản đầu
# in `đã dùng 3 / còn 97` trên CẢ HAI dòng.
SECOND_RANGE_FROM = 101
SECOND_RANGE_TO = 200
SECOND_RANGE_QUANTITY = SECOND_RANGE_TO - SECOND_RANGE_FROM + 1


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
        ensure_customer(session, partner_id=CUSTOMER_ID, code=CUSTOMER_CODE)
        ensure_invoice_form(session, form_id=FORM_ID, serial=SERIAL)
        ensure_invoice_form(session, form_id=SECOND_FORM_ID, serial=SECOND_SERIAL)
        # Ký hiệu khai NHÀ CUNG CẤP: nhà cung cấp cấp số, nên tờ ở
        # `DANG_PHAT_HANH` **chưa có số** — số về ở lượt xác nhận (7E-2). Đây là
        # ca duy nhất phân biệt "đã tiêu một số" với "đã qua một ngưỡng trạng
        # thái", và bộ gieo không có nó thì hai cách viết ngưỡng cho cùng kết quả.
        ensure_invoice_form(
            session,
            form_id=PROVIDER_FORM_ID,
            serial=PROVIDER_SERIAL,
            provider_code="easyinvoice",
        )
        # Dải số khai TƯỜNG MINH ở ký hiệu thứ nhất, BỎ TRỐNG ở ký hiệu thứ hai:
        # "không khai dải" là trạng thái hợp lệ của hồ sơ đăng ký HĐĐT theo NĐ123,
        # và cột "còn lại" phải trả rỗng chứ không trả 0 — một bộ gieo chỉ có hồ sơ
        # khai dải không phân biệt được hai câu ấy.
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=FORM_ID,
            start_date=OCT_01,
            range_from=RANGE_FROM,
            range_to=RANGE_TO,
        )
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=FORM_ID,
            start_date=OCT_05,
            range_from=SECOND_RANGE_FROM,
            range_to=SECOND_RANGE_TO,
        )
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=SECOND_FORM_ID,
            start_date=OCT_01,
        )
        ensure_active_registration(
            session,
            branch_id=context.branch_id,
            invoice_form_id=PROVIDER_FORM_ID,
            start_date=OCT_01,
        )
    return codes


def _sales_voucher(
    session: Session,
    context: PostingContext,
    accounts: dict[str, int],
    *,
    posting_date: date,
    amount: Decimal = GOODS_AMOUNT,
    vat: Decimal = GOODS_VAT,
    post: bool = True,
) -> UUID:
    service = SalesInvoiceService(session)
    voucher = service.create(
        SalesInvoiceIn(
            kind=SalesInvoiceKind.GOODS,
            operation_code="ban-hang-hoa",
            customer_id=CUSTOMER_ID,
            receivable_account_id=accounts["131"],
            branch_id=context.branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code="VND",
            exchange_rate=Decimal(1),
            description="bán hàng cho báo cáo hóa đơn 7G-3",
            lines=(
                SalesInvoiceLineIn(
                    description="Hàng 7G-3",
                    quantity=Decimal(1),
                    unit_price_fc=amount,
                    amount_fc=amount,
                    vat_rate=Decimal(10) if vat else Decimal(0),
                    vat_amount_fc=vat,
                    account_id=accounts["5111"],
                    vat_account_id=accounts["33311"] if vat else None,
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    if post:
        service.post(voucher.id, user_id=ACTOR_ID)
    return voucher.id


@pytest.fixture(scope="module")
def books(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> dict[str, UUID]:
    """Năm tờ hóa đơn phủ năm trạng thái, cộng một tờ của ký hiệu thứ hai.

    Một bộ dữ liệu cho cả bốn báo cáo: mỗi bài đọc phần của nó và khẳng định trên
    những con số biết trước ở đầu tệp.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    ids: dict[str, UUID] = {}
    with unit_of_work(session_factory, scope) as session:
        service = EInvoiceService(session)

        # (1) Tờ CHƯA PHÁT HÀNH — chưa có số, chưa có ngày. Chứng từ gốc của nó
        # cũng chỉ CẤT: một tờ nháp của một chứng từ chưa ghi sổ là trạng thái
        # thường, và nó phải hiện ở #3 kèm ô tiền rỗng.
        draft_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_05, post=False)
        draft = service.create_draft(source_voucher_id=draft_voucher, invoice_form_id=FORM_ID)
        ids["draft"] = draft.id

        # (2) Tờ ĐÃ PHÁT HÀNH, có mã cơ quan thuế.
        issued_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_10)
        issued = service.create_draft(source_voucher_id=issued_voucher, invoice_form_id=FORM_ID)
        service.issue(issued.id, invoice_date=OCT_10)
        service.confirm(issued.id, tax_authority_code="M1-7G3-0001", lookup_code="TRA7G30001")
        ids["issued"] = issued.id

        # (3) Tờ PHÁT HÀNH LỖI — **giữ số đã cấp**, và đó là cả lý do nó ở đây.
        rejected_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_10)
        rejected = service.create_draft(source_voucher_id=rejected_voucher, invoice_form_id=FORM_ID)
        service.issue(rejected.id, invoice_date=OCT_10)
        service.reject(rejected.id, message="cơ quan thuế từ chối — bài kiểm 7G-3")
        ids["rejected"] = rejected.id

        # (4) Tờ ĐÃ HỦY — cũng giữ số (BR-INV-04 cấm tái sử dụng).
        cancelled_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_20)
        cancelled = service.create_draft(
            source_voucher_id=cancelled_voucher, invoice_form_id=FORM_ID
        )
        service.issue(cancelled.id, invoice_date=OCT_20)
        service.confirm(cancelled.id, tax_authority_code="M1-7G3-0002")
        # BR-EIV-04: hủy hóa đơn đòi ĐỦ HAI văn bản đã nộp — thông báo gửi cơ quan
        # thuế và biên bản thỏa thuận với người mua. 7D chặn cứng chuyện ấy ở state
        # machine, nên bộ gieo phải đi đúng đường người dùng đi.
        for kind, notice_no in (
            (ErrorNoticeKind.THONG_BAO_HUY, "TBH-7G3-01"),
            (ErrorNoticeKind.BIEN_BAN_HUY, "BBH-7G3-01"),
        ):
            notice = service.add_notice(
                cancelled.id,
                kind=kind,
                notice_no=notice_no,
                notice_date=OCT_20,
                reason="hủy theo thỏa thuận — bài kiểm 7G-3",
            )
            service.submit_notice(notice.id)
        service.cancel(cancelled.id)
        ids["cancelled"] = cancelled.id

        # (5) Tờ của ký hiệu THỨ HAI — hồ sơ không khai dải số.
        other_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_20)
        other = service.create_draft(
            source_voucher_id=other_voucher, invoice_form_id=SECOND_FORM_ID
        )
        service.issue(other.id, invoice_date=OCT_20)
        service.confirm(other.id, tax_authority_code="M2-7G3-0001")
        ids["other_form"] = other.id

        # (6) Tờ bị THAY THẾ + tờ thay thế nó. `_supersede` dùng lại **chính**
        # `source_voucher_id`, nên hai tờ đọc cùng một bộ `gl_postings` — không có
        # ca này thì phép cộng đôi của tờ đối chiếu không ai thấy.
        replaced_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_20)
        replaced = service.create_draft(source_voucher_id=replaced_voucher, invoice_form_id=FORM_ID)
        service.issue(replaced.id, invoice_date=OCT_20)
        service.confirm(replaced.id, tax_authority_code="M1-7G3-0003")
        outcome = ErrorFlowService(session).apply(
            replaced.id,
            error_kind=ErrorKind.SAI_THONG_TIN,
            buyer_declared=False,
            notice_no="TBSS-7G3-01",
            notice_date=OCT_20,
            reason="sai tên người mua — bài kiểm 7G-3",
        )
        assert outcome.replacement is not None
        # Tờ thay thế phải được PHÁT HÀNH: `_supersede` dựng một bản nháp, và một
        # bản nháp không có số nên nó không lọt vào tờ đối chiếu — tức phép cộng
        # đôi mà lát này sửa sẽ không có ca nào chứng minh.
        service.issue(outcome.replacement.id, invoice_date=OCT_20)
        service.confirm(outcome.replacement.id, tax_authority_code="M1-7G3-0005")
        ids["replaced"] = replaced.id
        ids["replacement"] = outcome.replacement.id

        # (7) Tờ ĐÃ PHÁT HÀNH trên chứng từ CHƯA GHI SỔ. Vòng đời cho phép
        # (FR-EIV-011), và cả ba vế tiền của tờ đối chiếu đều đọc `gl_postings`,
        # nên đây là ca duy nhất sinh ra một dòng LỆCH thật — không có nó thì cột
        # chênh và nhánh lọc "chỉ dòng lệch" chưa từng chạy.
        unposted_voucher = _sales_voucher(
            session, context, accounts, posting_date=OCT_20, post=False
        )
        unposted = service.create_draft(source_voucher_id=unposted_voucher, invoice_form_id=FORM_ID)
        service.issue(unposted.id, invoice_date=OCT_20)
        service.confirm(unposted.id, tax_authority_code="M1-7G3-0004")
        ids["issued_unposted"] = unposted.id

        # (8) Tờ trên ký hiệu của NHÀ CUNG CẤP, đã qua lượt phát hành nhưng
        # **chưa có số** — nhà cung cấp cấp số ở lượt xác nhận.
        provider_voucher = _sales_voucher(session, context, accounts, posting_date=OCT_20)
        provider_invoice = service.create_draft(
            source_voucher_id=provider_voucher, invoice_form_id=PROVIDER_FORM_ID
        )
        service.issue(provider_invoice.id, invoice_date=OCT_20)
        assert provider_invoice.invoice_no is None, "ký hiệu này phải để nhà cung cấp cấp số"
        assert provider_invoice.status == EInvoiceStatus.DANG_PHAT_HANH
        ids["provider_issuing"] = provider_invoice.id
    return ids


@pytest.fixture(scope="module")
def report_role(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> str:
    """Quyền báo cáo **và** quyền xem hóa đơn điện tử: bốn định nghĩa của lát này
    khai `required_permission_module = "einvoice"` (bản vá H-1b của 6E-1)."""
    return ensure_role(
        session_factory,
        dataset_alpha,
        REPORT_ROLE,
        ["reporting.report.view", permission_code("einvoice", "invoice", Action.VIEW)],
    )


@pytest.fixture
def preview(
    client: TestClient,
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    user_factory: UserFactory,
    secret_box: SecretBox,
    test_password: str,
    context: PostingContext,
    report_role: str,
) -> Preview:
    # `actor` thường KHÔNG dùng được ở đây: quyền hóa đơn điện tử khai
    # `requires_second_factor` (kể cả `view` — 7D, vì phát hành có hệ quả pháp
    # lý), nên đăng nhập bằng mật khẩu trần chỉ nhận một phiên hạn chế và mọi
    # lượt preview trả 403. Bốn báo cáo của lát này đọc đúng dữ liệu mà màn hình
    # hóa đơn đọc, nên để chúng đi vòng qua 2FA là mở một lối vòng.
    headers = {
        **actor_with_totp(
            client,
            session_factory,
            dataset_alpha,
            user_factory,
            secret_box,
            report_role,
            "bao_cao_hoa_don_dien_tu",
            test_password,
            branch_codes=[context.branch_code],
        ),
        BRANCH_HEADER: str(context.branch_id),
    }

    def run(code: str, **params: object) -> PreviewResult:
        body = {
            "params": {
                "from_date": OCT_01.isoformat(),
                "to_date": NOV_30.isoformat(),
                **params,
            }
        }
        response = client.post(f"/api/v1/reports/{code}/preview", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return PreviewResult(response.json())

    return run


class TestTheDraftInvoiceSurvivesThePeriodWindow:
    """Hóa đơn CHƯA PHÁT HÀNH chưa có ngày, và #3 tồn tại để liệt kê nó.

    `invoice_date` chỉ tới lúc cấp số. Cắt cửa sổ kỳ thẳng theo cột ấy vì thế
    **loại sạch** đúng nhóm mà tờ giấy sinh ra để cho thấy — loại bằng một tai nạn
    về NULL, không bằng chủ đích, đúng hình dạng lỗi mà 7G-1 đã trả giá ở khoản
    ứng trước đầu kỳ.
    """

    def test_a_draft_appears_on_the_status_report(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        report = preview("danh-sach-hoa-don-theo-trang-thai", status=EInvoiceStatus.CHUA_PHAT_HANH)
        assert len(report.rows) == 1
        # Chưa cấp số thì cột số hóa đơn rỗng — và dòng vẫn phải có mặt.
        assert all(row["invoice_no"].strip() == "" for row in report.rows)
        assert "Chưa phát hành" in report.headings

    def test_the_draft_falls_into_the_period_of_its_source_document(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Tờ nháp thuộc kỳ của chứng từ sinh ra nó, không thuộc "không kỳ nào".

        Chứng từ gốc lập 05/10, nên đọc kỳ 01/10–10/10 phải thấy; đọc kỳ tháng 11
        thì không. Một bản bỏ hẳn phép cắt ngày sẽ xanh ở vế đầu và đỏ ở vế sau.
        """
        inside = preview(
            "danh-sach-hoa-don-theo-trang-thai",
            status=EInvoiceStatus.CHUA_PHAT_HANH,
            from_date=OCT_01.isoformat(),
            to_date=OCT_10.isoformat(),
        )
        assert len(inside.rows) == 1
        outside = preview(
            "danh-sach-hoa-don-theo-trang-thai",
            status=EInvoiceStatus.CHUA_PHAT_HANH,
            from_date=date(2026, 11, 1).isoformat(),
            to_date=NOV_30.isoformat(),
        )
        assert outside.rows == []

    def test_a_draft_of_an_unposted_voucher_shows_an_empty_amount(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Ô tiền rỗng, KHÔNG phải dòng biến mất.

        Chứng từ chưa ghi sổ chưa có dòng phát sinh nào, nên phép nối sang sổ phải
        là `LEFT JOIN LATERAL`. Một `INNER JOIN` ở đó làm cả nhóm "chưa phát hành"
        biến mất y như một phép cắt ngày ngây thơ — hai đường khác nhau tới cùng
        một tờ giấy trống.
        """
        report = preview("danh-sach-hoa-don-theo-trang-thai", status=EInvoiceStatus.CHUA_PHAT_HANH)
        # Đúng tờ nháp của chứng từ CHƯA ghi sổ — nhận ra bằng ngày chứng từ của
        # nó, không bằng vị trí trong danh sách: tờ thay thế cũng là một bản nháp
        # nhưng chứng từ của nó đã ghi sổ, nên ô tiền của nó KHÁC 0.
        rows = [row for row in report.rows if row["period_date"] == "05/10/2026"]
        assert len(rows) == 1
        assert money(rows[0], "total_amount") == 0


class TestIssuedIsAThresholdNotAStatus:
    """Số hóa đơn tiêu từ `DANG_PHAT_HANH` và không nhả ra nữa."""

    def test_the_register_lists_every_invoice_that_consumed_a_number(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Bảng kê "đã phát hành" gồm cả tờ LỖI và tờ ĐÃ HỦY.

        Cả hai giữ số đã cấp (`PHAT_HANH_LOI` theo `EInvoiceStatus`, `DA_HUY` theo
        BR-INV-04), nên cả hai là những tờ giấy đã tồn tại. Liệt kê tay vài trạng
        thái vào `fixed_params` thay cho một ngưỡng thì lát sau thêm một trạng thái
        là bảng kê im lặng bỏ sót nó.
        """
        report = preview("bang-ke-hoa-don-dien-tu")
        numbers = {row["invoice_no"] for row in report.rows}
        assert "" not in numbers, "tờ chưa cấp số không thuộc bảng kê đã phát hành"
        # Sáu tờ có số trên ký hiệu thứ nhất + một tờ của ký hiệu thứ hai. Tờ trên
        # ký hiệu của nhà cung cấp KHÔNG có mặt: nó đã qua lượt phát hành nhưng
        # chưa có số, và đó đúng là ca mà ngưỡng theo trạng thái đọc sai.
        assert len(report.rows) == 7

    def test_the_draft_is_the_only_invoice_left_out(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Vế đối chứng: đúng một tờ ở ngoài, và nó là tờ chưa tiêu số.

        Không có vế này thì `issued_only` lọc sai chiều vẫn xanh ở bài trên.
        """
        everything = preview("danh-sach-hoa-don-theo-trang-thai")
        register = preview("bang-ke-hoa-don-dien-tu")
        numberless = [row for row in everything.rows if row["invoice_no"].strip() == ""]
        assert numberless, "bộ gieo không còn tờ nào chưa cấp số"
        assert len(everything.rows) == len(register.rows) + len(numberless)


class TestTheMoneyComesFromTheBooks:
    def test_each_posted_invoice_shows_the_receivable_side_of_its_voucher(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Tiền trên bảng kê = tiền hàng + thuế, đọc từ vế ghi Nợ TK phải thu.

        Cộng vế doanh thu rồi cộng thêm vế thuế là dựng lại cùng con số bằng hai
        phép cộng thay vì một — và hai phép cộng ấy lệch nhau ở hóa đơn nào mapper
        ghi thêm một cặp.
        """
        rows = preview("bang-ke-hoa-don-dien-tu").rows
        amounts = [money(row, "total_amount") for row in rows]
        # Đúng một tờ mang 0: tờ đã phát hành trên chứng từ CHƯA ghi sổ. Mọi tờ
        # còn lại mang trọn tiền hàng + thuế.
        assert amounts.count(Decimal(0)) == 1
        assert all(amount in (Decimal(0), INVOICE_TOTAL) for amount in amounts)

    def test_the_register_does_not_total_its_money_column(self) -> None:
        """Bảng kê KHÔNG có dòng tổng tiền, và đó là một quyết định.

        Lượt THAY THẾ dựng một tờ mới trên **chính** chứng từ cũ, nên tờ bị thay
        thế và tờ thay thế nó đọc cùng một bộ phát sinh. Cả hai đều là chứng từ
        pháp lý đã tồn tại và bảng kê không được giấu tờ nào, nhưng cộng chúng lại
        cho ra 2,2 triệu cho một lần bán 1,1 triệu — một con số trông như doanh
        thu mà không phải doanh thu. Cùng doctrine với "không cộng USD với EUR":
        thứ không cộng được thì đừng in, vì nó nguy hiểm hơn một ô trống.
        """
        layouts = {layout.code: layout for layout in load_builtin_reports().manifest.layouts}
        assert load_builtin_reports().layout_specs["einvoice-register"].totals == ()
        assert layouts["einvoice-register"].kind == "table"


class TestTheReconciliationSheet:
    def test_a_clean_invoice_shows_no_variance(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        report = preview("doi-chieu-hoa-don-voi-doanh-thu")
        posted = [row for row in report.rows if row["posting_state"] == "Đã ghi sổ"]
        assert posted
        assert all(money(row, "variance") == 0 for row in posted)

    def test_the_two_sides_are_measured_separately(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hai vế phải là HAI con số khác nhau, nếu không phép so không kiểm gì.

        Vế trái là tổng tiền thanh toán (tiền hàng + thuế), vế phải là doanh thu
        THUẦN. Một bản đọc cùng một cột cho cả hai vế sẽ luôn cho chênh bằng 0 —
        xanh tuyệt đối, và vô dụng tuyệt đối.
        """
        row = next(
            row
            for row in preview("doi-chieu-hoa-don-voi-doanh-thu").rows
            if row["posting_state"] == "Đã ghi sổ"
        )
        assert money(row, "invoice_amount") == INVOICE_TOTAL
        assert money(row, "revenue_amount") == GOODS_AMOUNT
        assert money(row, "vat_amount") == GOODS_VAT
        assert money(row, "revenue_amount") != money(row, "invoice_amount")

    def test_an_issued_invoice_on_an_unposted_voucher_is_a_mismatch(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Chứng từ chưa ghi sổ là dòng LỆCH, không phải dòng sạch.

        Cả ba vế tiền đọc `gl_postings`, nên một tờ đã phát hành trên chứng từ còn
        nháp cho ra `0 − 0 − 0 = 0` và tờ giấy báo "khớp" — đúng ca mà SRS 07 §5 #4
        tồn tại để bắt, và vòng đời cho phép trạng thái ấy (FR-EIV-011). Đây cũng
        là ca duy nhất của bộ gieo sinh ra một dòng lệch thật: không có nó thì
        nhánh lọc "chỉ dòng lệch" chưa từng chạy và cột chênh chưa từng khác 0.
        """
        everything = preview("doi-chieu-hoa-don-voi-doanh-thu")
        mismatches = preview("doi-chieu-hoa-don-voi-doanh-thu", mismatch_only=True)
        assert everything.rows
        assert len(mismatches.rows) == 1
        row = mismatches.rows[0]
        assert row["posting_state"] == "CHƯA ghi sổ"
        assert money(row, "invoice_amount") == 0
        assert len(mismatches.rows) < len(everything.rows)

    def test_a_superseded_invoice_leaves_the_reconciliation(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Tờ đã BỊ THAY THẾ đứng ngoài, nếu không doanh thu cộng đôi.

        `_supersede` dùng lại **chính** `source_voucher_id`, nên tờ cũ và tờ thay
        thế nó đọc cùng một bộ phát sinh: mỗi dòng tự nó nói chênh 0 — tờ giấy
        xanh trót lọt — trong khi cột doanh thu ở dòng TỔNG cộng gấp đôi. Hỏng
        theo kiểu tệ nhất: không dòng nào đỏ, và con số tổng thì sai.

        Tờ đã ĐIỀU CHỈNH thì ở lại: hai `kind` điều chỉnh mang phần chênh trên một
        chứng từ KHÁC, nên tờ gốc vẫn đối chiếu được với chứng từ của chính nó.
        """
        report = preview("doi-chieu-hoa-don-voi-doanh-thu")
        # Mỗi chứng từ gốc xuất hiện ĐÚNG MỘT LẦN — đó là thứ phép cộng đôi phá,
        # và nó là khẳng định duy nhất không phụ thuộc nhãn hiển thị.
        vouchers = [row["source_voucher_no"] for row in report.rows]
        assert len(vouchers) == len(set(vouchers))
        # Vế dương: tờ THAY THẾ (tờ đang có hiệu lực) phải CÓ MẶT, nếu không phép
        # loại ở trên có thể loại nhầm cả hai tờ mà bài kiểm vẫn xanh.
        assert any(row["invoice_no"] == "00000006" for row in report.rows), report.texts()


class TestTheNumberRangeSheet:
    def test_used_counts_from_the_moment_a_number_is_taken(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Ba tờ tiêu số ở ký hiệu thứ nhất: đã phát hành, lỗi, đã hủy.

        Đếm từ `DA_PHAT_HANH` sẽ ra 1 và báo "còn 99" trong khi dải đã tiêu 3 số —
        người dùng đi xin cấp thêm dải trong khi dải cũ chưa hết, hoặc tưởng mình
        còn số để dùng.
        """
        report = preview("bang-ke-dai-so-hoa-don")
        first = self._row_of(report, SERIAL, RANGE_FROM)
        # Sáu số đã cấp trên ký hiệu này, tất cả nằm trong dải thứ nhất.
        assert int(first["used_count"]) == 6
        assert int(first["remaining_count"]) == RANGE_QUANTITY - 6

    def test_cancelled_is_a_slice_of_used_not_a_column_beside_it(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Cộng `đã dùng + đã hủy` là đếm hai lần chính những tờ ấy."""
        row = self._row_of(preview("bang-ke-dai-so-hoa-don"), SERIAL, RANGE_FROM)
        assert int(row["cancelled_count"]) == 1
        assert int(row["cancelled_count"]) < int(row["used_count"])

    def test_each_registration_counts_only_the_numbers_inside_its_own_range(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hai hồ sơ trên cùng một ký hiệu KHÔNG cùng báo một con số.

        Một ký hiệu được thông báo phát hành nhiều lần (không có `UNIQUE` trên cặp
        (ký hiệu, chi nhánh) — xem `registration_service.effective_for`). Bản đầu
        của lát này đếm mọi hóa đơn của cặp ấy cho MỌI hồ sơ, nên hồ sơ `101..200`
        in `đã dùng 5 / còn 95` trong khi nó chưa tiêu số nào — và người đọc thấy
        một dải sắp hết ở chỗ dải vừa mới xin.
        """
        report = preview("bang-ke-dai-so-hoa-don")
        second = self._row_of(report, SERIAL, SECOND_RANGE_FROM)
        assert int(second["used_count"]) == 0
        assert int(second["cancelled_count"]) == 0
        assert int(second["remaining_count"]) == SECOND_RANGE_QUANTITY

    def test_the_cut_off_date_narrows_what_counts_as_used(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Mốc chốt áp cho ngày hóa đơn: đọc tới 10/10 chỉ thấy số cấp tới đó.

        Không có vế này thì bỏ hẳn phép cắt ngày vẫn xanh, và bảng kê dải số nói
        về "hôm nay" kể cả khi người dùng hỏi về cuối tháng trước.
        """
        early = preview("bang-ke-dai-so-hoa-don", to_date=OCT_10.isoformat())
        row = self._row_of(early, SERIAL, RANGE_FROM)
        assert int(row["used_count"]) == 2
        assert int(row["cancelled_count"]) == 0

    def test_a_registration_without_a_declared_range_leaves_remaining_empty(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """ "Không khai dải" và "hết số" là hai câu khác hẳn nhau.

        In 0 cho câu đầu là dựng một cảnh báo giả, và người dùng đi xin cấp dải
        cho một hồ sơ vốn không cần dải.
        """
        row = self._row_of(preview("bang-ke-dai-so-hoa-don"), SECOND_SERIAL, None)
        assert row["quantity"].strip() == ""
        assert row["remaining_count"].strip() == ""
        # Vế dương: số đã tiêu vẫn đếm được dù hồ sơ không khai dải.
        assert int(row["used_count"]) == 1

    @staticmethod
    def _row_of(report: PreviewResult, serial: str, range_from: int | None) -> dict[str, str]:
        """Một dòng hồ sơ, nhận dạng bằng (ký hiệu, đầu dải).

        Ký hiệu một mình KHÔNG đủ: một ký hiệu có nhiều hồ sơ, và đó chính là
        hình dạng dữ liệu mà lỗi đếm của bản đầu ẩn trong.
        """
        wanted = "" if range_from is None else str(range_from)
        rows = [
            row
            for row in report.rows
            if row["form_code"] == serial and row["range_from"].strip() == wanted
        ]
        assert len(rows) == 1, f"không có đúng một dòng cho {serial} từ số {wanted!r}"
        return rows[0]
