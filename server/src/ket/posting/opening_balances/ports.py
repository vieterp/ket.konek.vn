"""Cổng chi tiết của số dư ban đầu — nhóm mà bảng chi tiết thuộc về một module.

Nhóm 0–4 sống trọn trong `posting` (dòng cha + `opening_balance_invoices`).
Nhóm **tồn kho (5)** thì không: lớp đầu kỳ chỉ có nghĩa khi nó nằm trong **sổ
kho** (`inventory_movements`, bảng của module `inventory`) — engine tính giá,
guard tồn, lưới tồn và báo cáo đọc đúng một nguồn ấy — và lô (`lots`) cũng
thuộc module. `posting` không được import `modules` (C4), nên phần "vật chất
hóa lớp thành movement" đi qua một registry do module đăng ký lúc import, cùng
lý do tồn tại với `LOCK_CHECKS` và `REFERENCE_GUARDS`.

Cổng **mù sổ** (`ledger`): chi tiết phía module (sổ kho) là một cho cả hai sổ,
nên `posting` chỉ gọi cổng cho lượt ghi/xóa **sổ tài chính** — nhóm 5 sổ quản
trị bị từ chối ở kiểm tra cứu và chỉ tới từ chuyển năm (dòng sổ cái, không lớp).

Hợp đồng bốn việc, đều chạy trong transaction của người gọi:

* `lot_id_for` — số lô của sheet thành id lô (tra/tạo);
* `clear` — gỡ movement của lớp cũ **trước** khi `posting` xóa dòng cha (FK
  `RESTRICT` từ movement về lớp giữ thứ tự này ở tầng bảng);
* `materialize` — sau khi `posting` đã chèn cha + lớp: dựng movement nhập đã có
  giá cho từng lớp;
* `annotate_carried` — sau lượt chuyển năm: điền số lượng/đơn giá cho dòng
  nhóm 5 năm nhận từ sổ kho (số tiền đã tính trong SQL của `posting`).

Nhóm 6–9 (8E) đăng ký cổng của chúng ở đây khi tới lượt; `OPENING_DETAIL_PORTS.
require` là chỗ duy nhất `posting` hỏi "nhóm này ai giữ chi tiết".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.posting.opening_balances.models import OpeningDetailKind


@dataclass(frozen=True)
class OpeningStockLayer:
    """Một lớp tồn đầu kỳ đã ghi vào `opening_balance_stock_layers` — đầu vào của
    `materialize`. Số lượng theo **đơn vị chính**, tiền VND."""

    id: UUID
    warehouse_id: int
    item_id: int
    lot_id: int | None
    received_on: date | None
    receipt_no: str | None
    sort_order: int
    quantity: Decimal
    unit_cost: Decimal
    amount: Decimal


class OpeningDetailPort(Protocol):
    """Bản cài của một module cho một nhóm số dư có chi tiết ngoài `posting`."""

    @property
    def kind(self) -> int: ...

    def lot_id_for(self, session: Session, *, item_id: int, lot_no: str) -> int:
        """Tra/tạo lô theo số lô của sheet — bảng lô thuộc module."""
        ...

    def clear(self, session: Session, *, fiscal_year: FiscalYear, branch_id: int) -> int:
        """Gỡ chi tiết phía module của (năm, chi nhánh); trả số dòng đã gỡ."""
        ...

    def materialize(
        self,
        session: Session,
        *,
        fiscal_year: FiscalYear,
        first_period: AccountingPeriod,
        branch_id: int,
        layers: Sequence[OpeningStockLayer],
    ) -> int:
        """Dựng chi tiết phía module cho các lớp vừa ghi; trả số dòng đã dựng."""
        ...

    def annotate_carried(
        self,
        session: Session,
        *,
        source_year: FiscalYear,
        target_year: FiscalYear,
        branch_id: int,
    ) -> int:
        """Điền phần chỉ module biết (số lượng, đơn giá) cho dòng năm nhận."""
        ...


class OpeningDetailPorts:
    """Sổ đăng ký của tiến trình — mỗi nhóm đúng một cổng (hai bản cài là hai
    nơi tranh nhau một bảng chi tiết; đăng ký trùng ném ngay)."""

    def __init__(self) -> None:
        self._ports: dict[int, OpeningDetailPort] = {}

    def register(self, port: OpeningDetailPort) -> None:
        if port.kind in self._ports:
            raise RuntimeError(f"Nhóm số dư {port.kind} đã có cổng chi tiết đăng ký")
        self._ports[port.kind] = port

    def get(self, kind: int) -> OpeningDetailPort | None:
        return self._ports.get(kind)

    def require(self, kind: int) -> OpeningDetailPort:
        """Nhóm có mặt trong tệp/tham số mà không ai giữ chi tiết là lỗi cấu hình
        tiến trình (module chưa được `model_registry` nạp), không phải lỗi người dùng."""
        port = self._ports.get(kind)
        if port is None:
            raise RuntimeError(
                f"Nhóm số dư {kind} cần cổng chi tiết của module nhưng chưa ai đăng ký — "
                "kiểm tra `ket.model_registry`"
            )
        return port


OPENING_DETAIL_PORTS: Final[OpeningDetailPorts] = OpeningDetailPorts()
"""Registry của tiến trình — module đăng ký lúc import (qua `ket.model_registry`)."""

PORTED_KINDS: Final[frozenset[int]] = frozenset({OpeningDetailKind.STOCK})
"""Nhóm mà `posting` **phải** gọi cổng khi nhóm có mặt: tồn kho (5). 8E nối
thêm CCDC/TSCĐ/trả trước vào đây cùng cổng của chúng."""
