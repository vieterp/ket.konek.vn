"""Adapter nhà cung cấp hóa đơn điện tử.

Import gói này là đăng ký hai nhà cung cấp: `internal` (phát hành nội bộ, không
qua bên thứ ba) và `easyinvoice` (SoftDreams). `ket.model_registry` là điểm
import, cùng khuôn với mọi registry khác.
"""

from __future__ import annotations

from ket.modules.einvoice.providers import internal as _internal_registration
from ket.modules.einvoice.providers.contracts import (
    EInvoiceProvider,
    IssueOutcome,
    ProviderAcceptance,
    ProviderBinding,
    ProviderStatus,
)
from ket.modules.einvoice.providers.easyinvoice import provider as _easyinvoice_registration
from ket.modules.einvoice.providers.easyinvoice.provider import EASYINVOICE_PROVIDER_CODE
from ket.modules.einvoice.providers.internal import INTERNAL_PROVIDER_CODE
from ket.modules.einvoice.providers.registry import PROVIDERS

__all__ = [
    "EASYINVOICE_PROVIDER_CODE",
    "INTERNAL_PROVIDER_CODE",
    "PROVIDERS",
    "EInvoiceProvider",
    "IssueOutcome",
    "ProviderAcceptance",
    "ProviderBinding",
    "ProviderStatus",
    "_easyinvoice_registration",
    "_internal_registration",
]
