"""Ranh giới công khai của `ket.modules.einvoice`.

Mọi thứ module khác được phép chạm phải khai ở đây (Protocol / Pydantic model
/ dataclass có kiểu). Cấm `dict[str, Any]` qua ranh giới module — ADR-015.
Đây cũng là đích neo cho `import-linter` (ADR-004).

**Cố ý gần như rỗng, và sẽ còn rỗng.** Luật C3 cấm module import module, nên
không phân hệ nào gọi thẳng vào đây; hai chiều quan hệ mà hóa đơn điện tử có
với phần còn lại đều đi qua tầng dùng chung:

* chiều "loại chứng từ nào xuất được hóa đơn" — cờ
  `PostingDocumentType.invoiceable` của `ket.posting`;
* chiều "chứng từ này đang bị hóa đơn giữ lại" — `EDIT_GUARDS` và
  `REFERENCE_GUARDS`, cũng của `ket.posting`.

Thứ khai ở đây là bộ **kiểu trạng thái**: chúng đi vào phản hồi API, ra tới
type TypeScript sinh cho máy khách, và lát 7E/7F đọc chúng từ đây thay vì moi
vào `models`.
"""

from __future__ import annotations

from ket.modules.einvoice.models import (
    EInvoiceStatus,
    ErrorNoticeKind,
    NoticeStatus,
    RegistrationStatus,
)
from ket.modules.einvoice.state_machine import EInvoiceAction

__all__ = [
    "EInvoiceAction",
    "EInvoiceStatus",
    "ErrorNoticeKind",
    "NoticeStatus",
    "RegistrationStatus",
]
