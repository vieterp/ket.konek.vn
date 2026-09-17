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
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.keystore import SecretBox
from ket.kernel.security.permissions import Action, permission_code
from ket.main import create_app
from ket.modules.einvoice.models import EInvoiceStatus, ErrorNoticeKind
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
CUSTOMER_ID = 9511
SERIAL = "C26TGA"
SECOND_SERIAL = "C26TGB"
CUSTOMER_CODE = "KH-7G3-01"

GOODS_AMOUNT = Decimal(1_000_000)
GOODS_VAT = Decimal(100_000)
INVOICE_TOTAL = GOODS_AMOUNT + GOODS_VAT
"""Tổng tiền thanh toán của tờ hóa đơn — phần ghi Nợ TK phải thu."""

RANGE_FROM = 1
RANGE_TO = 100
RANGE_QUANTITY = RANGE_TO - RANGE_FROM + 1


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
            invoice_form_id=SECOND_FORM_ID,
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
        row = report.rows[0]
        # Chưa cấp số thì cột số hóa đơn rỗng — và dòng vẫn phải có mặt.
        assert row["invoice_no"].strip() == ""
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
        assert money(report.rows[0], "total_amount") == 0


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
        assert len(report.rows) == 4

    def test_the_draft_is_the_only_invoice_left_out(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Vế đối chứng: đúng một tờ ở ngoài, và nó là tờ chưa tiêu số.

        Không có vế này thì `issued_only` lọc sai chiều vẫn xanh ở bài trên.
        """
        everything = preview("danh-sach-hoa-don-theo-trang-thai")
        register = preview("bang-ke-hoa-don-dien-tu")
        assert len(everything.rows) == len(register.rows) + 1


class TestTheMoneyComesFromTheBooks:
    def test_the_register_total_is_the_receivable_side_of_each_invoice(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Tiền trên bảng kê = tiền hàng + thuế, đọc từ vế ghi Nợ TK phải thu.

        Cộng vế doanh thu rồi cộng thêm vế thuế là dựng lại cùng con số bằng hai
        phép cộng thay vì một — và hai phép cộng ấy lệch nhau ở hóa đơn nào mapper
        ghi thêm một cặp.
        """
        report = preview("bang-ke-hoa-don-dien-tu")
        assert all(money(row, "total_amount") == INVOICE_TOTAL for row in report.rows)
        assert report.total("total_amount") == INVOICE_TOTAL * len(report.rows)


class TestTheReconciliationSheet:
    def test_a_clean_invoice_shows_no_variance(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        report = preview("doi-chieu-hoa-don-voi-doanh-thu")
        assert report.rows
        assert all(money(row, "variance") == 0 for row in report.rows)
        assert report.total("variance") == 0

    def test_the_two_sides_are_measured_separately(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Hai vế phải là HAI con số khác nhau, nếu không phép so không kiểm gì.

        Vế trái là tổng tiền thanh toán (tiền hàng + thuế), vế phải là doanh thu
        THUẦN. Một bản đọc cùng một cột cho cả hai vế sẽ luôn cho chênh bằng 0 —
        xanh tuyệt đối, và vô dụng tuyệt đối.
        """
        row = preview("doi-chieu-hoa-don-voi-doanh-thu").rows[0]
        assert money(row, "invoice_amount") == INVOICE_TOTAL
        assert money(row, "revenue_amount") == GOODS_AMOUNT
        assert money(row, "vat_amount") == GOODS_VAT
        assert money(row, "revenue_amount") != money(row, "invoice_amount")

    def test_mismatch_only_narrows_to_nothing_on_clean_books(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Trên sổ sạch, lọc "chỉ dòng lệch" phải trả về RỖNG.

        Đây là vế duy nhất phân biệt được `:mismatch_only` nối đúng với nối sai:
        một tham số bị bỏ quên vẫn cho ra cùng bộ dòng khi không có dòng nào lệch,
        nên bài kiểm phải so với vế KHÔNG lọc.
        """
        everything = preview("doi-chieu-hoa-don-voi-doanh-thu")
        mismatches = preview("doi-chieu-hoa-don-voi-doanh-thu", mismatch_only=True)
        assert everything.rows
        assert mismatches.rows == []


class TestTheNumberRangeSheet:
    def test_used_counts_from_the_moment_a_number_is_taken(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Ba tờ tiêu số ở ký hiệu thứ nhất: đã phát hành, lỗi, đã hủy.

        Đếm từ `DA_PHAT_HANH` sẽ ra 1 và báo "còn 99" trong khi dải đã tiêu 3 số —
        người dùng đi xin cấp thêm dải trong khi dải cũ chưa hết, hoặc tưởng mình
        còn số để dùng.
        """
        row = self._row_of(preview("bang-ke-dai-so-hoa-don"), SERIAL)
        assert int(row["used_count"]) == 3
        assert int(row["remaining_count"]) == RANGE_QUANTITY - 3

    def test_cancelled_is_a_slice_of_used_not_a_column_beside_it(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """Cộng `đã dùng + đã hủy` là đếm hai lần chính những tờ ấy."""
        row = self._row_of(preview("bang-ke-dai-so-hoa-don"), SERIAL)
        assert int(row["cancelled_count"]) == 1
        assert int(row["cancelled_count"]) < int(row["used_count"])

    def test_a_registration_without_a_declared_range_leaves_remaining_empty(
        self, preview: Preview, books: dict[str, UUID]
    ) -> None:
        """ "Không khai dải" và "hết số" là hai câu khác hẳn nhau.

        In 0 cho câu đầu là dựng một cảnh báo giả, và người dùng đi xin cấp dải
        cho một hồ sơ vốn không cần dải.
        """
        row = self._row_of(preview("bang-ke-dai-so-hoa-don"), SECOND_SERIAL)
        assert row["quantity"].strip() == ""
        assert row["remaining_count"].strip() == ""
        # Vế dương: số đã tiêu vẫn đếm được dù hồ sơ không khai dải.
        assert int(row["used_count"]) == 1

    @staticmethod
    def _row_of(report: PreviewResult, serial: str) -> dict[str, str]:
        rows = [row for row in report.rows if row["form_code"] == serial]
        assert len(rows) == 1, f"không có đúng một dòng cho ký hiệu {serial}"
        return rows[0]
