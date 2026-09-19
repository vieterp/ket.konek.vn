"""Bản cài `InventoryPosting` (kernel Protocol, RT-18 / ADR-024) — cửa duy nhất để
chứng từ mua/bán sinh và gỡ phiếu kho mà không import module này.

`create_movement` lập phiếu NK/XK với `source_document_id` trỏ chứng từ nguồn
rồi **ghi sổ ngay trong cùng transaction** (hook `after_post` của nguồn gọi
vào đây): sổ kho có movement cùng lúc sổ cái có bút toán của hóa đơn — nửa
này ghi, nửa kia chưa là đúng hình lệch mà check BR-STK-03 sinh ra để bắt.

Ghi sổ với `acknowledged_warnings=True`, có chủ đích: guard tồn kho
(`guards.StockNegativeGuard`) đã soi CHÍNH những dòng này ở chứng từ nguồn qua
`InventoryLineSource`, nơi người dùng xác nhận được; kêu lần hai ở đây là kêu
trong một hook không ai trả lời được. Mức "chặn" thì đã chặn từ nguồn.

`remove_movement` là chiều ngược của hook `after_unpost` nguồn: bỏ ghi sổ +
xóa phiếu sinh. Lượt bỏ ghi sổ phiếu đi qua `REFERENCE_GUARDS`, trong đó có
`guards.refuse_when_source_posted` — nó cho qua vì nguồn vừa về Đã cất trước
khi hook chạy (`PostingService.unpost` đổi trạng thái trước, hook sau).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.protocols import InventoryMovementKind, InventoryMovementLine
from ket.modules.inventory.service import (
    SOURCE_ISSUE_OPERATION,
    SOURCE_RECEIPT_OPERATION,
    InventoryVoucherService,
)

_DEFAULT_OPERATION = {
    InventoryMovementKind.RECEIPT: SOURCE_RECEIPT_OPERATION,
    InventoryMovementKind.ISSUE: SOURCE_ISSUE_OPERATION,
}


class InventoryPostingBridge:
    """Cài `kernel.protocols.InventoryPosting` — đăng ký ở `inventory/__init__`."""

    def create_movement(
        self,
        session: Session,
        *,
        kind: InventoryMovementKind,
        source_voucher_id: UUID,
        branch_id: int,
        posting_date: date,
        currency_code: str,
        exchange_rate: Decimal,
        lines: Sequence[InventoryMovementLine],
        user_id: int,
        operation_code: str | None = None,
        partner_id: int | None = None,
        partner_kind: PartnerKind | None = None,
        description: str | None = None,
    ) -> UUID:
        service = InventoryVoucherService(session)
        voucher = service.create_from_source(
            kind=kind,
            source_voucher_id=source_voucher_id,
            branch_id=branch_id,
            posting_date=posting_date,
            currency_code=currency_code,
            exchange_rate=exchange_rate,
            lines=lines,
            operation_code=operation_code or _DEFAULT_OPERATION[kind],
            partner_id=partner_id,
            partner_kind=partner_kind,
            description=description,
            user_id=user_id,
        )
        service.post(voucher.id, user_id=user_id, acknowledged_warnings=True)
        return voucher.id

    def remove_movement(self, session: Session, *, source_voucher_id: UUID, user_id: int) -> None:
        service = InventoryVoucherService(session)
        for voucher in service.generated_for_source(source_voucher_id):
            # `unpost` chạy `REFERENCE_GUARDS` (thủ kho đã ghi sổ kho — 8D — sẽ
            # chặn ở đây) rồi gỡ movement; xóa phiếu sau cùng.
            service.unpost(voucher.id, user_id=user_id)
            service.delete(voucher.id)
