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
