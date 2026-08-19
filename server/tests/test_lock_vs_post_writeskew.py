"""Write-skew ghi-sổ ↔ khóa-sổ (RT-09, phase-04 bước 18) — hai kết nối thật.

Ba mặt của cùng một cơ chế, mỗi mặt một test tất định + một vòng đua lặp:

1. Lượt ghi sổ đang giữ `FOR SHARE` trên hàng kỳ → lượt khóa (`FOR UPDATE
   NOWAIT`) phải bị chặn — cửa sổ "kiểm xong nhưng chưa ghi xong" không tồn
   tại với lệnh khóa.
2. Kỳ đã khóa → ghi sổ bị từ chối `period.locked` (tầng service; tầng DB đã có
   test trigger riêng ở `test_posting_engine_flow.py`).
3. Lượt ghi commit TRƯỚC khi lượt khóa bắt đầu → lượt khóa vấp dấu bẩn trong
   hàng đợi (`period.recalc_pending`). Hệ quả gộp: **không bao giờ cả hai cùng
   thắng** — bất biến của vòng đua lặp ở cuối.
"""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    PeriodLockSequenceError,
    PeriodRecalcPendingError,
    PostingValidationError,
)
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.engine import service as engine_service
from ket.posting.periods.lock_service import PeriodLockService
from posting_support import PostingContext, posting_scope, seed_posting_context
from test_period_lock_service import (
    ACTOR_ID,
    clear_queue_of_year,
    full_scope,
    make_year,
    period_of,
)

pytestmark = pytest.mark.db

RACE_ROUNDS = 5


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


def _journal_payload(context: PostingContext, posting_date: date) -> JournalVoucherIn:
    return JournalVoucherIn(
        branch_id=context.branch_id,
        document_date=posting_date,
        posting_date=posting_date,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description="đua ghi sổ với khóa sổ",
        lines=(
            JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(10_000)),
            JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(10_000)),
        ),
    )


def _create_saved_voucher(
    session_factory: sessionmaker[Session],
    scope: RequestScope,
    context: PostingContext,
    posting_date: date,
) -> object:
    with unit_of_work(session_factory, scope) as session:
        voucher = JournalVoucherService(session).create(
            _journal_payload(context, posting_date), user_id=ACTOR_ID
        )
        return voucher.id


