"""Sổ đăng ký adapter nhà cung cấp — đổi nhà cung cấp = đổi cấu hình.

Cùng khuôn với `kernel.jobs.registry` và registry mã quyền, vì cùng một lý do:
7E-2 thêm EasyInvoice, các lát sau thêm nhà thứ hai, và một chuỗi
`if provider_code == "..."` trong `outbox.py` sẽ là nơi mọi lát phải sửa chung
một tệp.

Tra cứu là **theo `provider_code` ghi trên dòng outbox**, không theo cấu hình
hiện hành: cấu hình đổi giữa lúc một dòng còn treo là chuyện bình thường, còn
`client_ref` thì chỉ có nghĩa với đúng nhà đã nhận nó.
"""

from __future__ import annotations

from ket.kernel.errors import EInvoiceProviderUnknownError
from ket.modules.einvoice.providers.contracts import EInvoiceProvider


class ProviderRegistry:
    """Registry của tiến trình, nạp lúc import module."""

    def __init__(self) -> None:
        self._providers: dict[str, EInvoiceProvider] = {}

    def register(self, code: str, provider: EInvoiceProvider) -> None:
        if code in self._providers:
            raise ValueError(f"Nhà cung cấp hóa đơn `{code}` đã đăng ký")
        self._providers[code] = provider

    def resolve(self, code: str) -> EInvoiceProvider:
        """Adapter của `code`, hoặc lỗi nghiệp vụ nêu đúng mã còn thiếu.

        Không trả `None`: một dòng outbox mang mã không ai cài là một lượt phát
        hành **không bao giờ chạy**, và người vận hành cần biết tên nhà cung cấp
        phải cấu hình, chứ không phải một `AttributeError` ở giữa worker.
        """
        provider = self._providers.get(code)
        if provider is None:
            raise EInvoiceProviderUnknownError(
                f"Chưa cài đặt nhà cung cấp hóa đơn điện tử `{code}`", provider_code=code
            )
        return provider

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


PROVIDERS = ProviderRegistry()
