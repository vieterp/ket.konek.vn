"""Khung dựng bối cảnh cho test phân hệ Hóa đơn điện tử (lát 7D).

Bồi lên `sales_support`: hóa đơn điện tử luôn sinh từ một chứng từ gốc, và
chứng từ gốc duy nhất tồn tại ở lát này là hóa đơn bán (`SAL`). Hai thứ riêng
của phân hệ này phải gieo thêm:

* một **ký hiệu hóa đơn** trong danh mục `invoice_forms` — mã danh mục mang ký
  hiệu, cột `form_no` mang mẫu số (xem `models/invoice_form.py`);
* một **hồ sơ đăng ký đã kích hoạt**, vì BR-EIV-03 chặn mọi lượt cấp số khi
  chưa có hồ sơ còn hiệu lực phủ ngày hóa đơn. Chính lượt kích hoạt ấy là chỗ
  dãy số của ký hiệu được khai.

Mỗi tệp test tự chọn ký hiệu riêng của mình (`serial=`): dãy số là trạng thái
**toàn dataset** và không ai reset nó giữa các bài, nên hai tệp dùng chung một
ký hiệu sẽ thấy số bắt đầu ở chỗ tệp kia dừng lại — đúng khuôn bẫy "bài test
phụ thuộc thứ tự tệp" đã bắt được năm lần từ 7A tới 7C-5.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.master_data.models.invoice_form import InvoiceForm, InvoiceFormKind
from ket.modules.einvoice.models import InvoiceRegistration
from ket.modules.einvoice.registration_service import InvoiceRegistrationService

REGISTRATION_START = date(2026, 1, 1)
"""Ngày bắt đầu sử dụng của hồ sơ mẫu — trước mọi ngày hóa đơn các bài dùng."""


def ensure_invoice_form(
    session: Session,
    *,
    form_id: int,
    serial: str,
    form_no: str | None = "1",
    kind: InvoiceFormKind | None = InvoiceFormKind.DIEN_TU,
    provider_code: str | None = None,
    is_group: bool = False,
    is_active: bool = True,
) -> InvoiceForm:
    """Một ký hiệu hóa đơn với `id` cố định — idempotent để fixture module dùng lại.

        `provider_code` là thứ quyết định **ai cấp số** (7E-2): trống thì dãy gap-free
    cục bộ cấp, có thì nhà cung cấp cấp và `invoice_no` còn `NULL` tới lúc xác nhận.

    `id` truyền vào chứ không để tự tăng, cùng lối `ensure_customer` /
        `ensure_payment_term`: `path` của một dòng danh mục phải là chuỗi id có dấu
        chấm (`ck_..._path_is_dotted_ids`), và một id chưa biết thì không dựng được
        `path` hợp lệ trong cùng một câu `INSERT`.
    """
    existing = session.get(InvoiceForm, form_id)
    if existing is not None:
        existing.form_no = form_no
        existing.kind = kind
        existing.provider_code = provider_code
        existing.is_group = is_group
        existing.is_active = is_active
        session.flush()
        return existing
    form = InvoiceForm(
        id=form_id,
        code=serial,
        name=f"Ký hiệu {serial}",
        path=f"{form_id}.",
        form_no=form_no,
        kind=kind,
        provider_code=provider_code,
        is_group=is_group,
        is_active=is_active,
    )
    session.add(form)
    session.flush()
    return form


def ensure_active_registration(
    session: Session,
    *,
    branch_id: int,
    invoice_form_id: int,
    start_date: date = REGISTRATION_START,
    range_from: int | None = None,
    range_to: int | None = None,
) -> InvoiceRegistration:
    """Hồ sơ đăng ký đã kích hoạt cho một ký hiệu — mở đường cấp số (BR-EIV-03).

    Idempotent theo (ký hiệu, chi nhánh, ngày bắt đầu): nhiều bài trong một tệp
    gọi nó mà không sinh ra một chuỗi hồ sơ chồng nhau, và `activate` của lượt
    thứ hai trả về ngay vì hồ sơ đã ở trạng thái hiệu lực.
    """
    existing = session.scalar(
        select(InvoiceRegistration).where(
            InvoiceRegistration.invoice_form_id == invoice_form_id,
            InvoiceRegistration.branch_id == branch_id,
            InvoiceRegistration.start_date == start_date,
        )
    )
    service = InvoiceRegistrationService(session)
    if existing is None:
        existing = service.create(
            branch_id=branch_id,
            invoice_form_id=invoice_form_id,
            start_date=start_date,
            notice_no=f"TB-{invoice_form_id}-{start_date:%Y%m%d}",
            notice_date=start_date,
            range_from=range_from,
            range_to=range_to,
        )
    return service.activate(existing.id)


_DEFAULT_INBOUND_LINES = """        <HHDVu>
          <TChat>1</TChat>
          <STT>1</STT>
          <THHDVu>Thép hộp 20x40</THHDVu>
          <DVTinh>cây</DVTinh>
          <SLuong>10</SLuong>
          <DGia>120000</DGia>
          <ThTien>1200000</ThTien>
          <TSuat>10%</TSuat>
        </HHDVu>
        <HHDVu>
          <TChat>1</TChat>
          <STT>2</STT>
          <THHDVu>Công vận chuyển</THHDVu>
          <DVTinh>chuyến</DVTinh>
          <SLuong>1</SLuong>
          <DGia>800000</DGia>
          <ThTien>800000</ThTien>
          <TSuat>10%</TSuat>
        </HHDVu>"""

_DEFAULT_INBOUND_VAT_GROUPS = """          <LTSuat>
            <TSuat>10%</TSuat>
            <ThTien>2000000</ThTien>
            <TThue>200000</TThue>
          </LTSuat>"""


def inbound_xml(
    *,
    seller_tax_code: str = "0101243150",
    seller_name: str = "Công ty TNHH Vật tư Bình Minh",
    buyer_tax_code: str = "0312345678",
    buyer_name: str = "Công ty CP Thử Nghiệm",
    form: str = "1",
    serial: str = "C26TAA",
    number: str = "00004994",
    invoice_date: str = "2026-03-05",
    currency: str | None = None,
    lines: str = _DEFAULT_INBOUND_LINES,
    vat_groups: str = _DEFAULT_INBOUND_VAT_GROUPS,
    total_before_tax: str = "2000000",
    total_vat: str = "200000",
    total_amount: str = "2200000",
    namespace: str | None = None,
    prologue: str = "",
    related: str = "",
) -> bytes:
    """Một tệp XML hóa đơn đầu vào hình dạng TCT (TT78/QĐ1450).

    Hình dạng bám bản tích hợp đang chạy thật ở `~/code/beta.konek.vn`, không
    theo tài liệu — xem `inbound_parser` về ba thứ chỉ dữ liệu thật mới dạy.

    `namespace` và `prologue` mở đúng hai biến thể mà bài test cần dựng: cùng
    một tờ hóa đơn trong namespace của nhà cung cấp, và một tệp mang khối DTD
    ở đầu.
    """
    root_open = f'<HDon xmlns="{namespace}">' if namespace else "<HDon>"
    related_block = related
    currency_tag = f"<DVTTe>{currency}</DVTTe>" if currency else ""
    return (
        f"""<?xml version="1.0" encoding="UTF-8"?>
{prologue}{root_open}
  <DLHDon>
    <TTChung>
      <KHMSHDon>{form}</KHMSHDon>
      <KHHDon>{serial}</KHHDon>
      <SHDon>{number}</SHDon>
      <NLap>{invoice_date}</NLap>
      {currency_tag}
    </TTChung>
    {related_block}
    <NDHDon>
      <NBan>
        <Ten>{seller_name}</Ten>
        <MST>{seller_tax_code}</MST>
        <DChi>Số 1 phố Thử Nghiệm, Hà Nội</DChi>
      </NBan>
      <NMua>
        <Ten>{buyer_name}</Ten>
        <MST>{buyer_tax_code}</MST>
        <DChi>Số 2 đường Kiểm Thử, TP.HCM</DChi>
      </NMua>
      <DSHHDVu>
{lines}
      </DSHHDVu>
      <TToan>
        <THTTLTSuat>
{vat_groups}
        </THTTLTSuat>
        <TgTCThue>{total_before_tax}</TgTCThue>
        <TgTThue>{total_vat}</TgTThue>
        <TgTTTBSo>{total_amount}</TgTTTBSo>
      </TToan>
    </NDHDon>
  </DLHDon>
  <MCCQT>M1-26-ABCDE-12345678</MCCQT>