def test_posting_in_flight_holds_the_period_row_against_for_update(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mặt 1: `FOR SHARE` của lượt ghi đang chạy chặn `FOR UPDATE` của lượt khóa.

    Gate đặt tại `mark_dirty` của posting engine — thời điểm đó transaction ghi
    đã cầm FOR SHARE trên hàng kỳ và đã INSERT `gl_postings`, tức là đúng giữa
    cửa sổ mà write-skew cần chen vào.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        year_id = make_year(session, code="SKEW-GATE", start=date(2081, 1, 1))
        period_id = period_of(session, year_id, 1).id
    voucher_id = _create_saved_voucher(session_factory, scope, context, date(2081, 1, 15))

    in_post = threading.Event()
    release = threading.Event()
    real_mark_dirty = engine_service.mark_dirty

    def gated_mark_dirty(*args: object, **kwargs: object) -> None:
        in_post.set()
        assert release.wait(timeout=30), "test không nhả gate"
        real_mark_dirty(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_service, "mark_dirty", gated_mark_dirty)

    outcome: dict[str, object] = {}

    def poster() -> None:
        try:
            with unit_of_work(session_factory, scope) as session:
                JournalVoucherService(session).post(voucher_id, user_id=ACTOR_ID)  # type: ignore[arg-type]
        except Exception as error:  # pragma: no cover - chỉ chạm khi test đổ
            outcome["error"] = error

    thread = threading.Thread(target=poster)
    thread.start()
    try:
        assert in_post.wait(timeout=30), f"lượt ghi không tới gate: {outcome.get('error')}"
        with pytest.raises(OperationalError):
            with unit_of_work(session_factory, scope) as session:
                session.execute(
                    sql_text(
                        "SELECT id FROM accounting_periods WHERE id = :period FOR UPDATE NOWAIT"
                    ),
                    {"period": period_id},
                )
    finally:
        release.set()
        thread.join(timeout=30)
    assert "error" not in outcome, outcome.get("error")


def test_locked_period_rejects_posting_and_committed_posting_blocks_lock(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Mặt 2 + 3, tất định: mỗi thứ tự kết thúc đều chặn đúng phía còn lại."""
    wide = full_scope(session_factory, dataset_alpha, context)

    # Thứ tự A — ghi commit trước: lượt khóa phải vấp dấu bẩn.
    with unit_of_work(session_factory, wide) as session:
        year_id = make_year(session, code="SKEW-ORDER", start=date(2082, 1, 1))
        service = JournalVoucherService(session)
        voucher = service.create(_journal_payload(context, date(2082, 1, 15)), user_id=ACTOR_ID)
        service.post(voucher.id, user_id=ACTOR_ID)
    with unit_of_work(session_factory, wide) as session:
        with pytest.raises(PeriodRecalcPendingError):
            PeriodLockService(session).lock(period_of(session, year_id, 1).id, scope=wide)

    # Thứ tự B — khóa trước: lượt ghi phải bị `period.locked`. Chứng từ phải
    # Cất TRƯỚC khi khóa (kỳ khóa từ chối cả việc cất chứng từ mới vào nó —
    # `VoucherService._resolve_open_period`), nên ở đây chỉ còn đường `post`.
    blocked_id = _create_saved_voucher(session_factory, wide, context, date(2082, 1, 20))
    with unit_of_work(session_factory, wide) as session:
        clear_queue_of_year(session, year_id)
        PeriodLockService(session).lock(period_of(session, year_id, 1).id, scope=wide)
    saved_id = _create_saved_voucher(session_factory, wide, context, date(2082, 2, 10))
    with unit_of_work(session_factory, wide) as session:
        # Chứng từ tháng 2 (kỳ 2 chưa khóa) ghi được — khóa kỳ 1 không lan.
        JournalVoucherService(session).post(saved_id, user_id=ACTOR_ID)  # type: ignore[arg-type]
    with pytest.raises(PostingValidationError) as caught:
        with unit_of_work(session_factory, wide) as session:
            JournalVoucherService(session).post(blocked_id, user_id=ACTOR_ID)  # type: ignore[arg-type]
    assert any(v.code == "period.locked" for v in caught.value.violations)


def test_lock_and_unlock_on_different_periods_serialise_on_the_year_row(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Write-skew lock↔unlock (review 4D, H-1): `lock(p2)` và `unlock(p1)` chạy
    song song từng để lại "p1 mở + p2 khóa" — mỗi bên chỉ khóa hàng kỳ của
    mình còn gate tuần tự đọc hàng kia bằng SELECT thường. Sửa bằng
    `FOR UPDATE` dòng NĂM ở cả hai đường; test chứng minh hai điều:

    1. giữa transaction `lock(p2)`, dòng `fiscal_years` bị giữ `FOR UPDATE`
       (probe `NOWAIT` đổ lỗi) — lượt `unlock(p1)` đến sau phải chờ;
    2. sau khi `lock(p2)` commit, `unlock(p1)` bị gate tuần tự từ chối —
       trạng thái "p1 mở + p2 khóa" không dựng ra được nữa.
    """
    wide = full_scope(session_factory, dataset_alpha, context)
    with unit_of_work(session_factory, wide) as session:
        year_id = make_year(session, code="SKEW-YEARLOCK", start=date(2083, 1, 1))
        p1_id = period_of(session, year_id, 1).id
        p2_id = period_of(session, year_id, 2).id
        PeriodLockService(session).lock(p1_id, scope=wide)

    with unit_of_work(session_factory, wide) as session:
        PeriodLockService(session).lock(p2_id, scope=wide)
        # Transaction khóa còn mở — dòng năm phải đang bị giữ FOR UPDATE.
        with pytest.raises(OperationalError):
            with unit_of_work(session_factory, wide) as probe:
                probe.execute(
                    sql_text("SELECT id FROM fiscal_years WHERE id = :year FOR UPDATE NOWAIT"),
                    {"year": year_id},
                )

    with pytest.raises(PeriodLockSequenceError):
        with unit_of_work(session_factory, wide) as session:
            PeriodLockService(session).unlock(p1_id, scope=wide, reason="thử mở giữa chừng")


def test_race_never_lets_both_sides_win(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Vòng đua thật, lặp nhiều lần (success criteria phase-04): sau mỗi vòng
    hoặc chứng từ đã ghi sổ và kỳ KHÔNG khóa được (dấu bẩn), hoặc kỳ khóa và
    chứng từ KHÔNG ghi được — không tồn tại "vừa ghi lọt vừa khóa xong"."""
    wide = full_scope(session_factory, dataset_alpha, context)

    for round_no in range(RACE_ROUNDS):
        with unit_of_work(session_factory, wide) as session:
            year_id = make_year(
                session, code=f"SKEW-RACE-{round_no}", start=date(2090 + round_no, 1, 1)
            )
            period_id = period_of(session, year_id, 1).id
        voucher_id = _create_saved_voucher(
            session_factory, wide, context, date(2090 + round_no, 1, 15)
        )

        barrier = threading.Barrier(2, timeout=30)
        results: dict[str, object] = {}

        def poster(
            gate: threading.Barrier = barrier,
            target: object = voucher_id,
            sink: dict[str, object] = results,
        ) -> None:
            try:
                gate.wait()
                with unit_of_work(session_factory, wide) as session:
                    JournalVoucherService(session).post(target, user_id=ACTOR_ID)  # type: ignore[arg-type]
                sink["posted"] = True
            except PostingValidationError as error:
                sink["post_error"] = error

        def locker(
            gate: threading.Barrier = barrier,
            target: int = period_id,
            sink: dict[str, object] = results,
        ) -> None:
            try:
                gate.wait()
                with unit_of_work(session_factory, wide) as session:
                    PeriodLockService(session).lock(target, scope=wide)
                sink["locked"] = True
            except PeriodRecalcPendingError as error:
                sink["lock_error"] = error

        threads = (threading.Thread(target=poster), threading.Thread(target=locker))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        posted = results.get("posted", False)
        locked = results.get("locked", False)
        assert posted != locked, f"vòng {round_no}: cả hai cùng thắng/thua — {results}"

        with unit_of_work(session_factory, wide) as session:
            voucher = session.get(Voucher, voucher_id)
            assert voucher is not None
            if posted:
                assert VoucherStatus(voucher.status) is VoucherStatus.DA_GHI_SO
                assert isinstance(results["lock_error"], PeriodRecalcPendingError)
            else:
                assert VoucherStatus(voucher.status) is VoucherStatus.DA_CAT
                locked_at = session.execute(
                    select(AccountingPeriod.locked_at).where(AccountingPeriod.id == period_id)
                ).scalar_one()
                assert locked_at is not None
