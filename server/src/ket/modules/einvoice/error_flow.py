"""Luồng hỏi–đáp xử lý sai sót hóa đơn (`docs/srs/07` §4.4, FR-EIV-030..034).

Kế toán trả lời hai câu hỏi, hệ thống **tra bảng** ra cách xử lý rồi thi hành nó:

```
"Hóa đơn sai chỗ nào?"
  ├─ Sai thông tin không đổi tiền      → "Khách đã kê khai chưa?"
  │                                       chưa: THAY THẾ · rồi: ĐIỀU CHỈNH THÔNG TIN
  ├─ Sai số tiền / số lượng / thuế suất → "Khách đã kê khai chưa?"
  │                                       chưa: THAY THẾ · rồi: ĐIỀU CHỈNH TĂNG/GIẢM
  └─ Không phát sinh giao dịch          → HỦY
```

**Tra bảng, không `if/elif`** — bảng nằm ở `einvoice_error_flows` (FR-NFR-055,
xem `models.EInvoiceErrorFlow`). Hệ quả đáng nói: câu hỏi thứ hai cũng đọc từ
bảng chứ không mang sẵn "có đúng hai câu hỏi" trong mã. Nhánh nào khai
`buyer_declared IS NULL` thì dừng sau câu một, và một nhánh thứ tư thêm vào bảng
sau này không phải sửa tệp này.

**Cả bốn cách xử lý đều thi hành được.** Ba cách dựng tờ nháp mới ngay trên
chứng từ gốc — thay thế và điều chỉnh THÔNG TIN, vì cả hai giữ nguyên số tiền —
còn hủy thì không dựng gì. Cách thứ tư, điều chỉnh TIỀN, đòi người gọi đưa kèm
một **chứng từ bán mang phần chênh** (`DELTA_VOUCHER_REMEDIES`): hóa đơn đọc
tổng từ chứng từ gốc, nên tờ khai phần chênh phải có chứng từ của riêng nó.

**Ba nhánh dựng tờ mới đi chung một hàm** (`_supersede`) chứ không ba bản chép:
chúng khác nhau đúng ba thứ — cạnh trạng thái nào, tờ mới đứng trên chứng từ
nào, và cột nào trỏ ngược về tờ cũ — và một hàm nhận ba thứ ấy là chỗ duy nhất
để thứ tự "tờ cũ đi trước" đúng cho cả ba (xem `service.mark_replaced` về vì
sao thứ tự ấy là bất biến chứ không sở thích).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    EInvoiceAdjustmentVoucherInvalidError,
    EInvoiceAdjustmentVoucherRequiredError,
    EInvoiceErrorFlowUnknownError,
)
from ket.kernel.protocols import PROVIDERS as CROSS_MODULE
from ket.kernel.protocols import EInvoiceSourceDocument
from ket.modules.einvoice.models import (
    DELTA_VOUCHER_REMEDIES,
    EInvoice,
    EInvoiceErrorFlow,
    EInvoiceErrorNotice,
    ErrorKind,
    ErrorNoticeKind,
    Remedy,
)
from ket.modules.einvoice.service import EInvoiceService
from ket.posting.documents.models import Voucher, VoucherStatus


@dataclass(frozen=True, slots=True)
class ErrorFlowOutcome:
    """Kết quả một lượt xử lý sai sót."""

    flow: EInvoiceErrorFlow
    notice: EInvoiceErrorNotice
    """Thông báo sai sót gửi cơ quan thuế (Mẫu 04/SS) — lập ở **mọi** cách xử lý."""
    replacement: EInvoice | None
    """Hóa đơn thay thế vừa dựng, chỉ có ở `THAY_THE`."""
    adjustment: EInvoice | None
    """Hóa đơn điều chỉnh vừa dựng, chỉ có ở hai nhánh `DIEU_CHINH_*`.

    Trường riêng chứ không dùng chung `replacement`: hai tờ khác nhau về pháp
    lý — tờ thay thế **đứng thay** tờ cũ với đủ số tiền, tờ điều chỉnh **đứng
    cạnh** nó mang phần chênh — và một trường duy nhất buộc nơi đọc phải quay
    lại nhìn `remedy` mới biết mình đang cầm tờ nào."""


class ErrorFlowService:
    """Tra bảng quyết định và thi hành cách xử lý, trong transaction của người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._invoices = EInvoiceService(session)

    def table(self) -> list[EInvoiceErrorFlow]:
        """Cả bảng quyết định còn hiệu lực — wizard dựng câu hỏi từ đây."""
        return list(
            self._session.scalars(
                select(EInvoiceErrorFlow)
                .where(EInvoiceErrorFlow.is_active)
                .order_by(EInvoiceErrorFlow.error_kind, EInvoiceErrorFlow.buyer_declared)
            )
        )

    def asks_whether_buyer_declared(self, error_kind: ErrorKind) -> bool:
        """Loại sai sót này có phải hỏi tiếp câu thứ hai không.

        Đọc từ bảng: còn nhánh nào khai `buyer_declared` thì còn phải hỏi. Không
        viết cứng "hai loại đầu thì hỏi" — đó lại là bảng quyết định chép lần thứ
        hai vào mã, tức chỗ để hai bản lệch nhau.
        """
        return any(
            flow.buyer_declared is not None
            for flow in self._session.scalars(
                select(EInvoiceErrorFlow).where(
                    EInvoiceErrorFlow.error_kind == error_kind,
                    EInvoiceErrorFlow.is_active,
                )
            )
        )

    def resolve(self, *, error_kind: ErrorKind, buyer_declared: bool | None) -> EInvoiceErrorFlow:
        """Cách xử lý cho một bộ câu trả lời (FR-EIV-030/033/034).

        Bộ câu trả lời không có trong bảng → lỗi nghiệp vụ mang cả hai vế, chứ
        không im lặng chọn bừa một nhánh: một wizard hỏi thiếu câu thứ hai phải
        **đỏ**, không được rơi vào nhánh "chưa kê khai" chỉ vì nó đứng trước.
        """
        flow = self._session.scalars(
            select(EInvoiceErrorFlow)
            .where(
                EInvoiceErrorFlow.error_kind == error_kind,
                EInvoiceErrorFlow.buyer_declared.is_(buyer_declared),
                EInvoiceErrorFlow.is_active,
            )
            .limit(1)
        ).first()
        if flow is None:
            raise EInvoiceErrorFlowUnknownError(
                "Bộ câu trả lời này không khớp nhánh xử lý sai sót nào",
                error_kind=int(error_kind),
                buyer_declared=buyer_declared,
            )
        return flow

    def apply(
        self,
        einvoice_id: UUID,
        *,
        error_kind: ErrorKind,
        buyer_declared: bool | None,
        notice_no: str,
        notice_date: date,
        reason: str | None = None,
        adjustment_voucher_id: UUID | None = None,
    ) -> ErrorFlowOutcome:
        """Tra bảng rồi thi hành (FR-EIV-030..034).

        Thông báo sai sót lập ở **mọi** nhánh, và lập **sau** khi cạnh trạng
        thái đã đi qua: máy trạng thái là thứ từ chối một tờ hóa đơn chưa phát
        hành, nên lập văn bản trước sẽ để lại một thông báo mồ côi kèm một hóa
        đơn không đổi trạng thái.

        `adjustment_voucher_id` kiểm **trước** mọi phép ghi, và kiểm cả chiều
        thừa lẫn chiều thiếu — xem `_require_delta_voucher`.
        """
        flow = self.resolve(error_kind=error_kind, buyer_declared=buyer_declared)
        remedy = self._remedy_of(flow)
        delta_voucher_id = self._require_delta_voucher(
            einvoice_id, remedy=remedy, adjustment_voucher_id=adjustment_voucher_id
        )

        replacement: EInvoice | None = None
        adjustment: EInvoice | None = None
        if remedy is Remedy.THAY_THE:
            replacement = self._supersede(
                einvoice_id,
                step=self._invoices.mark_replaced,
                link="replaces_invoice_id",
            )
        elif remedy is Remedy.DIEU_CHINH_THONG_TIN:
            adjustment = self._supersede(
                einvoice_id,
                step=self._invoices.mark_adjusted,
                link="adjusts_invoice_id",
            )
        elif remedy is Remedy.DIEU_CHINH_TIEN:
            adjustment = self._supersede(
                einvoice_id,
                step=self._invoices.mark_adjusted,
                link="adjusts_invoice_id",
                source_voucher_id=delta_voucher_id,
            )
        elif remedy is Remedy.HUY:
            self._invoices.cancel(einvoice_id)
        else:  # pragma: no cover - lưới cho một thành viên `Remedy` thêm về sau
            # Không nhánh mặc định, cùng doctrine với `TRANSITIONS`. Giá trị lạ
            # từ **dữ liệu** đã chết sớm hơn, ở `_remedy_of`; cái lưới này canh
            # chiều còn lại — một thành viên `Remedy` thêm vào enum mà quên cài
            # đường thi hành. Rơi vào nhánh hủy khi ấy là **tiêu một số hóa đơn**
            # không lấy lại được (ADR-013) cho một cách xử lý chưa ai viết.
            raise EInvoiceErrorFlowUnknownError(
                "Cách xử lý này chưa có đường thi hành",
                flow_code=flow.code,
                remedy=int(remedy),
            )

        notice = self._invoices.add_notice(
            einvoice_id,
            kind=ErrorNoticeKind.THONG_BAO_SAI_SOT,
            notice_no=notice_no,
            notice_date=notice_date,
            reason_code=flow.code,
            reason=reason,
        )
        return ErrorFlowOutcome(
            flow=flow, notice=notice, replacement=replacement, adjustment=adjustment
        )

    def _remedy_of(self, flow: EInvoiceErrorFlow) -> Remedy:
        """`flow.remedy` → `Remedy`, và giá trị lạ là **lỗi nghiệp vụ, không 500**.

        Bảng quyết định là dữ liệu sửa được lúc chạy (FR-NFR-055), nên một con số
        ngoài enum vào được bảng bằng một lượt `UPDATE` chứ không cần ai sửa mã.
        `Remedy(...)` trần ném `ValueError`, thứ đi thẳng thành "lỗi không mong
        muốn, gọi bộ phận hỗ trợ" — câu vô dụng cho người vừa gõ sai một dòng
        cấu hình. `remedy_known` của `0036` canh chiều `INSERT`; đây là chiều
        đọc, cho những dòng đã lọt vào trước khi có ràng buộc ấy.
        """
        try:
            return Remedy(flow.remedy)
        except ValueError as error:
            raise EInvoiceErrorFlowUnknownError(
                "Nhánh xử lý sai sót khai một cách xử lý không có thật",
                flow_code=flow.code,
                remedy=flow.remedy,
            ) from error

    def _supersede(
        self,
        einvoice_id: UUID,
        *,
        step: Callable[[UUID], EInvoice],
        link: Literal["replaces_invoice_id", "adjusts_invoice_id"],
        source_voucher_id: UUID | None = None,
    ) -> EInvoice:
        """Đưa tờ cũ ra khỏi hiệu lực rồi dựng tờ nháp mới trỏ ngược về nó.

        `source_voucher_id` bỏ trống nghĩa là tờ mới đứng trên **chính chứng từ
        cũ** — đúng cho thay thế và cho điều chỉnh THÔNG TIN, vì cả hai giữ
        nguyên số tiền. Chỉ mục riêng phần `uq_einvoices_live_source_voucher`
        của 7D loại đúng ba trạng thái cuối ra khỏi luật "một chứng từ một hóa
        đơn còn hiệu lực", tức hình dạng này là thứ schema đã chừa sẵn chỗ.

        Tờ cũ phải đi trước — `step` chạy trước `create_draft` — và đó là bất
        biến chứ không thứ tự tùy ý: chừng nào nó còn ở `DA_PHAT_HANH` thì chỉ
        mục ấy vẫn chặn tờ thứ hai trên cùng chứng từ.

        **Chuỗi không vòng được, không cần phép kiểm.** Tờ mới luôn vừa dựng
        xong nên chưa ai trỏ vào nó, còn tờ cũ vừa rơi vào `SUPERSEDED_STATUSES`
        — ba trạng thái không có cạnh ra — nên nó không bao giờ là đầu của một
        lượt sau. Nợ 7D ghi ở `models.replaces_invoice_id` đóng bằng cấu trúc,
        và `test_the_supersede_chain_cannot_close_on_itself` ghim lập luận ấy.

        Chứng từ gốc mở ra ngay sau lượt này (`SUPERSEDED_STATUSES`), nên kế
        toán sửa lại rồi phát hành tờ mới.
        """
        superseded = step(einvoice_id)
        successor = self._invoices.create_draft(
            source_voucher_id=source_voucher_id or superseded.source_voucher_id,
            invoice_form_id=superseded.invoice_form_id,
        )
        # `Literal` chứ không `str`: `setattr` với một tên gõ sai **không** đỏ —
        # nó gán một thuộc tính Python thường, không phải cột, nên tờ mới lặng lẽ
        # mất đường trỏ ngược về tờ cũ (FR-EIV-036). Kiểu đóng biến nó thành lỗi
        # lúc kiểm kiểu, tại nơi gọi.
        setattr(successor, link, superseded.id)
        self._session.flush()
        return successor

    def _require_delta_voucher(
        self, einvoice_id: UUID, *, remedy: Remedy, adjustment_voucher_id: UUID | None
    ) -> UUID | None:
        """Kiểm chứng từ mang phần chênh — **cả chiều thừa lẫn chiều thiếu**.

        Chiều thừa đáng kiểm ngang chiều thiếu: một chứng từ bán đưa kèm ở nhánh
        hủy hay nhánh thay thế sẽ bị **bỏ qua im lặng**, và thứ nó để lại là một
        khoản doanh thu đã ghi sổ mà không tờ hóa đơn nào khai — lỗ đúng kiểu
        chỉ lộ ra ở lượt đối chiếu hóa đơn ↔ doanh thu sổ cái.
        """
        if remedy not in DELTA_VOUCHER_REMEDIES:
            if adjustment_voucher_id is not None:
                raise EInvoiceAdjustmentVoucherRequiredError(
                    "Cách xử lý này không lập hóa đơn từ chứng từ chênh lệch",
                    einvoice_id=str(einvoice_id),
                    remedy=int(remedy),
                    adjustment_voucher_id=str(adjustment_voucher_id),
                )
            return None
        if adjustment_voucher_id is None:
            raise EInvoiceAdjustmentVoucherRequiredError(
                "Hóa đơn điều chỉnh tăng/giảm cần một chứng từ bán mang phần chênh",
                einvoice_id=str(einvoice_id),
                remedy=int(remedy),
            )
        self._verify_delta_voucher(einvoice_id, adjustment_voucher_id)
        return adjustment_voucher_id

    def _verify_delta_voucher(self, einvoice_id: UUID, voucher_id: UUID) -> None:
        """Sáu điều kiện của `EInvoiceAdjustmentVoucherInvalidError`.

        Khách hàng và đồng tiền đọc qua Protocol `EInvoiceSource` chứ không bằng
        một lượt `SELECT` vào bảng của `sales`: C3 cấm `einvoice` import phân hệ
        khác, và cấm đúng — cửa ấy đã tồn tại từ ADR-022 cho đúng câu hỏi này.

        **Điều kiện "đã ghi sổ" là điều kiện LÚC LẬP, không phải một bảo đảm
        vĩnh viễn.** Hóa đơn đọc tổng từ chứng từ nó trỏ vào **tại lúc phát
        hành**, nên BR-EIV-07 đúng dù chứng từ còn sửa — điều mà lượt kiểm này
        canh là khác: thông báo 04/SS gửi cơ quan thuế ngay trong lượt `apply`,
        và khai một khoản chênh lệch chưa hề lên sổ là khai một nghiệp vụ chưa
        xảy ra. Sau lượt này chứng từ chênh lệch vẫn bỏ ghi sổ và sửa được chừng
        nào tờ điều chỉnh còn ở `CHUA_PHAT_HANH` — đúng như mọi chứng từ bán
        khác mang một tờ hóa đơn nháp, và `refuse_when_invoice_issued` khóa nó
        lại từ lúc phát hành.

        **Cả sáu điều kiện đều là điều kiện lúc lập**, cùng một luật: đường sửa
        chứng từ còn mở tới lúc phát hành, nên `adjusts_voucher_id`, chi nhánh,
        đồng tiền và khách hàng của chứng từ chênh lệch cũng đổi được trong
        khoảng ấy. Không kiểm lại ở `issue` là có chủ đích — bằng chứng gửi cơ
        quan thuế là bản XML đã lưu trữ, và nó chụp đúng trạng thái lúc phát
        hành chứ không lúc lập.

        **Điều kiện nặng nhất là điều kiện cuối**: chứng từ chênh lệch phải là
        chứng từ ĐIỀU CHỈNH **của đúng chứng từ gốc** của tờ hóa đơn này. Thiếu
        nó thì một hóa đơn bán *thường* — cùng khách, cùng đồng tiền, cùng chi
        nhánh, đã ghi sổ, chưa mang hóa đơn nào — cũng lọt, và hệ quả là một
        khoản doanh thu **thật** được khai với cơ quan thuế thành phần chênh của
        tờ khác, còn chính nó thì vĩnh viễn không xuất được hóa đơn
        (`uq_einvoices_live_source_voucher`). Bốn điều kiện trên đều không bắt
        được ca ấy.
        """
        invoice = self._invoices.require(einvoice_id)
        if voucher_id == invoice.source_voucher_id:
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch phải khác chứng từ gốc của hóa đơn bị điều chỉnh",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
            )
        voucher = self._session.get(Voucher, voucher_id)
        if voucher is None or voucher.status != VoucherStatus.DA_GHI_SO:
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch phải đã ghi sổ",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
            )
        # Cùng chi nhánh, và đây là điều kiện **chặn một ngõ cụt**, không phải
        # một phép kiểm phòng xa: `create_draft` lấy chi nhánh từ chứng từ chênh
        # lệch còn ký hiệu chép từ tờ cũ, mà một ký hiệu thuộc đúng một chi
        # nhánh (7D) — nên tờ điều chỉnh dựng xong sẽ đổ ở `_require_registration`
        # lúc phát hành, trong khi tờ gốc đã sang `DA_DIEU_CHINH` và 04/SS đã
        # lập. `DA_DIEU_CHINH` không có cạnh ra, nên ngõ cụt ấy là vĩnh viễn.
        # Khóa ngoại của `adjusts_invoice_id` là một cột nên nó **không** chặn
        # hộ, khác `(source_voucher_id, branch_id)` của chính bảng này.
        if voucher.branch_id != invoice.branch_id:
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch phải cùng chi nhánh với hóa đơn bị điều chỉnh",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
            )
        delta = self._read_source(voucher_id)
        original = self._read_source(invoice.source_voucher_id)
        if delta is None or original is None:
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch không xuất hóa đơn điện tử được",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
            )
        if (delta.partner_kind, delta.partner_id) != (original.partner_kind, original.partner_id):
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch phải cùng khách hàng với hóa đơn bị điều chỉnh",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
            )
        # Cùng đồng tiền: một tờ điều chỉnh khai phần chênh bằng USD cho một hóa
        # đơn lập bằng VND là hai con số không cộng trừ được với nhau, và cơ quan
        # thuế nhận về một khoản điều chỉnh không quy về đâu. Hai bản đã đọc sẵn
        # ở trên nên phép kiểm này không thêm lượt truy vấn nào.
        if delta.currency_code != original.currency_code:
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch phải cùng đồng tiền với hóa đơn bị điều chỉnh",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
            )
        # Và nó phải là chứng từ ĐIỀU CHỈNH của ĐÚNG chứng từ gốc này. Một phép
        # so chứ không hai: `adjusts_voucher_id IS NULL` nghĩa là "không phải
        # chứng từ điều chỉnh" (ràng buộc `adjustment_link_matches_kind` của
        # `sales` dựng nghĩa ấy), nên cùng lúc nó loại cả hóa đơn bán thường lẫn
        # chứng từ điều chỉnh lập cho một tờ hóa đơn khác.
        if delta.adjusts_voucher_id != invoice.source_voucher_id:
            raise EInvoiceAdjustmentVoucherInvalidError(
                "Chứng từ chênh lệch phải là chứng từ điều chỉnh của chính chứng từ gốc",
                einvoice_id=str(einvoice_id),
                voucher_id=str(voucher_id),
                adjusts_voucher_id=(
                    None if delta.adjusts_voucher_id is None else str(delta.adjusts_voucher_id)
                ),
            )

    def _read_source(self, voucher_id: UUID) -> EInvoiceSourceDocument | None:
        """Nội dung một chứng từ, hỏi lần lượt từng nguồn đã đăng ký (ADR-022).

        Cùng vòng lặp với `print_details._read_source`: mỗi bản cài trả lời về
        chứng từ **của chính nó** và trả `None` cho phần còn lại, nên "không
        nguồn nào nhận" là câu trả lời hợp lệ chứ không phải sai sót.
        """
        for source in CROSS_MODULE.einvoice_sources():
            document = source.read(self._session, voucher_id=voucher_id)
            if document is not None:
                return document
        return None
