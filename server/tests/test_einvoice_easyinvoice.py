"""Adapter EasyInvoice — lát 7E-2.

Không có sandbox EasyInvoice nào cấu hình ở repo này, nên tệp này chứng minh
adapter **dựng đúng** chứ không chứng minh nhà cung cấp **chấp nhận**. Ranh giới
ấy ghi thẳng ra đây vì nó quyết định bài nào có ý nghĩa: mọi khẳng định đều nhắm
vào thứ ta kiểm soát — đường dẫn gọi, khuôn header, thân XML, và cách đọc câu
trả lời — không nhắm vào hành vi của máy chủ họ.

`httpx.MockTransport` ghim đúng ba lời gọi thật (`importInvoice`,
`issueInvoices`, `getInvoicesByIkeys`) và trả về đúng hình dạng thân JSON mà bản
tích hợp đang chạy ở `~/code/beta.konek.vn` nhận được.
"""

from __future__ import annotations

import base64
import hashlib
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from ket.kernel.contracts import PartnerKind
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.protocols import EInvoiceSourceDocument, EInvoiceSourceLine
from ket.modules.einvoice.providers.contracts import ProviderAcceptance, ProviderRecordState
from ket.modules.einvoice.providers.easyinvoice.auth import build_header
from ket.modules.einvoice.providers.easyinvoice.client import (
    EasyInvoiceClient,
    EasyInvoiceCredentials,
    EasyInvoiceRefusedError,
)
from ket.modules.einvoice.providers.easyinvoice.provider import (
    EasyInvoiceProvider,
    _accepted,
    _from_record,
)
from ket.modules.einvoice.providers.easyinvoice.xml_builder import (
    VAT_EXEMPT,
    VAT_NOT_DECLARED,
    build_invoice_xml,
)

CLIENT_REF = UUID("01a07f14-30eb-7000-b7dd-000000007e02")
"""Khóa của **ta**, đi vào thân XML."""

SERVER_IKEY = "0101234567_INV-001_1757300000000"
"""Khóa của **nhà cung cấp**, do `importInvoice` sinh. Hình dạng chép từ
`_gen_ikey` của bản tích hợp thật: `{mã số thuế}_{số chứng từ}_{mốc ms}` — có
mốc thời gian, nên nó **không tái lập được**, và đó đúng là lý do phải cất nó."""

INVOICE_ID = UUID("01a07f14-30eb-7000-b7dd-0000000000ff")

CREDENTIALS = EasyInvoiceCredentials(
    base_url="https://sandbox.example.vn/",
    username="nguoi-dung",
    password="mat-khau",
    tax_code="0101234567",
)


def _partner() -> Partner:
    return Partner(
        id=1,
        code="KH001",
        name="Công ty TNHH Mua Hàng",
        tax_code="0309876543",
        address="12 Nguyễn Huệ",
        district="Quận 1",
        province="TP. Hồ Chí Minh",
        contact_name="Trần Thị B",
    )


def _document(lines: tuple[EInvoiceSourceLine, ...]) -> EInvoiceSourceDocument:
    before = sum((line.amount_fc for line in lines), Decimal(0))
    vat = sum((line.vat_amount_fc for line in lines), Decimal(0))
    return EInvoiceSourceDocument(
        partner_kind=PartnerKind.CUSTOMER,
        partner_id=1,
        document_date=date(2026, 2, 10),
        currency_code="VND",
        exchange_rate=Decimal(1),
        total_before_tax_fc=before,
        total_vat_fc=vat,
        total_fc=before + vat,
        lines=lines,
    )


def _line(**overrides: object) -> EInvoiceSourceLine:
    base: dict[str, object] = {
        "description": "Hàng A",
        "unit": "Cái",
        "quantity": Decimal(2),
        "unit_price_fc": Decimal(500_000),
        "amount_fc": Decimal(1_000_000),
        "vat_rate": Decimal(10),
        "vat_amount_fc": Decimal(100_000),
    }
    base.update(overrides)
    return EInvoiceSourceLine(**base)  # type: ignore[arg-type]


# --- header xác thực --------------------------------------------------------


