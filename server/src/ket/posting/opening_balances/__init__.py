"""Số dư ban đầu nhóm 0–4 (SRS 02, RT-24) — schema từ 4A, logic từ 4C.

Import gói này (qua `ket.model_registry`, như mọi đường cấp dataset) là đăng ký
hai thứ, cùng khuôn với `posting/balances/__init__.py`:

* mã quyền `posting.opening_balance.view` (xem, kiểm tệp) và
  `posting.opening_balance.create` (ghi từ tệp đã kiểm, chuyển số dư năm sau)
  — phải có mặt trước khi `provision_dataset` gieo bảng `permissions`;
* bốn loại job (kiểm tệp, ghi tệp, chuyển năm, xóa nhóm) vào registry hàng đợi
  — worker và API cùng nhìn thấy chúng;
* (lát 6B) nguồn công nợ số dư ban đầu vào `kernel.protocols.PROVIDERS` —
  `ReceivableProvider`/`PayableProvider`/`SettlementTargetSource` cho đối trừ
  của phiếu thu/chi tiền.
"""

from __future__ import annotations

from ket.kernel.security.permissions import (
    REGISTRY as PERMISSION_REGISTRY,
)
from ket.kernel.security.permissions import (
    Action,
    DocumentType,
)
from ket.posting.opening_balances import carry_forward_job as carry_forward_job
from ket.posting.opening_balances import clear_job as clear_job
from ket.posting.opening_balances import import_job as import_job
from ket.posting.opening_balances import settlement_source as settlement_source

PERMISSION_REGISTRY.register(
    DocumentType(
        module="posting",
        code="opening_balance",
        # `view` + `create` là đủ: đường sửa là nhập lại theo lô (xem docstring
        # `OpeningBalance`) nên "sửa" chính là một lượt `create` thay trọn nhóm;
        # xóa lẻ từng dòng số dư không phải một thao tác nghiệp vụ có thật.
        actions=frozenset({Action.VIEW, Action.CREATE}),
    )
)
