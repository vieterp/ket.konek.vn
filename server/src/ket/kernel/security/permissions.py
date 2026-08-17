"""Registry loại chứng từ → mã quyền (FR-SYS-071, FR-NFR-011).

Mã quyền có dạng `{module}.{document_type}.{action}` và được **sinh ra**, không
gõ tay. Lý do nằm ở rủi ro đã ghi trong plan: nếu bảng quyền lưu một danh sách
mã cố định thì mỗi loại chứng từ mới (phase 6 trở đi có hàng chục) lại là một
lần sửa schema quyền. Ở đây, thêm chứng từ = thêm một `DocumentType` vào
registry; bảng `permissions` được đồng bộ từ registry (`role_service`), không ai
phải viết migration.

Registry sống trong `kernel` còn loại chứng từ thật thì thuộc `modules.*` —
module đăng ký lúc import (luật phụ thuộc cho phép `modules → kernel`, không
cho chiều ngược lại). Phase 2 chỉ có loại "chứng từ" của chính hệ thống: người
dùng, vai trò, chi nhánh, tùy chọn.

**Hai lớp hành vi tách bạch:** `Action` là danh sách đóng (tám hành vi của SRS
19 §2), còn tập hành vi *hợp lệ* thì khai theo từng loại chứng từ — danh mục
không có `post`/`unpost`, và sinh mã cho hành vi vô nghĩa chỉ làm loãng màn hình
phân quyền.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ket.kernel.security.rls import validate_identifier


class Action(StrEnum):
    """Tám hành vi của SRS 19 §2 (FR-NFR-011). Danh sách đóng.

    Đóng chứ không mở: mã quyền là **hợp đồng công khai** — nó nằm trong dữ liệu
    phân quyền của khách hàng, trong gói cấu hình, trong tài liệu vận hành. Một
    hành vi thứ chín thêm tùy hứng ở một module sẽ không có chỗ nào hiển thị nó.
    """

    VIEW = "view"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    POST = "post"
    UNPOST = "unpost"
    PRINT = "print"
    EXPORT = "export"


CATALOG_ACTIONS: Final[frozenset[Action]] = frozenset(
    {Action.VIEW, Action.CREATE, Action.EDIT, Action.DELETE}
)
"""Bộ hành vi của một **danh mục** (không ghi sổ, không in chứng từ)."""

VOUCHER_ACTIONS: Final[frozenset[Action]] = frozenset(Action)
"""Bộ hành vi đầy đủ của một **chứng từ**. Dùng từ phase 6."""


@dataclass(frozen=True)
class DocumentType:
    """Một loại chứng từ / danh mục có thể phân quyền được.

    `requires_second_factor` là chiều thứ hai của FR-SYS-074: tài khoản nắm
    quyền trên loại này bị bắt bật 2FA. Khai **ở đây** chứ không phải một danh
    sách mã nằm cạnh luồng đăng nhập, vì danh sách rời sẽ không được cập nhật
    khi phase 7 thêm `einvoice` — và cái giá của việc quên là một quyền nhạy cảm
    chạy không có lớp thứ hai.
    """

    module: str
    code: str
    actions: frozenset[Action]
    requires_second_factor: bool = False

    def __post_init__(self) -> None:
        # Mã quyền đi vào DB và vào gói cấu hình ký số; một dấu chấm lọt vào
        # `module`/`code` sẽ làm mọi phép tách mã sau này lệch một đoạn.
        validate_identifier(self.module)
        validate_identifier(self.code)
        if not self.actions:
            raise ValueError(f"Loại chứng từ {self.module}.{self.code} không có hành vi nào")

    def permission_codes(self) -> tuple[str, ...]:
        return tuple(
            permission_code(self.module, self.code, action) for action in sorted(self.actions)
        )


def permission_code(module: str, document_type: str, action: Action) -> str:
    """`{module}.{document_type}.{action}` — dạng duy nhất của một mã quyền."""
    return f"{module}.{document_type}.{action.value}"


class PermissionRegistry:
    """Sổ đăng ký loại chứng từ của một tiến trình.

    Là đối tượng chứ không phải biến module toàn cục có sẵn: test cần dựng
    registry riêng để kiểm "thêm loại chứng từ mới không phải đụng schema" mà
    không làm bẩn registry thật.
    """

    def __init__(self) -> None:
        self._types: dict[tuple[str, str], DocumentType] = {}

    def register(self, document_type: DocumentType) -> None:
        """Đăng ký một loại. Đăng ký trùng khóa → lỗi lập trình, ném ngay.

        Ném thay vì ghi đè: hai module cùng khai một loại là dấu hiệu ranh giới
        module đã bị chồng lấn, và ghi đè im lặng sẽ khiến bên thua cuộc mất
        quyền tùy theo thứ tự import — thứ không tái hiện được.
        """
        key = (document_type.module, document_type.code)
        if key in self._types:
            raise ValueError(f"Loại chứng từ {key[0]}.{key[1]} đã được đăng ký")
        self._types[key] = document_type

    def document_types(self) -> tuple[DocumentType, ...]:
        return tuple(self._types[key] for key in sorted(self._types))

    def codes(self) -> tuple[str, ...]:
        """Mọi mã quyền, đã sắp xếp — nguồn để đồng bộ bảng `permissions`."""
        return tuple(
            sorted(code for doc in self._types.values() for code in doc.permission_codes())
        )

    def second_factor_codes(self) -> frozenset[str]:
        """Mã quyền mà người nắm giữ bắt buộc phải bật 2FA (FR-SYS-074)."""
        return frozenset(
            code
            for doc in self._types.values()
            if doc.requires_second_factor
            for code in doc.permission_codes()
        )


SYSTEM_MODULE: Final[str] = "system"

REGISTRY: Final[PermissionRegistry] = PermissionRegistry()
"""Registry của tiến trình. Module nghiệp vụ đăng ký loại của mình lúc import."""

REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="user",
        actions=CATALOG_ACTIONS,
        # Quản lý tài khoản = tạo được một danh tính rồi đăng nhập bằng nó. Đây
        # đúng là "vai trò quản trị" mà FR-NFR-016 đòi 2FA.
        requires_second_factor=True,
    )
)
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="role",
        actions=CATALOG_ACTIONS,
        # Sửa được vai trò là tự cấp được mọi quyền còn lại — cùng mức rủi ro
        # với quản lý tài khoản, nên cùng yêu cầu.
        requires_second_factor=True,
    )
)
REGISTRY.register(DocumentType(module=SYSTEM_MODULE, code="branch", actions=CATALOG_ACTIONS))
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="audit_log",
        # Nhật ký chỉ đọc được (FR-NFR-013): không `create`/`edit`/`delete`, và
        # đó không phải thiếu sót mà là chính bất biến của bảng — quyền ghi ở
        # đây sẽ mâu thuẫn với việc DB đã từ chối `UPDATE`/`DELETE` cho vai trò
        # runtime.
        actions=frozenset({Action.VIEW, Action.EXPORT}),
    )
)
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE, code="setting", actions=frozenset({Action.VIEW, Action.EDIT})
    )
)
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="job",
        # Hàng đợi tác vụ nền: `view` = xem hàng đợi và tiến độ, `create` = xếp
        # một tác vụ vào hàng, `edit` = yêu cầu hủy. Không `delete`: dòng job là
        # lịch sử "đã chạy gì lúc nào", và xóa nó đi thì câu hỏi "vì sao số liệu
        # đổi tối qua" không còn trả lời được.
        actions=frozenset({Action.VIEW, Action.CREATE, Action.EDIT}),
    )
)
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="installation",
        # Tác vụ chạm **bảng dùng chung cho mọi dữ liệu kế toán** (schema điều
        # khiển): dọn phiên đăng nhập là ví dụ đầu tiên. Tách khỏi `maintenance`
        # vì phạm vi khác hẳn nhau, và ranh giới đó đo được: quyền được cấp
        # **trong một dataset** (bảng `role_permissions` nằm trong schema
        # dataset), nên một mã quyền dùng chung sẽ cho người chỉ quản trị doanh
        # nghiệp B chạy được thao tác xóa dữ liệu của cả bản cài.
        #
        # Đòi 2FA (FR-SYS-074): đây là quyền duy nhất ở phase 2 mà một dataset
        # cấp được nhưng hậu quả vượt ra ngoài dataset đó.
        actions=frozenset({Action.VIEW, Action.CREATE}),
        requires_second_factor=True,
    )
)
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="attachment",
        # Tệp đính kèm (FR-NFR-053). Ba hành vi, không có `edit`: nội dung một
        # tệp không sửa được tại chỗ — thay tệp = gỡ tệp cũ, đính tệp mới, và
        # hai vết đó là thứ kiểm toán viên cần thấy.
        #
        # Một mã quyền **riêng** ở phase này, không suy theo chứng từ chủ: chưa
        # có loại chứng từ nghiệp vụ nào tồn tại để suy. Từ phase 6, khi đính
        # kèm gắn vào phiếu chi và hóa đơn thật, quyền có thể siết thêm bằng
        # cách kiểm quyền của chứng từ chủ — mã này vẫn là cổng thứ nhất.
        actions=frozenset({Action.VIEW, Action.CREATE, Action.DELETE}),
    )
)
REGISTRY.register(
    DocumentType(
        module=SYSTEM_MODULE,
        code="maintenance",
        # Tác vụ dọn dẹp chạy tại máy chủ (dọn khóa idempotency, dọn phiên đăng
        # nhập). Tách khỏi `job` vì hai câu hỏi khác nhau: `job.create` cho phép
        # chạy tác vụ nghiệp vụ bình thường, còn quyền này cho phép chạy thứ
        # **xóa dữ liệu** ngoài luồng nghiệp vụ.
        actions=frozenset({Action.VIEW, Action.CREATE}),
        # **Không** đòi 2FA, khác `installation`. Quyền này chỉ chạy tác vụ dọn
        # dẹp **trong phạm vi một dữ liệu kế toán** (khóa idempotency hết hạn):
        # phạm vi bằng đúng phạm vi nơi nó được cấp, không leo thang được, và
        # không chạm dữ liệu đang sống. Thao tác vượt ra ngoài dataset thuộc về
        # `system.installation` ở trên.
    )
)