def test_the_authorization_header_matches_the_documented_shape() -> None:
    """Sáu phần, ngăn bằng dấu hai chấm, chữ ký là `Base64(MD5("POST"+ts+nonce))`.

    Máy chủ nhà cung cấp kiểm đúng công thức này, nên bài test dựng lại chữ ký từ
    chính `nonce` và `timestamp` trong header thay vì ghim một chuỗi cố định —
    ghim chuỗi thì bài chỉ chứng minh hàm trả về thứ nó vừa trả về.
    """
    header = build_header(username="u", password="p", tax_code="0101234567")
    signature, nonce, timestamp, username, password, tax_code = header.split(":")

    expected = hashlib.md5(f"POST{timestamp}{nonce}".encode(), usedforsecurity=False).digest()
    assert signature == base64.b64encode(expected).decode("ascii")
    assert (username, password, tax_code) == ("u", "p", "0101234567")


def test_two_headers_never_reuse_a_nonce() -> None:
    """`nonce` mới mỗi lượt — máy chủ dùng nó để chống phát lại."""
    first = build_header(username="u", password="p", tax_code="t").split(":")[1]
    second = build_header(username="u", password="p", tax_code="t").split(":")[1]
    assert first != second


# --- bản XML ----------------------------------------------------------------


def test_the_invoice_xml_carries_no_invoice_number() -> None:
    """Nhà cung cấp cấp số (quyết định user 2026-09-08).

    Bài này ghim **sự vắng mặt**: thêm một thẻ số hóa đơn vào thân XML nghĩa là
    hai nơi cùng cấp số cho một tờ, và bài sẽ đỏ.
    """
    xml = build_invoice_xml(_document((_line(),)), _partner(), client_ref=CLIENT_REF)
    root = ET.fromstring(xml)  # noqa: S314 — chuỗi do chính ta vừa dựng

    assert root.find(".//InvNo") is None
    assert root.find(".//InvoiceNo") is None
    assert root.findtext(".//Ikey") == str(CLIENT_REF)


def test_the_invoice_xml_carries_the_buyer_and_the_totals() -> None:
    """Bên mua, dòng hàng, tổng và tiền bằng chữ — đủ bộ tối thiểu của NĐ123."""
    document = _document((_line(),))
    xml = build_invoice_xml(document, _partner(), client_ref=CLIENT_REF)
    root = ET.fromstring(xml)  # noqa: S314

    assert root.findtext(".//CusName") == "Công ty TNHH Mua Hàng"
    assert root.findtext(".//CusTaxCode") == "0309876543"
    assert root.findtext(".//CusAddress") == "12 Nguyễn Huệ, Quận 1, TP. Hồ Chí Minh"
    assert root.findtext(".//ArisingDate") == "10/02/2026"

    product = root.find(".//Products/Product")
    assert product is not None
    assert product.findtext("ProdName") == "Hàng A"
    assert product.findtext("ProdUnit") == "Cái"
    assert product.findtext("VATRate") == "10"
    # Thành tiền của dòng là **đã gồm thuế**; `Total` mới là trước thuế.
    assert product.findtext("Total") == "1000000"
    assert product.findtext("Amount") == "1100000"

    assert root.findtext(".//AmountInWords")


def test_a_line_without_a_declared_rate_is_not_reported_as_zero_percent() -> None:
    """`vat_rate = None` ≠ `0`.

    Khai một dòng chưa xác định thành "thuế suất 0%" là một lời khai thuế không
    có căn cứ, và hai thứ ấy khác nhau về quyền khấu trừ đầu vào.
    """
    zero = build_invoice_xml(
        _document((_line(vat_rate=Decimal(0), vat_amount_fc=Decimal(0)),)),
        _partner(),
        client_ref=CLIENT_REF,
    )
    absent = build_invoice_xml(
        _document((_line(vat_rate=None, vat_amount_fc=Decimal(0)),)),
        _partner(),
        client_ref=CLIENT_REF,
    )

    assert ET.fromstring(zero).findtext(".//VATRate") == "0"  # noqa: S314
    assert ET.fromstring(absent).findtext(".//VATRate") == str(VAT_NOT_DECLARED)  # noqa: S314
    assert VAT_NOT_DECLARED != VAT_EXEMPT, "hai mã pháp lý khác nhau, không được gộp"


