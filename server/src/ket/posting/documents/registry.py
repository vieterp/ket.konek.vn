"""Registry loại chứng từ của posting — mã loại → quyền + cách dựng định khoản.

Vì sao tồn tại: endpoint `POST /vouchers/{id}/actions/post` là **một** cho mọi
loại chứng từ (phase-04 §API surface), nhưng ghi sổ cần hai thứ chỉ module sở
hữu mới biết — mã quyền phải kiểm (`general_ledger.journal_voucher.post`) và
cách dịch chi tiết thành `PostingRequest`. Module đăng ký hai thứ đó lúc
import; posting không import module nào (luật phụ thuộc, contract C4) mà chỉ
gọi qua callable đã đăng ký — cùng khuôn "Protocol đăng ký lúc khởi động" của
plan §Luật phụ thuộc #2.

Khác với registry **phân quyền** (`kernel/security/permissions.py`): bên đó
trả lời "mã quyền nào tồn tại", bên này trả lời "chứng từ loại này ghi sổ thế
nào". Một loại chứng từ ghi sổ được phải có mặt ở **cả hai**.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.config.printing.context import DocumentPrintDetails
from ket.kernel.errors import DocumentTypeUnknownError
from ket.kernel.security.permissions import Action, permission_code
from ket.posting.engine.requests import PostingRequest

RequestBuilder = Callable[[Session, UUID], PostingRequest]
"""Dựng `PostingRequest` từ chi tiết đã lưu của một chứng từ."""

PrintDetailsBuilder = Callable[[Session, UUID, int], DocumentPrintDetails]
"""Phần riêng của loại chứng từ trên bản in — cùng lối `RequestBuilder`: chỉ
module biết "Họ và tên người nộp tiền" nằm ở cột nào. Nhận thêm `user_id` như
`LifecycleHook`: `money.scale` là tùy chọn có cấp NGƯỜI DÙNG, mà bản in phải
quy đổi đúng bằng đường ghi sổ của chính người đang in."""

LifecycleHook = Callable[[Session, UUID, int], None]
"""`(session, voucher_id, user_id)` — việc riêng của module quanh một bước
vòng đời, chạy trong CÙNG transaction với bước đó."""


@dataclass(frozen=True)
class PostingDocumentType:
    """Một loại chứng từ ghi sổ được qua endpoint hành động chung.

    Ba hook vòng đời (thêm ở lát 6B): phiếu thu/chi phải cộng số đã trả vào
    chứng từ công nợ khi ghi sổ, gỡ ra khi bỏ ghi sổ, và trả bộ đếm tham chiếu
    danh mục trước khi xóa — mà endpoint hành động chung (`routers/vouchers.py`)
    gọi thẳng `PostingService`/`VoucherService`, không đi qua service của
    module. Không có hook thì mỗi module có việc-đi-kèm sẽ phải tự mở endpoint
    hành động riêng, phá "một bộ endpoint cho mọi loại chứng từ" của phase-04.
    Hook chạy cùng transaction: ghi sổ và việc-đi-kèm cùng sống cùng chết.
    """

    code: str
    """Mã trên `vouchers.document_type` (`GLE`, `PT`, …)."""

    permission_module: str
    permission_name: str
    """Cặp ghép thành mã quyền `{module}.{name}.{action}` — trùng với
    `DocumentType` đã đăng ký bên registry phân quyền."""

    title: str
    build_request: RequestBuilder

    after_post: LifecycleHook | None = None
    """Chạy SAU khi `PostingService.post` xong, cùng transaction."""

    after_unpost: LifecycleHook | None = None
    """Chạy SAU khi `PostingService.unpost` xong, cùng transaction."""

    before_delete: LifecycleHook | None = None
    """Chạy TRƯỚC `VoucherService.delete`, cùng transaction — dọn những gì
    `ON DELETE CASCADE` không tự dọn được (bộ đếm tham chiếu, dấu vết ngoài)."""

    print_details: PrintDetailsBuilder | None = None
    """Trường riêng của loại này trên bản in (lát 6E-2). `None` = mẫu chỉ dùng
    phần chung (số, ngày, diễn giải, dòng định khoản) — đúng cho Phiếu kế toán.
    Có hook thì đường in **không** phải phân nhánh theo mã loại: thêm phân hệ
    là thêm một hàm ở module, không sửa `routers/printing.py` dùng chung."""

    def permission(self, action: Action) -> str:
        return permission_code(self.permission_module, self.permission_name, action)


class PostingDocumentRegistry:
    """Sổ đăng ký của một tiến trình — đối tượng để test dựng registry riêng."""

    def __init__(self) -> None:
        self._types: dict[str, PostingDocumentType] = {}

    def register(self, document_type: PostingDocumentType) -> None:
        if document_type.code in self._types:
            raise ValueError(f"Loại chứng từ {document_type.code} đã được đăng ký")
        self._types[document_type.code] = document_type

    def get(self, code: str) -> PostingDocumentType:
        document_type = self._types.get(code)
        if document_type is None:
            raise DocumentTypeUnknownError(
                "Loại chứng từ này chưa được đăng ký với posting engine", document_type=code
            )
        return document_type

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._types))


VoucherReferenceGuard = Callable[[Session, UUID], None]
"""`(session, voucher_id)` — "chứng từ này còn bị ai đó tham chiếu không".

