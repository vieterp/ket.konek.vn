"""Khai và đọc hồ sơ đăng nhập nhà cung cấp hóa đơn điện tử (FR-EIV-001).

**Một dòng cho mỗi `provider_code`**, nên đường ghi là *đặt* chứ không *thêm*:
khai lại cùng mã là sửa hồ sơ đang có. Hai dòng cho một nhà cung cấp thì
`invoice_forms.provider_code` không còn trỏ được vào đâu cả, và ràng buộc duy
nhất ở tầng bảng canh chiều ấy.

**Mật khẩu vào thì mã hóa, ra thì không có.** Bí mật đi qua `SecretBox` (Fernet)
đúng như bí mật TOTP, và không hàm nào ở đây trả nó về — kể cả dạng bản mã, vì
bản mã vẫn là thứ mang đi thử ngoại tuyến được.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.security.keystore import SecretBox
from ket.modules.einvoice.models import EInvoiceProviderProfile


class ProviderProfileService:
    """Đọc và ghi hồ sơ nhà cung cấp, trong transaction của người gọi."""

    def __init__(self, session: Session, secret_box: SecretBox) -> None:
        self._session = session
        self._secret_box = secret_box

    def put(
        self,
        *,
        provider_code: str,
        base_url: str,
        username: str,
        password: str,
        tax_code: str,
        is_active: bool = True,
    ) -> EInvoiceProviderProfile:
        """Đặt hồ sơ cho một nhà cung cấp — tạo mới hoặc ghi đè hồ sơ đang có."""
        profile = self._session.scalars(
            select(EInvoiceProviderProfile).where(
                EInvoiceProviderProfile.provider_code == provider_code
            )
        ).one_or_none()
        password_enc = self._secret_box.encrypt(password)
        if profile is None:
            profile = EInvoiceProviderProfile(provider_code=provider_code)
            self._session.add(profile)
        profile.base_url = base_url
        profile.username = username
        profile.password_enc = password_enc
        profile.tax_code = tax_code
        profile.is_active = is_active
        self._session.flush()
        return profile

    def list_all(self) -> list[EInvoiceProviderProfile]:
        stmt = select(EInvoiceProviderProfile).order_by(EInvoiceProviderProfile.provider_code)
        return list(self._session.scalars(stmt))