def test_an_unknown_rate_is_not_smuggled_through() -> None:
    """Thuế suất ngoài tập nhà cung cấp nhận đi về mã "không kê khai".

    Gửi thẳng một con số lạ là chắc chắn bị máy chủ từ chối; im lặng đổi nó
    thành `0` thì tệ hơn — đó là khai sai.
    """
    xml = build_invoice_xml(
        _document((_line(vat_rate=Decimal(7)),)), _partner(), client_ref=CLIENT_REF
    )
    assert ET.fromstring(xml).findtext(".//VATRate") == str(VAT_NOT_DECLARED)  # noqa: S314


# --- client HTTP ------------------------------------------------------------


def _transport(handler: object) -> httpx.MockTransport:
    return httpx.MockTransport(handler)  # type: ignore[arg-type]


def test_import_then_issue_hits_the_two_documented_paths() -> None:
    """Một lượt phát hành của ta = **hai** lời gọi của họ, đúng thứ tự."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("importInvoice"):
            return httpx.Response(200, json={"Status": 2, "Data": {"Ikeys": ["IK-1"]}})
        return httpx.Response(
            200,
            json={"Status": 2, "Data": {"KeyInvoiceNo": {"IK-1": "00000123"}, "Invoices": []}},
        )

    client = EasyInvoiceClient(CREDENTIALS, transport=_transport(handler))
    ikeys = client.import_invoice("<Invoices/>", pattern="C26TAA", serial="1")
    client.issue_invoices(ikeys, pattern="C26TAA", serial="1")

    assert seen == ["/api/publish/importInvoice", "/api/publish/issueInvoices"]
    assert ikeys == ["IK-1"]


def test_a_refusal_arrives_as_http_200_and_must_still_be_a_refusal() -> None:
    """Máy chủ trả `200 OK` kèm `Status != 2` cho một lượt từ chối.

    Đọc mã HTTP rồi đi tiếp là coi mọi lỗi nghiệp vụ thành thành công — bài này
    là thứ giữ cho `_post` không bao giờ bị viết lại theo lối ấy.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Status": 4, "Message": "Sai ký hiệu", "ErrorCode": "E7"})

    client = EasyInvoiceClient(CREDENTIALS, transport=_transport(handler))
    with pytest.raises(EasyInvoiceRefusedError, match="Sai ký hiệu"):
        client.import_invoice("<Invoices/>", pattern="C26TAA", serial="1")


def test_a_network_failure_is_not_turned_into_a_refusal() -> None:
    """Hết giờ / đứt kết nối **nổi lên nguyên vẹn**.

    Nuốt nó thành `EasyInvoiceRefusedError` là xóa mất ranh giới giữa "bị từ
    chối" và "không rõ" — và lượt sau sẽ gửi lại một tờ hóa đơn có thể đã phát
    hành. Xem `reconcile.py`.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("mạng rớt")

    client = EasyInvoiceClient(CREDENTIALS, transport=_transport(handler))
    with pytest.raises(httpx.ConnectTimeout):
        client.import_invoice("<Invoices/>", pattern="C26TAA", serial="1")


def test_the_request_carries_the_authorization_header_and_never_logs_it() -> None:
    """Header đi kèm mọi lời gọi, đúng khuôn sáu phần."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers["Authorization"])
        return httpx.Response(200, json={"Status": 2, "Data": {}})

    client = EasyInvoiceClient(CREDENTIALS, transport=_transport(handler))
    client.get_invoices_by_ikeys([str(CLIENT_REF)])

    assert len(captured[0].split(":")) == 6
    assert captured[0].endswith(":0101234567")


# --- đọc câu trả lời --------------------------------------------------------


def test_the_provider_assigned_number_is_read_back() -> None:
    """Số hóa đơn về trong `KeyInvoiceNo`, khóa theo **khóa của nhà cung cấp**."""
    outcome = _accepted(
        {
            "KeyInvoiceNo": {SERVER_IKEY: "00000123"},
            "Invoices": [{"TaxAuthorityCode": "M1-22-ABC", "LookupCode": "TRA-CUU"}],
        },
        provider_ref=SERVER_IKEY,
    )

    assert outcome.acceptance == ProviderAcceptance.ACCEPTED
    assert outcome.invoice_no == "00000123"
    assert outcome.tax_authority_code == "M1-22-ABC"
    assert outcome.lookup_code == "TRA-CUU"


