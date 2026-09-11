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

**Thi hành thì chỉ hai trong bốn** (`EXECUTABLE_REMEDIES`, quyết định user
2026-09-10) — và ranh giới ấy là **cấu trúc**, không phải việc chưa kịp làm: xem
docstring của `EXECUTABLE_REMEDIES`. `resolve` vẫn trả về đúng cách xử lý cho cả
bốn, nên wizard nói được câu đúng ngay cả ở nhánh chưa thi hành được.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import EInvoiceErrorFlowUnknownError, EInvoiceRemedyNotAvailableError
from ket.modules.einvoice.models import (
    EXECUTABLE_REMEDIES,
    EInvoice,
    EInvoiceErrorFlow,
    EInvoiceErrorNotice,
    ErrorKind,
    ErrorNoticeKind,
    Remedy,
)
from ket.modules.einvoice.service import EInvoiceService


@dataclass(frozen=True, slots=True)
class ErrorFlowOutcome:
    """Kết quả một lượt xử lý sai sót."""

    flow: EInvoiceErrorFlow
    notice: EInvoiceErrorNotice
    """Thông báo sai sót gửi cơ quan thuế (Mẫu 04/SS) — lập ở **mọi** cách xử lý."""
    replacement: EInvoice | None
    """Hóa đơn thay thế vừa dựng, chỉ có ở `THAY_THE`."""


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
    ) -> ErrorFlowOutcome:
        """Tra bảng rồi thi hành — thay thế hoặc hủy (FR-EIV-030/031/032).

        Thông báo sai sót lập ở **mọi** nhánh thi hành được, và lập **sau** khi
        cạnh trạng thái đã đi qua: máy trạng thái là thứ từ chối một tờ hóa đơn
        chưa phát hành, nên lập văn bản trước sẽ để lại một thông báo mồ côi kèm
        một hóa đơn không đổi trạng thái.
        """
        flow = self.resolve(error_kind=error_kind, buyer_declared=buyer_declared)
        remedy = Remedy(flow.remedy)
        if remedy not in EXECUTABLE_REMEDIES:
            raise EInvoiceRemedyNotAvailableError(
                "Cách xử lý đúng cho hóa đơn này là lập hóa đơn điều chỉnh, và "
                "đường lập nó cần một chứng từ bán mang phần chênh — chưa có ở "
                "bản này",
                einvoice_id=str(einvoice_id),
                flow_code=flow.code,
                remedy=int(remedy),
            )

        replacement = self._replace(einvoice_id) if remedy is Remedy.THAY_THE else None
        if remedy is Remedy.HUY:
            self._invoices.cancel(einvoice_id)

        notice = self._invoices.add_notice(
            einvoice_id,
            kind=ErrorNoticeKind.THONG_BAO_SAI_SOT,
            notice_no=notice_no,
            notice_date=notice_date,
            reason_code=flow.code,
            reason=reason,
        )
        return ErrorFlowOutcome(flow=flow, notice=notice, replacement=replacement)

    def _replace(self, einvoice_id: UUID) -> EInvoice:
        """Chuyển tờ cũ sang `DA_THAY_THE` và dựng tờ nháp mới trên **cùng chứng từ**.

        Cùng chứng từ chứ không chứng từ mới: chỉ mục riêng phần
        `uq_einvoices_live_source_voucher` của 7D loại đúng ba trạng thái cuối ra
        khỏi luật "một chứng từ một hóa đơn còn hiệu lực" — tức hình dạng này là
        thứ schema đã chừa sẵn chỗ. Tờ cũ phải đi trước: chừng nào nó còn ở
        `DA_PHAT_HANH` thì chỉ mục ấy vẫn chặn tờ thứ hai.

        Chứng từ gốc mở ra ngay sau lượt này (`SUPERSEDED_STATUSES`), nên kế toán
        sửa lại số tiền / thông tin rồi phát hành tờ mới.
        """
        superseded = self._invoices.mark_replaced(einvoice_id)
        replacement = self._invoices.create_draft(
            source_voucher_id=superseded.source_voucher_id,
            invoice_form_id=superseded.invoice_form_id,
        )
        replacement.replaces_invoice_id = superseded.id
        self._session.flush()
        return replacement