</HDon>
"""
    ).encode()


def inbound_lines_with(extra: str) -> str:
    """Hai dòng hàng mặc định, cộng thêm một dòng của riêng bài test."""
    return f"{_DEFAULT_INBOUND_LINES}\n{extra}"


def related_invoice_block(
    *,
    nature: str | None = "3",
    form: str = "1",
    serial: str = "C26TAA",
    number: str = "00001111",
    invoice_date: str = "2026-02-01",
) -> str:
    """Khối `TTHDLQuan` — tờ hóa đơn bị điều chỉnh hoặc thay thế.

    `nature=None` dựng đúng ca khó: khối liên quan **có mặt** mà `TCHDon` thì
    không, tức tệp nói tờ này liên quan tới tờ khác nhưng không nói liên quan
    kiểu gì — hai kiểu ấy ghi sổ ngược dấu nhau.
    """
    nature_tag = f"<TCHDon>{nature}</TCHDon>" if nature is not None else ""
    return (
        "<TTHDLQuan>"
        f"{nature_tag}"
        f"<KHMSHDCLQuan>{form}</KHMSHDCLQuan>"
        f"<KHHDCLQuan>{serial}</KHHDCLQuan>"
        f"<SHDCLQuan>{number}</SHDCLQuan>"
        f"<NLHDCLQuan>{invoice_date}</NLHDCLQuan>"
        "</TTHDLQuan>"
    )
