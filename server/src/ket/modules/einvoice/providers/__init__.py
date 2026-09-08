"""Adapter nhà cung cấp hóa đơn điện tử.

Import gói này là đăng ký nhà cung cấp `internal`; 7E-2 thêm `easyinvoice` cạnh
nó. `ket.model_registry` là điểm import, cùng khuôn với mọi registry khác.
"""

from __future__ import annotations

from ket.modules.einvoice.providers import internal as _internal_registration
from ket.modules.einvoice.providers.contracts import (
    EInvoiceProvider,
    IssueOutcome,
    ProviderAcceptance,
    ProviderStatus,
)
from ket.modules.einvoice.providers.internal import INTERNAL_PROVIDER_CODE
from ket.modules.einvoice.providers.registry import PROVIDERS

__all__ = [
    "INTERNAL_PROVIDER_CODE",
    "PROVIDERS",
    "EInvoiceProvider",
    "IssueOutcome",
    "ProviderAcceptance",
    "ProviderStatus",
    "_internal_registration",
]
