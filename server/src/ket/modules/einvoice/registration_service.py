"""Hồ sơ đăng ký sử dụng HĐĐT / thông báo phát hành hóa đơn giấy (`docs/srs/08`).

Hồ sơ đăng ký trả lời đúng một câu hỏi cho phân hệ hóa đơn: **ký hiệu này được
dùng từ ngày nào, với dải số nào, ở chi nhánh nào**. Ba việc của tệp này đều
mọc ra từ câu ấy:

* `create` — lập hồ sơ, và chặn **chồng lấn dải số** (FR-INV-008).
* `activate` — đưa hồ sơ vào hiệu lực, và **khai dãy số** của ký hiệu ngay tại
  đó. Khai lúc kích hoạt chứ không lúc phát hành hóa đơn đầu tiên: `next_value`
  của hóa đơn đặt in phải là số đầu dải đã thông báo, mà lượt phát hành thì
  không biết dải nào — nó chỉ biết mình đang cần một số.
* `effective_for` — vế phải của BR-EIV-03, đường đọc mà `EInvoiceService.issue`
  gọi.

**Hai phép kiểm chạy NGOÀI RLS**, và chúng là lần thứ **năm** của mẫu "phép
kiểm chạy dưới phạm vi nào" (6C H-1, 3B-2 B-8, 6G-2 H-3, 7B guard ngưỡng nợ).
Cùng cách vá: hàm `SECURITY DEFINER` nhận tham số vô hướng, không nhận điều
kiện lọc tự do, không ghi.

* `invoice_form_branch_owner` — **một ký hiệu thuộc đúng một chi nhánh**
  (quyết định user 2026-09-07). Chủ cũ của ký hiệu thuộc chi nhánh mà người gọi
  cố ý không nhìn thấy, nên phép kiểm phải đứng ngoài RLS mới thấy được — đúng
  lý do nó tồn tại. Lý do nghiệp vụ đầy đủ ở `_refuse_other_branch`.
* `invoice_range_conflicts` — chồng lấn dải (FR-INV-008). Sau luật trên, nó chỉ
  còn so **trong cùng một chi nhánh**, tức những dòng người gọi vốn nhìn thấy.
  Vẫn giữ `SECURITY DEFINER` có chủ đích, làm lớp thứ hai: nếu một lát sau nới
  luật "một ký hiệu một chi nhánh" (chia một ký hiệu cho nhiều chi nhánh có
  dải riêng), phép kiểm này **không được** lặng lẽ mù đi cùng lúc. Hồ sơ giữ
  chỗ là hồ sơ đã nộp hoặc đang hiệu lực; bản nháp chưa xin gì, bản đã ngừng đã
  trả lại.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    InvoiceFormBranchConflictError,
    InvoiceFormNotUsableError,
    InvoiceRegistrationOverlapError,
    MasterDataNotFoundError,
    ReferenceNotFoundError,
)
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.numbering.service import NumberingService
from ket.modules.einvoice.models import InvoiceRegistration, RegistrationStatus
from ket.modules.einvoice.numbering import INVOICE_NUMBER_PADDING, scope_key_for


class InvoiceRegistrationService:
    """Đọc và ghi hồ sơ đăng ký, trong transaction của người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._numbering = NumberingService(session)

    def create(
        self,
        *,
        branch_id: int,
        invoice_form_id: int,
        start_date: date,
        notice_no: str | None = None,
        notice_date: date | None = None,
        quantity: int | None = None,
        range_from: int | None = None,
        range_to: int | None = None,
    ) -> InvoiceRegistration:
        """Lập một hồ sơ đăng ký ở trạng thái nháp."""
        self._lock_form(invoice_form_id)
        self._refuse_other_branch(invoice_form_id=invoice_form_id, branch_id=branch_id)
        if range_from is not None and range_to is not None:
            self._refuse_overlap(
                invoice_form_id=invoice_form_id, range_from=range_from, range_to=range_to
            )
        registration = InvoiceRegistration(
            branch_id=branch_id,
            invoice_form_id=invoice_form_id,
            start_date=start_date,
            notice_no=notice_no,
            notice_date=notice_date,
            quantity=quantity,
            range_from=range_from,
            range_to=range_to,
            status=RegistrationStatus.NHAP,
        )
        self._session.add(registration)
        self._session.flush()
        return registration

    def activate(self, registration_id: UUID) -> InvoiceRegistration:
        """Đưa hồ sơ vào hiệu lực và khai dãy số của ký hiệu nếu chưa có.

        Kiểm **lần nữa** ở đây chứ không tin lượt kiểm lúc `create`: giữa hai
        thời điểm ấy có thể có một hồ sơ khác của chi nhánh khác vừa được lập,
        và người lập nó cũng không thấy hồ sơ này (RLS). Lượt kiểm lúc tạo là
        để báo sớm cho người dùng; lượt kiểm ở đây mới là cổng.

        Và "cổng" chỉ đúng khi có **khóa** (review 7D H-2): hai lượt kích hoạt
        song song đều đọc trước khi bên kia ghi, nên cả hai cùng thấy "không
        đụng ai" và hai dải chồng nhau cùng vào hiệu lực. `_lock_form` nối tiếp
        chúng theo ký hiệu — cùng khuôn `FOR UPDATE` mà `NumberingService` dùng
        để hai người không cùng lấy một số.
        """
        registration = self.require(registration_id)
        if registration.status is RegistrationStatus.HIEU_LUC:
            return registration
        self._lock_form(registration.invoice_form_id)
        self._refuse_other_branch(
            invoice_form_id=registration.invoice_form_id, branch_id=registration.branch_id
        )
        if registration.range_from is not None and registration.range_to is not None:
            self._refuse_overlap(
                invoice_form_id=registration.invoice_form_id,
                range_from=registration.range_from,
                range_to=registration.range_to,
                exclude_id=registration.id,
            )
        registration.status = RegistrationStatus.HIEU_LUC
        self._ensure_sequence(registration)
        self._session.flush()
        return registration

    def effective_for(
        self, *, invoice_form_id: int, branch_id: int, on_date: date
    ) -> InvoiceRegistration | None:
        """Hồ sơ còn hiệu lực cho phép phát hành hóa đơn ngày `on_date` — BR-EIV-03.

        Trả hồ sơ có `start_date` **muộn nhất** trong số các hồ sơ đã bắt đầu:
        một ký hiệu được thông báo phát hành nhiều lần (thêm dải, đổi thông
        tin), và hồ sơ mới nhất là hồ sơ đang mô tả tình trạng hiện tại — dải
        số phải đọc từ nó chứ không từ hồ sơ đầu tiên còn sót lại.
        """
        return self._session.scalars(
            select(InvoiceRegistration)
            .where(
                InvoiceRegistration.invoice_form_id == invoice_form_id,
                InvoiceRegistration.branch_id == branch_id,
                InvoiceRegistration.status == RegistrationStatus.HIEU_LUC,
                InvoiceRegistration.start_date <= on_date,
            )
            .order_by(InvoiceRegistration.start_date.desc())
            .limit(1)
        ).first()

    def covers_number(
        self, *, invoice_form_id: int, branch_id: int, on_date: date, number: int
    ) -> bool:
        """Số vừa cấp có nằm trong dải nào **còn hiệu lực** của ký hiệu này không?

        Hỏi "có hồ sơ nào phủ số này" chứ không "hồ sơ mới nhất có phủ không":
        một ký hiệu được thông báo phát hành nhiều lần, và những dải cũ **vẫn
        còn giá trị**. Doanh nghiệp thông báo `1–500` từ tháng 1 rồi `501–600`
        từ tháng 6; tháng 7 bộ đếm mới tới 250 thì tờ hóa đơn số `00000250` là
        hợp lệ — nó thuộc thông báo thứ nhất. Đọc dải từ hồ sơ mới nhất sẽ từ
        chối đúng tờ ấy.

        Hồ sơ **không khai dải** (hóa đơn điện tử) nghĩa là không giới hạn, nên
        chỉ cần một hồ sơ như vậy là mọi số đều được.
        """
        rows = self._session.execute(
            select(InvoiceRegistration.range_from, InvoiceRegistration.range_to).where(
                InvoiceRegistration.invoice_form_id == invoice_form_id,
                InvoiceRegistration.branch_id == branch_id,
                InvoiceRegistration.status == RegistrationStatus.HIEU_LUC,
                InvoiceRegistration.start_date <= on_date,
            )
        ).all()
        return any(low is None or high is None or low <= number <= high for low, high in rows)

    def require(self, registration_id: UUID) -> InvoiceRegistration:
        registration = self._session.get(InvoiceRegistration, registration_id)
        if registration is None:
            raise ReferenceNotFoundError(
                "Không tìm thấy hồ sơ đăng ký hóa đơn",
                entity_type="invoice_registrations",
                entity_id=str(registration_id),
            )
        return registration

    def _lock_form(self, invoice_form_id: int) -> InvoiceForm:
        """Đọc ký hiệu và **giữ khóa ghi** tới cuối transaction của người gọi.

        Danh mục mẫu số là dòng duy nhất mà cả hai lượt đăng ký cùng chạm tới,
        nên nó là chỗ tự nhiên để nối tiếp chúng. Khóa ở đây, không ở
        `invoice_registrations`: hai hồ sơ của hai chi nhánh là **hai dòng khác
        nhau**, và khóa hai dòng khác nhau thì không ai chờ ai.
        """
        form = self._session.scalar(
            select(InvoiceForm).where(InvoiceForm.id == invoice_form_id).with_for_update()
        )
        return self._verify_usable(form, invoice_form_id)

    def _refuse_other_branch(self, *, invoice_form_id: int, branch_id: int) -> None:
        """**Một ký hiệu thuộc đúng một chi nhánh** (quyết định user 2026-09-07, C-1).

        Đây là thứ giữ cho dãy số và dải số cùng một trục phạm vi. Dãy số phân
        theo (mẫu số, ký hiệu) — đúng chữ BR-EIV-02 — còn dải số đăng ký thì
        phân theo chi nhánh (FR-INV-008). Hai trục khác nhau cho cùng một con
        số nghĩa là: chi nhánh A đăng ký `1–500`, chi nhánh B đăng ký `501–600`
        trên cùng ký hiệu, bộ đếm dùng chung bắt đầu ở 1, và **B phát hành ra
        `00000001`** — một số thuộc dải đã thông báo của A. Hóa đơn của B nằm
        ngoài dải B đăng ký (không hợp lệ về thuế) và dải của A bị B tiêu mất.

        Thêm một phép kiểm cận dưới không cứu được: B sẽ bị chặn ngay từ tờ đầu
        và không bao giờ phát hành được, vì một bộ đếm dùng chung không có cách
        nào nhảy tới 501 cho riêng B.

        Buộc một ký hiệu về một chi nhánh làm "dãy theo ký hiệu" **chính là**
        "dãy theo chi nhánh", nên mâu thuẫn biến mất mà không phải đụng vào khóa
        phạm vi. Khớp cách TT78 vận hành: ký hiệu vốn mã hóa đơn vị phát hành.
        Cái giá đã biết: mỗi chi nhánh phải khai ký hiệu riêng của mình.

        Chạy **ngoài RLS** (`invoice_form_branch_owner`, `SECURITY DEFINER`) vì
        chủ cũ của ký hiệu thuộc chi nhánh mà người gọi không nhìn thấy — chính
        điều làm phép kiểm này cần thiết.
        """
        owner = self._session.execute(
            text(
                "SELECT branch_id FROM invoice_form_branch_owner("
                "    CAST(:form_id AS integer), CAST(:branch_id AS integer)) LIMIT 1"
            ),
            {"form_id": invoice_form_id, "branch_id": branch_id},
        ).first()
        if owner is None:
            return
        raise InvoiceFormBranchConflictError(
            "Ký hiệu hóa đơn này đã được một chi nhánh khác đăng ký sử dụng — "
            "mỗi ký hiệu thuộc đúng một chi nhánh, hãy khai một ký hiệu riêng "
            "cho chi nhánh này",
            invoice_form_id=invoice_form_id,
        )

    def _require_usable_form(self, invoice_form_id: int) -> InvoiceForm:
        return self._verify_usable(self._session.get(InvoiceForm, invoice_form_id), invoice_form_id)

    def _verify_usable(self, form: InvoiceForm | None, invoice_form_id: int) -> InvoiceForm:
        if form is None:
            raise MasterDataNotFoundError(
                "Không tìm thấy ký hiệu hóa đơn",
                entity_type="invoice_forms",
                entity_id=invoice_form_id,
            )
        if form.is_group:
            raise InvoiceFormNotUsableError(
                "Đây là nút nhóm trong danh mục mẫu số, không phải một ký hiệu hóa đơn"
            )
        if not form.is_active:
            raise InvoiceFormNotUsableError("Ký hiệu hóa đơn này đã ngừng theo dõi")
        return form

    def _refuse_overlap(
        self,
        *,
        invoice_form_id: int,
        range_from: int,
        range_to: int,
        exclude_id: UUID | None = None,
    ) -> None:
        """FR-INV-008 — xem docstring đầu tệp về việc phải chạy ngoài RLS."""
        conflict = self._session.execute(
            text(
                "SELECT range_from, range_to"
                " FROM invoice_range_conflicts("
                "     CAST(:form_id AS integer), CAST(:range_from AS bigint),"
                "     CAST(:range_to AS bigint), CAST(:exclude_id AS uuid))"
                " LIMIT 1"
            ),
            {
                "form_id": invoice_form_id,
                "range_from": range_from,
                "range_to": range_to,
                "exclude_id": exclude_id,
            },
        ).first()
        if conflict is None:
            return
        # `details` mang **dải**, không mang `notice_no` (review 7D M-4): số
        # thông báo là dữ liệu của một chi nhánh khác — chi nhánh mà người gọi
        # cố ý không nhìn thấy được (RLS) — và đưa nó vào thân lỗi là để hàm
        # `SECURITY DEFINER` rò ra đúng thứ RLS đang che. Dải số thì phải nói
        # ra: không có nó người dùng không biết xin dải nào cho khỏi đụng.
        raise InvoiceRegistrationOverlapError(
            "Dải số này chồng lấn một thông báo phát hành đã có của cùng ký hiệu",
            range_from=conflict.range_from,
            range_to=conflict.range_to,
        )

    def _ensure_sequence(self, registration: InvoiceRegistration) -> None:
        """Khai dãy số của ký hiệu nếu chưa có; không đụng dãy đã chạy.

        **Không** dời `next_value` khi hồ sơ thứ hai của cùng ký hiệu được kích
        hoạt: dãy đang chạy tới `00000123` mà một hồ sơ "thêm dải `1–1000`" kéo
        nó về 1 là cấp lại đúng những số đã in ra. Dải mới chỉ nới **trần**, và
        trần được canh ở lượt cấp số (`EInvoiceService._refuse_out_of_range`).
        """
        form = self._session.get(InvoiceForm, registration.invoice_form_id)
        if form is None or form.form_no is None:
            raise MasterDataNotFoundError(
                "Không tìm thấy ký hiệu hóa đơn",
                entity_type="invoice_forms",
                entity_id=registration.invoice_form_id,
            )
        scope_key = scope_key_for(form_no=form.form_no, serial=form.code)
        if self._numbering.peek(scope_key) is not None:
            return
        self._numbering.define_by_scope_key(
            scope_key,
            document_type="EIV",
            padding=INVOICE_NUMBER_PADDING,
            allow_gaps=False,
            start_value=registration.range_from if registration.range_from is not None else 1,
        )

    def list_for_form(self, *, invoice_form_id: int) -> Sequence[InvoiceRegistration]:
        """Hồ sơ của một ký hiệu **trong phạm vi chi nhánh của người gọi** — RLS lọc."""
        return self._session.scalars(
            select(InvoiceRegistration)
            .where(InvoiceRegistration.invoice_form_id == invoice_form_id)
            .order_by(InvoiceRegistration.start_date)
        ).all()
