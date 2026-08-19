"""Cổng khóa / mở kỳ kế toán (RT-09, FR-GLE-030/031, phase-04 bước 14).

Chống write-skew với đường ghi sổ bằng khóa hàng tường minh: lượt khóa cầm
`SELECT … FOR UPDATE` trên dòng kỳ nên nó **chờ** mọi `FOR SHARE` của các lượt
ghi sổ đang chạy (`PostingService._share_lock_period`) commit xong, và lượt ghi
mới không lấy được `FOR SHARE` khi lượt khóa đang giữ dòng. Mọi phép kiểm vì
thế phải nằm **sau** `FOR UPDATE`, trong cùng transaction — kiểm trước đó là
kiểm trên một bản chụp có thể đã cũ.

Ba phép kiểm chặn + một cảnh báo, theo bàn giao 4B/4C→4D:

* **Tuần tự**: khóa kỳ N đòi mọi kỳ trước nó trong niên độ đã khóa; mở kỳ N
  đòi mọi kỳ sau nó đang mở. Recalc viết lại snapshot của cả kỳ đã khóa khi kỳ
  trước nó đổi — chỉ an toàn khi "đã khóa" kéo theo "không còn đường ghi nào
  đổi được kỳ trước".
* **Phạm vi mọi chi nhánh**: hàng đợi tính lại và chứng từ nằm dưới RLS, nên
  người thiếu chi nhánh sẽ thấy hàng đợi của chi nhánh vắng mặt như thể rỗng.
  Kỳ không thuộc chi nhánh nào — khóa nó là chốt số của tất cả chi nhánh.
* **Hàng đợi tính lại rỗng** cho mọi dấu bẩn chạm tới kỳ (cùng niên độ,
  `from_period_no <= N`), nhìn **mọi** chi nhánh nhờ phép kiểm phạm vi ở trên.
* Chứng từ **Đã cất chưa ghi sổ** trong kỳ = cảnh báo, không chặn: chứng từ
  chưa lên sổ không đổi số liệu, nhưng người khóa phải thấy nó (U1/U11).

`DA_KHOA_SO` trên voucher **không bao giờ được ghi** (quyết định 4D cho câu hỏi
bàn giao 4B): nguồn sự thật là `accounting_periods.locked_at`; UPDATE hàng loạt
trạng thái voucher lúc khóa vừa O(n) vừa đổ một trận diff vô nghĩa vào
`audit_log`, và hai nguồn sự thật thì một trong hai sẽ nói dối. Trạng thái
"thuộc kỳ đã khóa" là dữ liệu **suy ra** lúc đọc.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.errors import (
    PeriodClosedError,
    PeriodLockChecklistError,
    PeriodLockScopeError,
    PeriodLockSequenceError,
    PeriodNotFoundError,
    PeriodNotLockedError,
    PeriodRecalcPendingError,
)
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.kernel.periods.service import PeriodService
from ket.kernel.persistence.unit_of_work import RequestScope
from ket.kernel.security.models import Branch
from ket.posting.balances.models import BalanceRecalcQueue
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.opening_balances.models import OpeningBalance

PERIOD_ENTITY_TYPE = AccountingPeriod.__tablename__

UNPOSTED_SAMPLE_LIMIT = 5
"""Số chứng từ Đã cất nêu đích danh trong cảnh báo — đủ để lần tới chỗ sửa,
không biến thân response thành một trang danh sách (danh sách đầy đủ nằm ở
`GET /vouchers?status=1&period_id=…`)."""


@dataclass(frozen=True)
class LockWarning:
    """Một việc còn dở mà lượt khóa **cho qua** nhưng phải nói ra."""

    code: str
    message: str
    count: int
    sample: tuple[str, ...]


@dataclass(frozen=True)
class LockOutcome:
    period: AccountingPeriod
    warnings: tuple[LockWarning, ...]


class PeriodLockService:
    """Khóa/mở kỳ trong transaction đang mở của người gọi — không tự commit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock(self, period_id: int, *, scope: RequestScope) -> LockOutcome:
        period, year = self._lock_period_row(period_id)
        if year.is_closed:
            raise PeriodClosedError(
                "Năm tài chính đã quyết toán — không động vào kỳ của năm này",
                period_id=period.id,
                fiscal_year_id=year.id,
            )
        if period.locked_at is not None:
            raise PeriodClosedError(
                "Kỳ này đã khóa rồi — muốn đổi thì mở lại trước",
                period_id=period.id,
                locked_at=period.locked_at.isoformat(),
                locked_by=period.locked_by,
            )

        self._ensure_earlier_periods_locked(period, year)
        self._ensure_scope_covers_every_branch(scope)
        self._ensure_recalc_queue_clear(period, year)
        self._ensure_opening_balances_balanced(period, year)
        warnings = self._unposted_voucher_warnings(period)

        # Đóng dấu qua `PeriodService` để "ai khóa, lúc nào" chỉ có một cách
        # ghi; dòng kỳ đã nằm dưới `FOR UPDATE` của chính transaction này nên
        # lượt đọc lại bên trong không chờ ai.
        PeriodService(self._session).lock_period(period.id, user_id=scope.user_id)

        # Diff `locked_at`/`locked_by` đã có vết qua mixin `Audited`; dòng
        # LOCKED tường minh là hợp đồng tra cứu của FR-GLE-030 — "mọi lần
        # khóa/mở của kỳ này" phải là một phép lọc theo `action`, không phải
        # một phép đoán trên diff UPDATE.
        record_action(
            self._session,
            entity_type=PERIOD_ENTITY_TYPE,
            entity_id=str(period.id),
            action=AuditAction.LOCKED,
            new_values={
                "fiscal_year_id": year.id,
                "period_no": period.period_no,
                "unposted_vouchers": warnings[0].count if warnings else 0,
            },
        )
        return LockOutcome(period=period, warnings=warnings)

    def unlock(self, period_id: int, *, scope: RequestScope, reason: str) -> AccountingPeriod:
        """Mở lại kỳ đã khóa — quyền riêng, lý do bắt buộc (FR-GLE-031).

        Lý do trống là lỗi lập trình ở đây (schema API đã chặn 422 trước khi
        tới service); `ValueError` để nó nổ ở CI chứ không thành thông điệp
        dịu.
        """
        if not reason.strip():
            raise ValueError("Lý do mở kỳ không được để trống")

        period, year = self._lock_period_row(period_id)
        if year.is_closed:
            raise PeriodClosedError(
                "Năm tài chính đã quyết toán — phải mở lại năm trước khi động vào kỳ",
                period_id=period.id,
                fiscal_year_id=year.id,
            )
        if period.locked_at is None:
            raise PeriodNotLockedError(
                "Kỳ này đang mở — không có gì để mở khóa",
                period_id=period.id,
                period_no=period.period_no,
            )
        self._ensure_later_periods_open(period, year)

        PeriodService(self._session).unlock_period(period.id)
        record_action(
            self._session,
            entity_type=PERIOD_ENTITY_TYPE,
            entity_id=str(period.id),
            action=AuditAction.UNLOCKED,
            new_values={
                "fiscal_year_id": year.id,
                "period_no": period.period_no,
                "reason": reason,
            },
        )
        return period

    def _lock_period_row(self, period_id: int) -> tuple[AccountingPeriod, FiscalYear]:
        """`FOR UPDATE` trên dòng NĂM rồi dòng KỲ — điểm tựa của toàn bộ RT-09.

        Vì sao phải khóa cả dòng năm (review 4D, H-1): gate tuần tự đọc các
        **hàng kỳ khác** bằng SELECT thường, nên hai thao tác trên hai kỳ khác
        nhau — `lock(p2)` và `unlock(p1)` — không đụng hàng của nhau và cùng
        commit trót lọt, để lại "p1 mở, p2 khóa": đúng bất biến mà gate sinh ra
        để giữ. Cầm `FOR UPDATE` dòng `fiscal_years` tuần tự hóa mọi lượt
        khóa/mở trong một niên độ, và gate của người đến sau nhìn thấy kết quả
        đã commit của người đến trước. Khóa năm TRƯỚC, kỳ SAU — mọi lượt cùng
        thứ tự nên không có deadlock giữa hai lượt khóa/mở; đường ghi sổ chỉ
        `FOR SHARE` hàng kỳ, không chạm hàng năm.

        Chờ chứ không `NOWAIT`: lượt khóa đến sau vài lượt ghi sổ đang chạy là
        tình huống bình thường, và ý nghĩa của nó chính là "chờ họ xong rồi
        chốt" — trả lỗi bắt người dùng bấm lại là đổi một phép chờ ngắn lấy
        một vòng thử-lại thủ công.
        """
        fiscal_year_id = self._session.execute(
            select(AccountingPeriod.fiscal_year_id).where(AccountingPeriod.id == period_id)
        ).scalar_one_or_none()
        if fiscal_year_id is None:
            raise PeriodNotFoundError("Không tìm thấy kỳ kế toán", period_id=period_id)
        year = self._session.execute(
            select(FiscalYear).where(FiscalYear.id == fiscal_year_id).with_for_update()
        ).scalar_one()
        period = self._session.execute(
            select(AccountingPeriod).where(AccountingPeriod.id == period_id).with_for_update()
        ).scalar_one()
        return period, year

    def _ensure_earlier_periods_locked(self, period: AccountingPeriod, year: FiscalYear) -> None:
        open_before = (
            self._session.execute(
                select(AccountingPeriod.period_no)
                .where(AccountingPeriod.fiscal_year_id == year.id)
                .where(AccountingPeriod.period_no < period.period_no)
                .where(AccountingPeriod.locked_at.is_(None))
                .order_by(AccountingPeriod.period_no)
            )
            .scalars()
            .all()
        )
        if open_before:
            raise PeriodLockSequenceError(
                "Khóa sổ phải tuần tự — còn kỳ trước đó đang mở",
                period_no=period.period_no,
                open_period_nos=",".join(str(no) for no in open_before),
            )

    def _ensure_later_periods_open(self, period: AccountingPeriod, year: FiscalYear) -> None:
        locked_after = (
            self._session.execute(
                select(AccountingPeriod.period_no)
                .where(AccountingPeriod.fiscal_year_id == year.id)
                .where(AccountingPeriod.period_no > period.period_no)
                .where(AccountingPeriod.locked_at.is_not(None))
                .order_by(AccountingPeriod.period_no)
            )
            .scalars()
            .all()
        )
        if locked_after:
            raise PeriodLockSequenceError(
                "Mở khóa phải tuần tự từ kỳ muộn nhất — còn kỳ sau đó đang khóa",
                period_no=period.period_no,
                locked_period_nos=",".join(str(no) for no in locked_after),
            )

    def _ensure_scope_covers_every_branch(self, scope: RequestScope) -> None:
        """Phạm vi request phải phủ **mọi** chi nhánh, kể cả ngừng hoạt động.

        Chi nhánh ngừng hoạt động vẫn có phát sinh lịch sử và có thể còn dấu
        bẩn trong hàng đợi — loại nó khỏi phép kiểm là mở lại đúng lỗ hổng mà
        phép kiểm này sinh ra để bịt. `branches` không bật RLS (xem
        `security/models.py`) nên câu đếm này thấy đủ.
        """
        all_branch_ids = set(self._session.execute(select(Branch.id)).scalars().all())
        missing = all_branch_ids - set(scope.branch_ids)
        if missing:
            raise PeriodLockScopeError(
                "Khóa sổ cần quyền trên mọi chi nhánh — phạm vi hiện tại còn thiếu",
                missing_branch_ids=",".join(str(b) for b in sorted(missing)),
            )

    def _ensure_recalc_queue_clear(self, period: AccountingPeriod, year: FiscalYear) -> None:
        """Không dấu bẩn nào (mọi sổ, mọi chi nhánh) được chạm tới kỳ sắp khóa.

        "Chạm tới" = cùng niên độ và `from_period_no <= N`: lượt tính lại chạy
        từ `from_period` tới hết năm nên một dấu ở kỳ sớm hơn sẽ viết lại
        snapshot của chính kỳ này sau khi khóa. Dấu ở kỳ **muộn hơn** thì
        không — khóa kỳ 3 khi kỳ 5 còn dấu bẩn là hợp lệ.
        """
        blockers = self._session.execute(
            select(
                BalanceRecalcQueue.ledger,
                BalanceRecalcQueue.branch_id,
                AccountingPeriod.period_no,
            )
            .join(AccountingPeriod, AccountingPeriod.id == BalanceRecalcQueue.from_period_id)
            .where(AccountingPeriod.fiscal_year_id == year.id)
            .where(AccountingPeriod.period_no <= period.period_no)
        ).all()
        if blockers:
            branch_ids = sorted({row.branch_id for row in blockers})
            raise PeriodRecalcPendingError(
                "Hàng đợi tính lại số dư còn dấu bẩn chạm tới kỳ này — "
                "chạy tính lại số dư cho các chi nhánh nêu trong chi tiết rồi khóa",
                period_no=period.period_no,
                pending_marks=len(blockers),
                branch_ids=",".join(str(b) for b in branch_ids),
            )

    def _ensure_opening_balances_balanced(self, period: AccountingPeriod, year: FiscalYear) -> None:
        """Mục chặn duy nhất của danh mục kiểm tra ở phase 4 (U11, BR-OPB-01).

        Chỉ kiểm khi khóa **kỳ đầu năm**: số dư ban đầu bất động ngay khi kỳ
        đầu năm khóa (`guard_opening_writable` giữ FOR SHARE trên chính hàng
        kỳ đó), nên cân ở thời điểm này là cân mãi mãi — kiểm lại ở kỳ 2..13
        chỉ tốn một câu SUM cho một sự thật đã chốt. 4C ghi lệch là *cảnh
        báo* đúng chữ FR-OPB-006; chặn cứng được hoãn tới đây.

        Cân theo **từng (sổ, chi nhánh)** chứ không gộp toàn dữ liệu (review
        4D, H-2): lượt nhập 4C cảnh báo per-branch và check nền
        `opening_balance_balanced.sql` cũng đo per-branch — cổng khóa mà gộp
        thì hai chi nhánh lệch bù trừ nhau sẽ lọt qua, số dư bị guard đóng
        băng ngay sau khi khóa, và bộ kiểm toàn vẹn đỏ vĩnh viễn trên một năm
        không còn sửa được.
        """
        first_period_no = self._session.execute(
            select(func.min(AccountingPeriod.period_no)).where(
                AccountingPeriod.fiscal_year_id == year.id
            )
        ).scalar_one()
        if period.period_no != first_period_no:
            return
        imbalances = self._session.execute(
            select(
                OpeningBalance.ledger,
                OpeningBalance.branch_id,
                func.sum(OpeningBalance.debit).label("debit"),
                func.sum(OpeningBalance.credit).label("credit"),
            )
            .where(OpeningBalance.fiscal_year_id == year.id)
            .group_by(OpeningBalance.ledger, OpeningBalance.branch_id)
            .having(func.sum(OpeningBalance.debit) != func.sum(OpeningBalance.credit))
        ).all()
        if imbalances:
            worst = imbalances[0]
            raise PeriodLockChecklistError(
                "Số dư ban đầu của năm chưa cân Nợ/Có — sửa số dư ban đầu rồi khóa kỳ đầu năm",
                check="opening_balance_balanced",
                ledger=worst.ledger,
                branch_id=worst.branch_id,
                imbalanced_groups=len(imbalances),
                total_debit=str(worst.debit),
                total_credit=str(worst.credit),
            )

    def _unposted_voucher_warnings(self, period: AccountingPeriod) -> tuple[LockWarning, ...]:
        count = self._session.execute(
            select(func.count())
            .select_from(Voucher)
            .where(Voucher.period_id == period.id)
            .where(Voucher.status == VoucherStatus.DA_CAT)
        ).scalar_one()
        if count == 0:
            return ()
        sample = (
            self._session.execute(
                select(Voucher.voucher_no)
                .where(Voucher.period_id == period.id)
                .where(Voucher.status == VoucherStatus.DA_CAT)
                .order_by(Voucher.posting_date, Voucher.voucher_no)
                .limit(UNPOSTED_SAMPLE_LIMIT)
            )
            .scalars()
            .all()
        )
        return (
            LockWarning(
                code="vouchers.unposted",
                message=(
                    "Trong kỳ còn chứng từ Đã cất chưa ghi sổ — chúng sẽ không lên "
                    "báo cáo và không ghi sổ được nữa cho tới khi mở lại kỳ"
                ),
                count=count,
                sample=tuple(sample),
            ),
        )
