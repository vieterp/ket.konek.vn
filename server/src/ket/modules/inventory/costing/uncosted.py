"""FR-STK-008 — danh sách chứng từ kho chưa được tính giá (lát 8C-2).

Khác xem trước FR-STK-003 (`affected.py`: "chạy job thì chứng từ nào bị ghi
lại"), đây trả lời "còn chứng từ nào chưa có giá" — sau một lượt job xanh danh
sách phải rỗng, trừ khóa chưa có lần nhập có giá (luật vượt tồn 8B). Hai câu
SQL gộp (danh sách có `LIMIT` + đếm), Python chỉ đổ vào schema (ADR-014).
"""

from __future__ import annotations

from datetime import date
from importlib import resources
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

from ket.modules.inventory.schemas import AffectedVoucher, UncostedVouchersResponse

UNCOSTED_VOUCHER_LIMIT: Final[int] = 500
"""Cùng trần với xem trước FR-STK-003 — đủ để đọc, `count` mang tổng thật."""

_SQL_ROOT: Final = resources.files("ket.modules.inventory.costing").joinpath("sql")
UNCOSTED_VOUCHERS_SQL: Final[str] = _SQL_ROOT.joinpath("uncosted_vouchers.sql").read_text("utf-8")
UNCOSTED_COUNT_SQL: Final[str] = _SQL_ROOT.joinpath("uncosted_count.sql").read_text("utf-8")


def uncosted_vouchers(
    session: Session,
    *,
    branch_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> UncostedVouchersResponse:
    params = {"branch_id": branch_id, "date_from": date_from, "date_to": date_to}
    total = int(session.execute(text(UNCOSTED_COUNT_SQL), params).scalar_one() or 0)
    rows = session.execute(
        text(UNCOSTED_VOUCHERS_SQL), {**params, "limit": UNCOSTED_VOUCHER_LIMIT}
    ).all()
    return UncostedVouchersResponse(
        branch_id=branch_id,
        date_from=date_from,
        date_to=date_to,
        count=total,
        vouchers=tuple(
            AffectedVoucher(
                voucher_id=row.voucher_id,
                voucher_no=row.voucher_no,
                document_type=row.document_type,
                posting_date=row.posting_date,
                movements=int(row.movements),
            )
            for row in rows
        ),
    )
