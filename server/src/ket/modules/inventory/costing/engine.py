"""Lõi tính giá xuất kho — điều phối các câu SQL set-based theo năm và theo vòng.

Một lượt `run_costing` cho một chi nhánh (RLS của phiên gọi), trong transaction
đang mở của người gọi, không tự commit:

1. `years_to_process` — mỗi năm tài chính có dấu bẩn / dòng chưa tính là một
   lượt riêng; horizon ghi cắt ở cuối năm (cùng luật `lock_check`; lịch sử
   trước đó vẫn được ĐỌC để lấy tồn đầu / lớp còn lại — FIFO không cần "chuyển
   năm" ở tầng này).
2. `locked_periods_touched` — RT-11 phương án A: có kỳ khóa trong horizon là
   từ chối trước khi chạm bảng.
3. Vòng (`_run_passes`): câu phương pháp của năm ghi dòng XUẤT; `transfer_in_
   legs.sql` chép giá vế đi sang vế đến; cả hai chỉ ghi dòng đổi thật và trả
   `RETURNING` — hai câu cùng rỗng là điểm bất động. Vượt `MAX_PASSES` là dữ
   liệu có vòng lặp giá → `CostingNotConvergingError`.
4. `finalize.sql` — STALE có giá → COSTED; `mark_next_year.sql` — khóa còn
   movement ở năm sau nhận dấu bẩn đầu năm sau (số năm sau đã lệch tồn đầu).
5. Repost giá vốn (`PostingService.repost`, ADR-025) cho từng chứng từ XK/CK có
   dòng đổi — ranh giới hủy là một chứng từ; báo `sync_cost_posted(posted=…)`
   cho chứng từ nguồn của phiếu sinh (còn dòng chờ giá hay không).
6. Dựng lại `stock_layers` (FIFO/đích danh, cả chi nhánh) và `inventory_
   balances` (mọi kỳ của năm đã tính) — hai bảng dẫn xuất, DELETE + INSERT.

Python không đọc một dòng movement nào ngoài `RETURNING` (id + chứng từ) —
đó là danh sách chứng từ phải repost, không phải dữ liệu để tính (LD-14).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from typing import Any, Final
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select, text
from sqlalchemy.orm import Session

from ket.kernel.config.catalog import MONEY_SCALE_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import CostingNotConvergingError, CostingTouchesLockedPeriodError
from ket.kernel.periods.models import AccountingPeriod, FiscalYear, InventoryValuationMethod
from ket.kernel.protocols import PROVIDERS
from ket.modules.inventory.costing.affected import locked_periods_touched, years_to_process
from ket.modules.inventory.costing.queue import clear_marks, pending_marks
from ket.modules.inventory.models import (
    ISSUE_DOCUMENT_TYPE,
    TRANSFER_DOCUMENT_TYPE,
    CostState,
    InventoryBalance,
    InventoryMovement,
    MovementDirection,
    StockLayer,
)
from ket.modules.inventory.posting_mapper import build_posting_requests
from ket.posting.contracts import PostingService, Voucher

MAX_PASSES: Final[int] = 20
"""Trần số vòng chuyển kho chéo khóa — quyết định user 2026-09-19."""

_SQL_ROOT: Final = resources.files("ket.modules.inventory.costing").joinpath("sql")


def _sql(name: str) -> str:
    return _SQL_ROOT.joinpath(f"{name}.sql").read_text("utf-8")


METHOD_SQL: Final[dict[str, str]] = {
    InventoryValuationMethod.WEIGHTED_AVERAGE_MOVING.value: _sql("wavg_moving"),
    InventoryValuationMethod.WEIGHTED_AVERAGE_PERIOD.value: _sql("wavg_period"),
    InventoryValuationMethod.FIFO.value: _sql("fifo"),
    InventoryValuationMethod.SPECIFIC.value: _sql("specific"),
}
TRANSFER_IN_LEGS_SQL: Final[str] = _sql("transfer_in_legs")
FINALIZE_SQL: Final[str] = _sql("finalize")
MARK_NEXT_YEAR_SQL: Final[str] = _sql("mark_next_year")
REBUILD_BALANCES_SQL: Final[str] = _sql("rebuild_balances")
LAYER_SQL: Final[dict[str, str]] = {
    InventoryValuationMethod.FIFO.value: _sql("stock_layers_fifo"),
    InventoryValuationMethod.SPECIFIC.value: _sql("stock_layers_specific"),
}
_REPOSTED_DOCUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {ISSUE_DOCUMENT_TYPE, TRANSFER_DOCUMENT_TYPE}
)
_SET_BASED_METHODS: Final[frozenset[str]] = frozenset(
    {InventoryValuationMethod.FIFO.value, InventoryValuationMethod.SPECIFIC.value}
)
"""Phương pháp mà câu SQL join hai tập lớn suy từ CTE `keys` (lớp × xuất). Planner
ước lượng CTE ấy 1–2 dòng và chọn nested loop 50k × 50k (spike 8B: hàng chục
phút) — chạy chúng với `enable_nestloop = off` trong phạm vi transaction
(`SET LOCAL`, quyền người dùng thường), bật lại ngay sau. Hai phương pháp bình
quân dùng LATERAL trên chỉ mục khóa — nested loop là đúng cho chúng."""
_NESTLOOP_OFF: Final[str] = "SET LOCAL enable_nestloop = off"
_NESTLOOP_ON: Final[str] = "SET LOCAL enable_nestloop = on"

Progress = Callable[[int, str], None]
"""`(percent, message)` — job nối vào `JobProgress.report`; test truyền no-op."""
CancelCheck = Callable[[], bool]


@dataclass
class CostingRunResult:
    """Số đo một lượt chạy — vào `JobResult` và vào bài đo spike."""

    branch_id: int
    years: list[str] = field(default_factory=list)
    year_ranges: list[tuple[date, date]] = field(default_factory=list)
    """Khoảng ngày của từng năm đã chạy — job dùng để chỉ xóa dấu bẩn nằm trong đó."""
    passes: int = 0
    movements_updated: int = 0
    vouchers_reposted: int = 0
    pending_left: int = 0
    periods_rebuilt: int = 0
    marks_cleared: int = 0

    def as_job_result(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "years": list(self.years),
            "passes": self.passes,
            "movements_updated": self.movements_updated,
            "vouchers_reposted": self.vouchers_reposted,
            "pending_left": self.pending_left,
            "periods_rebuilt": self.periods_rebuilt,
            "marks_cleared": self.marks_cleared,
        }


class CostingCancelled(Exception):  # noqa: N818 — luồng điều khiển, job dịch sang JobCancelled
    """Người dùng hủy — ném ở ranh giới vòng / chứng từ để transaction rollback."""


def recalc_branch(
    session: Session,
    *,
    branch_id: int,
    user_id: int,
    force_from: date | None = None,
    progress: Progress | None = None,
    cancel_requested: CancelCheck | None = None,
) -> CostingRunResult:
    """Một lượt trọn vẹn như job chạy: đọc dấu bẩn (phiên bản), tính, rồi xóa
    đúng những dấu nằm trong năm đã chạy — ép từ một ngày chỉ chạm năm chứa
    ngày ấy, dấu năm khác để nguyên cho lượt sau. Job chỉ bọc thêm advisory
    lock và dịch hủy sang `JobCancelled`; test gọi thẳng hàm này."""
    marks = pending_marks(session, branch_id=branch_id)
    result = run_costing(
        session,
        branch_id=branch_id,
        user_id=user_id,
        force_from=force_from,
        progress=progress,
        cancel_requested=cancel_requested,
    )
    covered = tuple(
        mark
        for mark in marks
        if any(start <= mark.from_date <= end for start, end in result.year_ranges)
    )
    result.marks_cleared = clear_marks(session, covered)
    return result


def run_costing(
    session: Session,
    *,
    branch_id: int,
    user_id: int,
    force_from: date | None = None,
    progress: Progress | None = None,
    cancel_requested: CancelCheck | None = None,
) -> CostingRunResult:
    """Tính giá xuất kho cho mọi việc còn chờ của một chi nhánh (xem docstring
    module). `force_from` ép tính lại mọi khóa từ ngày đó (một năm)."""
    report = progress or (lambda _percent, _message: None)
    cancelled = cancel_requested or (lambda: False)
    result = CostingRunResult(branch_id=branch_id)
    years = years_to_process(session, branch_id=branch_id, force_from=force_from)
    if not years:
        report(100, "Không có khóa tồn kho nào chờ tính giá")
        return result

    scale = _money_scale(session, user_id)
    changed_vouchers: set[UUID] = set()
    for index, year in enumerate(years):
        _refuse_when_locked_touched(session, branch_id=branch_id, year=year, force_from=force_from)
        params = {
            "branch_id": branch_id,
            "year_start": year.start_date,
            "year_end": year.end_date,
            "fiscal_year_id": year.id,
            "force_from": force_from,
            "scale": scale,
        }
        method_sql = METHOD_SQL[year.inventory_valuation_method]
        result.years.append(year.code)
        result.year_ranges.append((year.start_date, year.end_date))
        base = index * 100 // len(years)

        def report_pass(pass_no: int, *, year_code: str = year.code, base: int = base) -> None:
            report(base, f"Năm {year_code}: vòng {pass_no}")

        set_based = year.inventory_valuation_method in _SET_BASED_METHODS
        if set_based:
            session.execute(text(_NESTLOOP_OFF))
        try:
            passes, updated, vouchers = _run_passes(
                session, method_sql, params, report_pass=report_pass, cancelled=cancelled
            )
        finally:
            if set_based:
                session.execute(text(_NESTLOOP_ON))
        result.passes += passes
        result.movements_updated += updated
        changed_vouchers |= vouchers
        session.execute(text(FINALIZE_SQL), params)
        # Khóa còn movement ở năm sau → dấu bẩn đầu năm sau (review 8B M-1),
        # TRƯỚC khi lượt này xóa dấu đã đọc: phiên bản mới nên sống sót.
        session.execute(text(MARK_NEXT_YEAR_SQL), params)

    result.vouchers_reposted = _repost(
        session,
        changed_vouchers,
        user_id=user_id,
        report=report,
        cancelled=cancelled,
    )
    rebuild_layers(session, branch_id=branch_id, years=years)
    for year in years:
        result.periods_rebuilt += rebuild_balances(session, branch_id=branch_id, year=year)
    result.pending_left = int(
        session.scalar(
            select(func.count())
            .select_from(InventoryMovement)
            .where(
                InventoryMovement.branch_id == branch_id,
                InventoryMovement.cost_state != CostState.COSTED,
                InventoryMovement.is_custodial.is_(False),
            )
        )
        or 0
    )
    # Mọi lượt ghi ở trên là SQL thẳng, đi vòng qua identity map của ORM — đối
    # tượng movement/số dư đã nạp trong phiên gọi sẽ nói dối nếu không hết hạn
    # (cùng lý do `movements.reorder_day` gọi `expire_all`).
    session.expire_all()
    report(
        100, f"Đã tính {result.movements_updated} dòng, ghi lại {result.vouchers_reposted} chứng từ"
    )
    return result


def rebuild_layers(session: Session, *, branch_id: int, years: Sequence[FiscalYear]) -> None:
    """`stock_layers` là dẫn xuất của cả chi nhánh: dựng lại theo phương pháp
    của năm **muộn nhất vừa tính** có lớp (FIFO / đích danh) — một câu INSERT,
    không nhân đôi khi hai năm khác phương pháp cùng lượt. Lượt chỉ có việc ở
    năm bình quân **không chạm** bảng (review 8B M-2: xóa rồi không dựng là xóa
    sạch lớp FIFO của năm khác)."""
    layered = [year for year in years if year.inventory_valuation_method in LAYER_SQL]
    if not layered:
        return
    layer_sql = LAYER_SQL[max(layered, key=lambda year: year.start_date).inventory_valuation_method]
    session.execute(delete(StockLayer).where(StockLayer.branch_id == branch_id))
    session.execute(text(_NESTLOOP_OFF))
    try:
        session.execute(text(layer_sql), {"branch_id": branch_id})
    finally:
        session.execute(text(_NESTLOOP_ON))


def rebuild_balances(session: Session, *, branch_id: int, year: FiscalYear) -> int:
    """Dựng lại `inventory_balances` cho mọi kỳ của một năm — cặp DELETE +
    INSERT mỗi kỳ. Trả số kỳ đã dựng."""
    periods = (
        session.execute(
            select(AccountingPeriod)
            .where(AccountingPeriod.fiscal_year_id == year.id)
            .order_by(AccountingPeriod.period_no)
        )
        .scalars()
        .all()
    )
    for period in periods:
        session.execute(
            delete(InventoryBalance).where(
                InventoryBalance.branch_id == branch_id, InventoryBalance.period_id == period.id
            )
        )
        session.execute(
            text(REBUILD_BALANCES_SQL),
            {
                "branch_id": branch_id,
                "period_id": period.id,
                "period_start": period.start_date,
                "period_end": period.end_date,
            },
        )
    return len(periods)


# ------------------------------------------------------------------ nội bộ


def _refuse_when_locked_touched(
    session: Session, *, branch_id: int, year: FiscalYear, force_from: date | None
) -> None:
    locked = locked_periods_touched(session, branch_id=branch_id, year=year, force_from=force_from)
    if locked:
        raise CostingTouchesLockedPeriodError(
            "Tính lại giá xuất kho sẽ ghi vào kỳ đã khóa — mở khóa các kỳ đó trước",
            fiscal_year=year.code,
            periods=",".join(str(item.period_no) for item in locked),
            movements=sum(item.movements for item in locked),
        )


def _run_passes(
    session: Session,
    method_sql: str,
    params: dict[str, Any],
    *,
    report_pass: Callable[[int], None],
    cancelled: CancelCheck,
) -> tuple[int, int, set[UUID]]:
    """Chạy tới điểm bất động; trả `(số vòng, số dòng đổi, chứng từ có dòng đổi)`."""
    vouchers: set[UUID] = set()
    movement_ids: set[int] = set()
    last_changed: list[Any] = []
    for pass_no in range(1, MAX_PASSES + 1):
        if cancelled():
            raise CostingCancelled(f"Người dùng hủy ở vòng {pass_no}")
        report_pass(pass_no)
        last_changed = [
            *_execute_returning(session, method_sql, params),
            *_execute_returning(session, TRANSFER_IN_LEGS_SQL, params),
        ]
        if not last_changed:
            return pass_no, len(movement_ids), vouchers
        # Tập id, không cộng dồn: một dòng đổi ở hai vòng là một dòng đổi.
        movement_ids.update(row.id for row in last_changed)
        vouchers.update(row.voucher_id for row in last_changed)
    still_changing = sorted(
        {f"{row.warehouse_id}:{row.item_id}:{row.lot_key}" for row in last_changed}
    )
    raise CostingNotConvergingError(
        f"Giá chuyển kho không hội tụ sau {MAX_PASSES} vòng — dữ liệu có vòng lặp "
        "chuyển kho chéo, tách ngày các phiếu chuyển rồi chạy lại",
        passes=MAX_PASSES,
        keys=",".join(still_changing[:20]),
    )


def _execute_returning(session: Session, sql: str, params: dict[str, Any]) -> list[Any]:
    result = session.execute(text(sql), params)
    # `RETURNING` biến UPDATE thành một câu trả hàng; `all()` là danh sách dòng đổi.
    return list(result.all()) if isinstance(result, CursorResult) and result.returns_rows else []


REPOST_BATCH: Final[int] = 500
"""Số chứng từ mỗi lô `repost_many` — cũng là ranh giới hủy và báo tiến độ."""


def _repost(
    session: Session,
    voucher_ids: set[UUID],
    *,
    user_id: int,
    report: Progress,
    cancelled: CancelCheck,
) -> int:
    """Ghi lại bút toán giá vốn cho chứng từ XK/CK có dòng đổi giá — theo LÔ:
    mapper đọc cả lô, `PostingService.repost_many` kiểm từng chứng từ nhưng ghi
    một lượt; cờ `cogs_posted` của nguồn đồng bộ theo một câu đếm dòng chờ."""
    if not voucher_ids:
        return 0
    vouchers = (
        session.execute(
            select(Voucher)
            .where(
                Voucher.id.in_(sorted(voucher_ids)),
                Voucher.document_type.in_(sorted(_REPOSTED_DOCUMENT_TYPES)),
            )
            .order_by(Voucher.posting_date, Voucher.voucher_no)
        )
        .scalars()
        .all()
    )
    posting = PostingService(session)
    total = len(vouchers)
    done = 0
    for start in range(0, total, REPOST_BATCH):
        if cancelled():
            raise CostingCancelled(f"Người dùng hủy sau {done}/{total} chứng từ ghi lại")
        batch = vouchers[start : start + REPOST_BATCH]
        ids = [voucher.id for voucher in batch]
        requests = build_posting_requests(session, ids)
        posting.repost_many([requests[voucher_id] for voucher_id in ids], user_id=user_id)
        _sync_source_flags(session, batch)
        done += len(batch)
        report(100 * done // total, f"Ghi lại giá vốn {done}/{total} chứng từ")
    return total


def _sync_source_flags(session: Session, vouchers: Sequence[Voucher]) -> None:
    """Cả hai chiều (review 8B M-4): gỡ lớp nhập duy nhất kéo dòng xuất về chờ
    giá và lượt repost vừa xóa bút toán 632 — cờ phải hạ theo."""
    generated = {
        voucher.id: voucher.source_document_id
        for voucher in vouchers
        if voucher.source_document_id is not None
    }
    if not generated:
        return
    pending = set(
        session.execute(
            select(InventoryMovement.voucher_id)
            .where(
                InventoryMovement.voucher_id.in_(list(generated)),
                InventoryMovement.direction == MovementDirection.OUT,
                InventoryMovement.cost_state != CostState.COSTED,
            )
            .distinct()
        ).scalars()
    )
    for voucher_id, source_id in generated.items():
        for source in PROVIDERS.inventory_line_sources():
            source.sync_cost_posted(session, source_id, posted=voucher_id not in pending)


def _money_scale(session: Session, user_id: int) -> int:
    scale = value_of(session, key=MONEY_SCALE_KEY, user_id=user_id)
    if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
        raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
    return scale
