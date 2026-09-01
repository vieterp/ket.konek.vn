"""Phân hệ Công nợ phải thu / phải trả — chủ sở hữu `ar_ap_ledger` (RT-18).

Module này **không có loại chứng từ nào của riêng nó**. Nó giữ một sổ phụ mà
hai phân hệ khác sinh dữ liệu vào (mua, bán) và hai phân hệ khác nữa tiêu thụ
(quỹ, ngân hàng — qua màn đối trừ). Vai trò của nó là làm chủ dữ liệu, nên nó
đăng ký đúng ba nhóm — **bốn** bản cài Protocol của kernel, cộng một guard và
một mã quyền:

* `ArApSubledger` — chiều ghi (ADR-021), `purchase`/`sales` gọi lúc ghi sổ.
* `ReceivableProvider` / `PayableProvider` + `SettlementTargetSource` — chiều
  đọc và chiều đối trừ, để phiếu thu/chi của phase 6 thấy hóa đơn phase 7 mà
  `cash_book` không đổi một dòng nào.
* Một `REFERENCE_GUARDS` — "khoản nợ đã được trả một phần thì chứng từ gốc
  không bỏ ghi sổ / không xóa được".

Guard đặt ở bộ **dùng chung** của posting chứ không ở hook riêng của loại
chứng từ, cùng lập luận với luật khớp sao kê (6G-2): luật thuộc về chủ bảng,
nhưng nó phải canh **mọi** cửa bỏ ghi sổ và xóa — kể cả cửa của một phân hệ
sau này sinh công nợ mà chưa ai nghĩ tới. Đăng ký từ phía chủ bảng giữ được cả
hai: mọi loại chứng từ được canh, mà `purchase`/`sales` không phải biết module
này tồn tại (luật C3).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.security.permissions import REGISTRY as PERMISSION_REGISTRY
from ket.kernel.security.permissions import Action, DocumentType
from ket.posting.contracts import REFERENCE_GUARDS

RECEIVABLES_PERMISSION_MODULE = "receivables"
LEDGER_PERMISSION_CODE = "ledger"

PERMISSION_REGISTRY.register(
    DocumentType(
        module=RECEIVABLES_PERMISSION_MODULE,
        code=LEDGER_PERMISSION_CODE,
        # Chỉ XEM và XUẤT: sổ phụ công nợ **không có đường sửa tay**. Mọi dòng
        # sinh ra từ chứng từ gốc qua `ArApSubledger`, và mọi thay đổi số đã
        # trả đi qua phiếu thu/chi. Cấp quyền `create`/`edit`/`delete` ở đây là
        # hứa một cánh cửa không tồn tại — và nếu sau này có ai mở nó thật thì
        # check toàn vẹn 131/331 là thứ duy nhất còn lại để bắt.
        actions=frozenset({Action.VIEW, Action.EXPORT}),
    )
)


def _register_providers() -> None:
    """Đăng ký ba Protocol còn lại + cửa ghi sổ phụ.

    Import đặt trong hàm theo đúng lối `cash_book._register_treasurer_source`,
    nhưng nói cho đúng: hàm này được gọi vô điều kiện ngay dưới đây, nên cây
    truy vấn **vẫn** được nạp lúc `model_registry` nạp gói. Cái nó thật sự mua
    là tránh vòng import lúc nạp module, không phải nạp lười.
    """
    from ket.kernel.protocols import PROVIDERS

    # `settlement_source` tự đăng ký ba Protocol còn lại lúc import.
    from ket.modules.receivables import settlement_source as settlement_source
    from ket.modules.receivables.ledger_service import SERVICE

    PROVIDERS.register_ar_ap_subledger(SERVICE)


def _register_settled_guard() -> None:
    """Chặn bỏ ghi sổ / xóa chứng từ mà khoản nợ của nó đã có người trả vào.

    `ArApLedgerService.remove` cũng kiểm điều này, và hai chỗ kiểm là có chủ
    đích chứ không thừa: `remove` canh đường mà `purchase`/`sales` **nhớ gọi**,
    còn guard này canh lượt bỏ ghi sổ mà chúng **quên gọi** — dòng sổ phụ ở lại
    thì vẫn hiện trên màn chọn đối trừ, và một phiếu thu trỏ vào nó.

    Phạm vi thật của bộ guard: nó chạy ở `PostingService.unpost`, **không** ở
    `VoucherService.delete` (`posting/documents/registry.py` gỡ lời gọi ấy ở
    review 6G-2 M-4). Đường xóa an toàn nhờ một bất biến khác: dòng sổ phụ chỉ
    sinh khi chứng từ GHI SỔ, còn `delete` chỉ nhận chứng từ đã cất — chứng từ
    xóa được thì không có dòng nào. Giữ bất biến ấy là nghĩa vụ của 7B/7C
    (`after_post` → `record`, `after_unpost` → `remove`).
    """
    from ket.modules.receivables.ledger_service import ensure_not_settled

    def _guard(session: Session, voucher_id: UUID) -> None:
        ensure_not_settled(session, voucher_id=voucher_id)

    REFERENCE_GUARDS.register(_guard)


_register_providers()
_register_settled_guard()