Ném `DomainError` để chặn, im lặng để cho qua. Khác `LifecycleHook` ở hai
điểm quyết định: không nhận `user_id` (câu hỏi về DỮ LIỆU, không về người
bấm), và **không gắn với một loại chứng từ nào** — nó chạy cho mọi loại.
"""


class VoucherReferenceGuards:
    """Bộ guard chạy trước MỌI lượt bỏ ghi sổ và MỌI lượt xóa chứng từ.

    Vì sao là một bộ dùng chung thay vì thêm một `LifecycleHook` nữa cho từng
    loại: thứ được bảo vệ nằm ở phía **người tham chiếu**, không phía chứng từ.
    Dòng sao kê ngân hàng khớp được với phiếu quỹ nộp tiền và bút toán GLE
    (lát 6G-2, M-3) — nếu luật "đã khớp thì không bỏ ghi sổ" phải được từng
    module tự nhớ đăng ký vào hook của mình, thì mỗi phân hệ mới của phase 7–9
    là một cơ hội quên, và chỗ quên đó im lặng: chứng từ bỏ ghi sổ xong, dòng
    sao kê vẫn trỏ vào một phiếu nháp sửa được số tiền.

    Đăng ký từ phía module **giữ** ràng buộc (`bank` đăng ký guard sao kê),
    không từ phía module bị ràng buộc — nên `cash_book` và `general_ledger`
    không phải biết phân hệ ngân hàng tồn tại, và luật C3 (module không import
    module) vẫn đứng.

    Điểm gọi là `PostingService.unpost` — hàm mà **mọi** cửa bỏ ghi sổ đều đi
    qua (endpoint hành động chung lẫn service của từng module) — chứ không ở
    tầng router: đặt ở router thì đường service là một cửa thứ hai không có
    cổng, đúng bài học 6D H-3.

    **Không** chạy ở `VoucherService.delete` (review 6G-2 M-4): guard duy nhất
    hiện có canh chứng từ ĐÃ GHI SỔ, mà đường xóa chỉ nhận chứng từ Đã cất —
    lời gọi ở đó là mã chết, trả bằng một truy vấn thừa mỗi lượt xóa. Chiều xóa
    do FK `RESTRICT` của người tham chiếu canh. Guard nào cần canh chứng từ
    NHÁP phải tự đặt điểm gọi trước `ensure_editable` **và** kèm test chứng
    minh đường ấy tới được — đừng tin một lời gọi không có bài kiểm.
    """

    def __init__(self) -> None:
        self._guards: list[VoucherReferenceGuard] = []

    def register(self, guard: VoucherReferenceGuard) -> None:
        self._guards.append(guard)

    def check(self, session: Session, voucher_id: UUID) -> None:
        for guard in self._guards:
            guard(session, voucher_id)


REFERENCE_GUARDS: Final[VoucherReferenceGuards] = VoucherReferenceGuards()
"""Bộ guard của tiến trình — module đăng ký lúc import, cùng chỗ với registry
loại chứng từ."""


REGISTRY: Final[PostingDocumentRegistry] = PostingDocumentRegistry()
"""Registry của tiến trình. Module đăng ký loại của mình lúc import — cùng chỗ
với đăng ký mã quyền (xem `modules/general_ledger/journal/__init__.py`)."""