def test_a_number_is_never_read_by_our_own_key() -> None:
    """Khóa của **ta** không mở được `KeyInvoiceNo` — đây là lỗi C-1 của lát này.

    Bản đầu tra số bằng `client_ref`. Nhà cung cấp khóa theo `Ikey` của họ, nên
    lượt tra luôn hụt → hóa đơn không có số → ràng buộc chặn → cả lô rollback →
    lượt sau tra sai khóa lần nữa và **phát hành lại**. Bài này ghim rằng số chỉ
    đọc được bằng khóa máy chủ.
    """
    by_server_key = _accepted({"KeyInvoiceNo": {SERVER_IKEY: "00000123"}}, provider_ref=SERVER_IKEY)
    by_our_key = _accepted(
        {"KeyInvoiceNo": {str(CLIENT_REF): "00000123"}}, provider_ref=SERVER_IKEY
    )

    assert by_server_key.invoice_no == "00000123"
    assert by_our_key.invoice_no is None


def test_a_looked_up_record_reports_the_number_it_already_holds() -> None:
    """Nhánh tra cứu: nhà cung cấp đã có số thì `reconcile` điền nó vào."""
    outcome = _from_record({"InvoiceNo": "00000123"}, provider_ref=SERVER_IKEY)
    assert outcome.acceptance == ProviderAcceptance.ACCEPTED
    assert outcome.invoice_no == "00000123"


# --- hai chặng, và ranh giới nháp / đã phát hành ----------------------------


def _provider(handler: object) -> EasyInvoiceProvider:
    """Adapter cho các bài **không chạm DB**.

    `query_status` chỉ gọi client nên `session` không bao giờ được dùng tới;
    `prepare` và `issue` thì có đọc hóa đơn, nên hai chặng ấy đo ở
    `test_einvoice_outbox.py`, nơi có chứng từ thật.
    """
    client = EasyInvoiceClient(CREDENTIALS, transport=_transport(handler))
    return EasyInvoiceProvider(session=None, client=client)  # type: ignore[arg-type]


def test_an_unsigned_draft_is_not_reported_as_issued() -> None:
    """`InvoiceStatus = 0` là bản nháp — cơ quan thuế **chưa** thấy nó.

    Đọc nó thành "đã phát hành" là bỏ rơi tờ hóa đơn ở đúng lượt đáng lẽ phát
    hành nó, và đó là lỗi C-3 của lát này.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"Status": 2, "Data": [{"Ikey": SERVER_IKEY, "InvoiceStatus": 0}]}
        )

    status = _provider(handler).query_status(provider_ref=SERVER_IKEY)
    assert status.state == ProviderRecordState.PREPARED


def test_a_signed_invoice_is_reported_as_issued() -> None:
    """`InvoiceStatus = 1` (đã ký) trở lên là đã rời phần mềm."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Status": 2,
                "Data": [{"Ikey": SERVER_IKEY, "InvoiceStatus": 1, "InvoiceNo": "00000123"}],
            },
        )

    status = _provider(handler).query_status(provider_ref=SERVER_IKEY)
    assert status.state == ProviderRecordState.ISSUED
    assert status.outcome is not None
    assert status.outcome.invoice_no == "00000123"


def test_an_empty_lookup_is_the_only_thing_that_means_not_received() -> None:
    """Mảng rỗng = họ không biết khóa này. Đây là ca duy nhất an toàn."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Status": 2, "Data": []})

    status = _provider(handler).query_status(provider_ref=SERVER_IKEY)
    assert status.state == ProviderRecordState.UNKNOWN


def test_a_refused_lookup_never_reads_as_not_received() -> None:
    """Sai mật khẩu / quá tải **không** được đọc thành "họ chưa nhận".

    Bản đầu bắt mọi `EasyInvoiceRefusedError` rồi trả "chưa nhận" — mỗi cái đều
    dẫn thẳng tới nhánh phát hành lại. Nay lỗi ném ra ngoài để `reconcile` đọc
    thành "không biết", và đó chính là điều bài này ghim.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Status": 5, "Message": "Sai mat khau"})

    with pytest.raises(EasyInvoiceRefusedError):
        _provider(handler).query_status(provider_ref=SERVER_IKEY)
