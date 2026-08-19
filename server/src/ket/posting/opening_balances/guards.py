"""Rào chắn dùng chung của mọi đường ghi số dư ban đầu (FR-OPB-011, RT-09).

Hai đường ghi — nhập từ Excel và chuyển số dư từ năm trước — phải qua cùng hai
rào này, nên chúng đứng riêng thay vì nằm trong một job nào đó:

* **Kỳ đầu năm chưa khóa**, kiểm bằng `SELECT … FOR SHARE` trên chính hàng kỳ:
  cùng cơ chế chống write-skew với `PostingService` (RT-09) — lượt khóa sổ giữ
  `FOR UPDATE` nên hoặc lượt ghi số dư vào trước khi khóa, hoặc thấy kỳ đã khóa;
  không có cửa sổ chen giữa "kiểm xong" và "ghi xong". Chỉ cần kỳ **đầu** năm:
  4D cấm khóa kỳ không tuần tự, nên kỳ 1 còn mở nghĩa là mọi kỳ sau còn mở.
* **Một người ghi tại một thời điểm** cho mỗi (năm, chi nhánh), bằng advisory
  lock: `opening_balances` không có ràng buộc duy nhất (nhiều dòng cùng khóa là
  hợp lệ), nên hai lượt ghi song song không đổ lỗi mà lặng lẽ **nhân đôi dòng**
  — advisory lock là chỗ duy nhất nối tiếp được hai lượt đó. Theo (schema, chi
  nhánh, năm) vì advisory lock có phạm vi cả database (cùng bài học
  `recalc_job`).
"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    OpeningFiscalYearClosedError,
    OpeningPeriodLockedError,
    OpeningPeriodMissingError,
)
from ket.kernel.periods.models import AccountingPeriod, FiscalYear


def first_period_of(session: Session, fiscal_year_id: int) -> AccountingPeriod:
    """Kỳ đầu năm — nơi số dư ban đầu đổ vào snapshot (bàn giao 4B)."""
    period = (
        session.execute(
            select(AccountingPeriod)
            .where(AccountingPeriod.fiscal_year_id == fiscal_year_id)
            .order_by(AccountingPeriod.period_no)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if period is None:
        raise OpeningPeriodMissingError(
            "Năm tài chính chưa có kỳ kế toán — hãy tạo kỳ trước khi nhập số dư",
            fiscal_year_id=fiscal_year_id,
        )
    return period


def guard_opening_writable(
    session: Session, fiscal_year: FiscalYear, *, for_share: bool
) -> AccountingPeriod:
    """Khẳng định số dư ban đầu của năm còn sửa được; trả về kỳ đầu năm.

    `for_share=True` cho đường **ghi** (giữ khóa tới hết transaction — RT-09);
    `False` cho lượt kiểm, nơi một khóa giữ suốt job chỉ làm phiền người đang
    khóa sổ mà không bảo vệ được gì (lượt kiểm không ghi).
    """
    if fiscal_year.is_closed:
        raise OpeningFiscalYearClosedError(
            "Năm tài chính đã quyết toán — số dư ban đầu không sửa được nữa",
            fiscal_year_id=fiscal_year.id,
        )
    period = first_period_of(session, fiscal_year.id)
    if for_share:
        locked_at = session.execute(
            select(AccountingPeriod.locked_at)
            .where(AccountingPeriod.id == period.id)
            .with_for_update(read=True)
        ).scalar_one()
    else:
        locked_at = period.locked_at
    if locked_at is not None:
        raise OpeningPeriodLockedError(
            "Kỳ đầu năm đã khóa sổ — mở khóa kỳ trước khi sửa số dư ban đầu",
            fiscal_year_id=fiscal_year.id,
            period_id=period.id,
        )
    return period


def acquire_opening_write_lock(
    session: Session, *, dataset_schema: str, branch_id: int, fiscal_year_id: int
) -> None:
    """Tuần tự hóa mọi đường ghi số dư của một (năm, chi nhánh) — nhả khi commit."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tag)::bigint)"),
        {"tag": f"posting.opening_balances.write:{dataset_schema}:{branch_id}:{fiscal_year_id}"},
    )
