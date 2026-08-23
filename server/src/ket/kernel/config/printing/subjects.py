"""Registry **bản in không phải chứng từ** — biên bản kiểm kê và họ hàng.

`print_templates.document_type` trả lời câu "mẫu này in ra cái gì", và không
phải cái gì in ra cũng là một dòng trong `vouchers`: biên bản kiểm kê quỹ
(08a-TT, lát 6E-2) là bản in đầu tiên như vậy, phase 8 sẽ có biên bản kiểm kê
kho, phase 9 có bảng kê.

Vì sao cần registry chứ không phải một bảng ánh xạ trong router: `GET
/print-templates` là endpoint DÙNG CHUNG và nó phải biết mã quyền nào canh mã
bản in nào. Nhét `{"KKQ": "cash_book.count_sheet.view"}` vào router là đưa tri
thức của một phân hệ vào đường dùng chung — đúng thứ mà bước 22 của phase 6 bắt
phải tổng quát hóa. Module tự khai lúc import, cùng khuôn
`POSTING_DOCUMENT_REGISTRY` và `PERMISSION_REGISTRY`.

Ở kernel (chứ không ở `posting`) vì bản in ở đây **không** đi qua posting: nó
không có định khoản, không ghi sổ, và `posting` không nên biết chúng tồn tại.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class PrintSubject:
    """Một loại bản in không phải chứng từ."""

    code: str
    """Giá trị đi vào `print_templates.document_type` (`KKQ`, …). Không được
    trùng mã loại chứng từ nào của posting — hai thứ dùng chung một cột."""

    title: str
    view_permission: str
    """Mã quyền đủ để XEM (và do đó in) bản in này. Bản in không có mã quyền
    `.print` riêng: quyền in một tờ giấy chỉ chép lại thứ người ta đã đọc được
    trên màn hình thì không phải một quyền thứ hai."""


class PrintSubjectRegistry:
    """Sổ đăng ký của một tiến trình — cùng lối `PermissionRegistry`."""

    def __init__(self) -> None:
        self._subjects: dict[str, PrintSubject] = {}

    def register(self, subject: PrintSubject) -> None:
        if subject.code in self._subjects:
            raise ValueError(f"Bản in {subject.code} đã được đăng ký")
        self._subjects[subject.code] = subject

    def get(self, code: str) -> PrintSubject | None:
        """`None` khi mã không thuộc registry này — người gọi tự quyết định
        đó là mã loại chứng từ hay là mã sai."""
        return self._subjects.get(code)

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._subjects))


REGISTRY: Final[PrintSubjectRegistry] = PrintSubjectRegistry()
"""Registry của tiến trình. Module đăng ký lúc import (`ket.model_registry`)."""
