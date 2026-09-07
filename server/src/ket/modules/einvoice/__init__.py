"""Hóa đơn điện tử + quản lý hóa đơn (SRS 07, 08).

Mã FR: EIV, INV · Phase sở hữu: 7 (lát 7D dựng nền, 7E phát hành, 7F sai sót).

Import gói này (qua `ket.model_registry`) là đăng ký ba thứ:

* **hai mã quyền**, `einvoice.invoice.*` và `einvoice.registration.*`. Tách khỏi
  quyền kế toán đúng như FR-EIV-007 đòi: người lập chứng từ bán hàng không nghiễm
  nhiên phát hành được hóa đơn đỏ, và ngược lại. Cả hai bật
  `requires_second_factor` — phát hành hóa đơn là hành vi có hệ quả pháp lý
  ngoài phần mềm, và `permissions.DocumentType` viết sẵn từ phase 2 rằng
  `einvoice` là ca nó chờ.
* **một `EDIT_GUARDS`** — FR-EIV-035 chiều sửa/xóa chứng từ nháp.
* **một `REFERENCE_GUARDS`** — FR-EIV-035 chiều bỏ ghi sổ.

Module này **không** đăng ký loại chứng từ posting: hóa đơn điện tử không phải
một chứng từ ghi sổ. Nó không sinh bút toán nào — bút toán doanh thu đã nằm ở
chứng từ bán hàng gốc, và một tờ hóa đơn ghi sổ lần nữa là ghi đúp doanh thu.
Quan hệ đi chiều ngược lại: hóa đơn **trỏ** về chứng từ, và giữ chứng từ ấy
đứng yên.

Chiều "loại chứng từ nào xuất được hóa đơn" cũng không nằm ở đây mà là cờ
`PostingDocumentType.invoiceable` của tầng `posting` — xem docstring của nó về
lý do (luật C3, và danh sách ấy trải qua ba phân hệ).
"""

from __future__ import annotations

from ket.kernel.security.permissions import (
    CATALOG_ACTIONS,
    Action,
    DocumentType,
)
from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.modules.einvoice.guards import refuse_when_invoice_issued
from ket.posting.contracts import EDIT_GUARDS, REFERENCE_GUARDS

EINVOICE_PERMISSION_MODULE = "einvoice"
INVOICE_PERMISSION_CODE = "invoice"
REGISTRATION_PERMISSION_CODE = "registration"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=EINVOICE_PERMISSION_MODULE,
        code=INVOICE_PERMISSION_CODE,
        # Không `post`/`unpost`: hóa đơn không ghi sổ (xem docstring đầu tệp).
        # `create` = lập hóa đơn, `edit` = phát hành / xác nhận / hủy — chúng
        # đổi trạng thái chứ không đổi nội dung, và nội dung thì BR-EIV-01 cấm
        # đổi hẳn. `print` = xem trước và tải bản thể hiện (FR-EIV-016/026).
        actions=frozenset(
            {
                Action.VIEW,
                Action.CREATE,
                Action.EDIT,
                Action.DELETE,
                Action.PRINT,
                Action.EXPORT,
            }
        ),
        requires_second_factor=True,
    )
)

PERMISSION_REGISTRY.register(
    DocumentType(
        module=EINVOICE_PERMISSION_MODULE,
        code=REGISTRATION_PERMISSION_CODE,
        actions=CATALOG_ACTIONS,
        # Hồ sơ đăng ký quyết định dải số nào thuộc chi nhánh nào; sửa được nó
        # là cấp được cho mình một dải số hóa đơn. Cùng mức rủi ro với quyền
        # phát hành, nên cùng yêu cầu lớp thứ hai.
        requires_second_factor=True,
    )
)

EDIT_GUARDS.register(refuse_when_invoice_issued)
REFERENCE_GUARDS.register(refuse_when_invoice_issued)
