"""Cột **"Có thể bán"** (U7, FR-STK-012, lát 8D) = tồn − đã hứa giao.

Tồn đọc từ sổ kho như mọi nơi khác (`stock.stock_rows`); "đã hứa giao" đọc qua
Protocol `CommitmentProvider` của kernel, module `sales` cài — `inventory`
không được import `sales` (luật phụ thuộc #1).

**Không** làm bằng VIEW SQL như phác thảo §Architecture của phase file: nguồn
cam kết là một bản cài Python đăng ký lúc chạy, và một view trong DB không gọi
được nó. Hình đúng là "một câu tồn + một lời gọi cổng, nối trong Python".

Cam kết trả về theo **mã hàng**, không theo lô: một đơn hứa giao 10 cái thì
khách không quan tâm lô nào, và ràng buộc lô là việc của lúc xuất. Nên khi một
mã hàng có nhiều lô ở cùng kho, cam kết trừ dần vào các dòng lô của chính khóa
`(kho, mã hàng)` ấy — lưới U7 hỏi theo mã hàng, còn ai hỏi theo lô thì đọc
`on_hand` của dòng ấy.

Cam kết là số **hiện tại**, không theo `as_of`: "còn đủ bán không" là câu hỏi
của hôm nay, và một lời hứa chưa giao thì chưa giao dù nhìn tồn ở mốc nào.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ket.kernel.protocols import PROVIDERS
from ket.modules.inventory.schemas import AvailabilityResponse, AvailabilityRow
from ket.modules.inventory.stock import stock_rows

_ZERO = Decimal(0)


def availability(
    session: Session,
    *,
    as_of: date,
    branch_id: int,
    warehouse_id: int | None = None,
    item_ids: tuple[int, ...] = (),
) -> AvailabilityResponse:
    """Tồn + đã hứa giao + có thể bán theo `(kho, vật tư, lô)` tại `as_of`."""
    rows = [
        row
        for row in stock_rows(session, as_of=as_of, branch_id=branch_id, warehouse_id=warehouse_id)
        if not item_ids or row.item_id in item_ids
    ]
    # Danh sách cổng, không một cổng: chiều ĐỌC của `PROVIDERS` cho phép nhiều
    # phân hệ cùng kể về cam kết của chúng (hôm nay chỉ `sales`), và tổng của
    # chúng mới là "đã hứa giao".
    #
    # Hỏi **một lượt cho mỗi kho** có mặt trong lưới, không một lượt cho cả
    # lưới: cổng trả về theo `item_id` (hợp đồng kernel), nên một lượt gọi gộp
    # cho nhiều kho cho ra một con số không biết thuộc kho nào — và lượt chia
    # bên dưới sẽ gán nó cho kho **sắp trước**, tức kho A gánh lời hứa của kho
    # B. Số kho trong một lưới là hàng đơn vị, số mã hàng là hàng trăm; lô gọi
    # theo kho giữ nguyên tính batch mà hợp đồng cổng quan tâm.
    #
    # Hệ quả có chủ đích: hóa đơn bán **không khai kho** không thuộc kho nào
    # nên không hiện ở dòng nào. Gán nó cho một kho bất kỳ là bịa; nó vẫn nằm
    # trong nhóm "chưa xuất kho" của tab việc còn thiếu, nơi nó có nghĩa.
    providers = PROVIDERS.commitment_providers()
    committed: dict[tuple[int, int], Decimal] = {}
    if providers and rows:
        wanted = sorted({row.item_id for row in rows})
        for warehouse in sorted({row.warehouse_id for row in rows}):
            for provider in providers:
                for item_id, quantity in provider.committed_quantities(
                    session, item_ids=wanted, branch_id=branch_id, warehouse_id=warehouse
                ).items():
                    key = (warehouse, item_id)
                    committed[key] = committed.get(key, _ZERO) + quantity
    # Cam kết của một (kho, mã hàng) trừ dần vào các dòng LÔ của chính khóa ấy
    # theo thứ tự đã sắp: dòng đầu gánh trước, phần còn thừa dồn vào dòng CUỐI
    # của khóa (nên `available_to_promise` âm hiện ở đó). Cộng nguyên số vào
    # MỌI dòng sẽ trừ cùng một lời hứa nhiều lần và biến một mã nhiều lô thành
    # "không còn gì để bán"; bỏ phần thừa đi thì Σ cam kết của lưới không bằng
    # số đã hứa thật.
    last_row_of_key = {(row.warehouse_id, row.item_id): index for index, row in enumerate(rows)}
    remaining = dict(committed)
    items: list[AvailabilityRow] = []
    for index, row in enumerate(rows):
        key = (row.warehouse_id, row.item_id)
        left = remaining.get(key, _ZERO)
        share = left if index == last_row_of_key[key] else min(left, max(row.on_hand, _ZERO))
        remaining[key] = left - share
        items.append(
            AvailabilityRow(
                branch_id=row.branch_id,
                warehouse_id=row.warehouse_id,
                item_id=row.item_id,
                lot_id=row.lot_id,
                on_hand=row.on_hand,
                committed=share,
                available_to_promise=row.on_hand - share,
                custodial_qty=row.custodial_qty,
            )
        )
    return AvailabilityResponse(
        as_of=as_of, has_commitment_source=bool(providers), items=tuple(items)
    )
