"""Tạo năm tài chính, tra kỳ theo ngày, chặn ghi vào kỳ đã khóa.

Ba việc, và việc thứ ba là việc quan trọng nhất của cả module: `ensure_open` là
cổng mà **mọi** đường ghi chứng từ phải đi qua (phase 4 trở đi). Kỳ đã khóa là
cam kết với cơ quan thuế rằng số của kỳ đó đã cố định — một chứng từ lọt vào sau
đó làm báo cáo đã nộp không còn khớp với sổ.

**Giới hạn có chủ đích của lát này:** `ensure_open` chỉ *đọc* trạng thái khóa.
Nó chưa chống được tình huống đua thật — người A ghi chứng từ trong khi người B
bấm khóa kỳ, cả hai cùng thành công vì mỗi bên đọc trạng thái trước khi bên kia
ghi (write-skew, RT-09). Lời giải là `SELECT … FOR SHARE` trên dòng kỳ ở đường
ghi sổ và `FOR UPDATE` + kiểm lại ở đường khóa kỳ; nó thuộc phase 4 vì chỉ ở đó
mới có transaction ghi sổ thật để đặt khóa vào. Ghi ra đây để phase 4 không phải
phát hiện lại.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.errors import (
    AccountingSchemeLockedError,
    FiscalYearOverlapError,
    PeriodClosedError,
    PeriodNotFoundError,
)
from ket.kernel.periods.models import (
    PERIODS_PER_YEAR,
    AccountingPeriod,
    AccountingScheme,
    FiscalYear,
    InventoryValuationMethod,
    VatMethod,
)

_ONE_DAY = timedelta(days=1)

OVERLAP_CONSTRAINT = "ex_fiscal_years_no_overlap"
"""Ràng buộc `EXCLUDE` chống chồng lấn niên độ, tạo ở migration `0002`."""


def fiscal_year_covering(session: Session, on_date: date) -> FiscalYear | None:
    """Năm tài chính phủ một ngày — `None` khi chưa có năm nào.

    Tách ra ở lát 6A vì đã có người dùng thứ hai (`/auto-posting/operations`
    cần chế độ kế toán của năm, cùng câu hỏi mà `/accounts` hỏi từ lát 4E).
    `ORDER BY start_date DESC` để hai năm chồng lấn (bất biến app canh — nếu
    vỡ) vẫn cho kết quả xác định: lấy năm bắt đầu muộn nhất phủ ngày này.
    """
    return session.execute(
        select(FiscalYear)
        .where(FiscalYear.start_date <= on_date)
        .where(FiscalYear.end_date >= on_date)
        .order_by(FiscalYear.start_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _violates(error: IntegrityError, constraint: str) -> bool:
    """Lỗi ràng buộc này có phải của đúng ràng buộc đang quan tâm không.

    Đọc `diag.constraint_name` chứ không dò chuỗi thông điệp — thông điệp của
    PostgreSQL đổi theo phiên bản và theo `lc_messages` của cụm.
    """
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None) == constraint


def _add_months(anchor: date, months: int) -> date:
    """Cùng ngày trong tháng, `months` tháng sau — không cần thư viện ngoài.

    Chỉ dùng cho **ngày đầu kỳ** (ngày 1), nên không phải xử lý ca 31/01 + 1
    tháng. Ràng buộc đó được giữ bằng chính chỗ gọi: kỳ luôn bắt đầu từ ngày đầu
    tháng của ngày bắt đầu niên độ.
    """
    zero_based = anchor.month - 1 + months
    return date(anchor.year + zero_based // 12, zero_based % 12 + 1, anchor.day)


class PeriodService:
    """Kỳ và năm tài chính trong transaction đang mở của người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_fiscal_year(
        self,
        *,
        code: str,
        start_date: date,
        accounting_scheme: AccountingScheme,
        base_currency: str,
        inventory_valuation_method: InventoryValuationMethod,
        vat_method: VatMethod,
    ) -> FiscalYear:
        """Tạo niên độ 12 tháng kể từ `start_date` và sinh đủ 12 kỳ.

        Sinh kỳ **cùng lúc** với năm chứ không để người dùng khai tay từng tháng:
        một năm thiếu tháng 7 chỉ lộ ra vào ngày đầu tiên có người nhập chứng từ
        tháng 7, và lúc đó thông điệp "không tìm thấy kỳ" trông như một lỗi hệ
        thống chứ không như một việc còn bỏ dở.

        Niên độ lệch năm dương lịch (bắt đầu 01/04) được hỗ trợ sẵn — đó là lý do
        tham số là `start_date` chứ không phải một con số năm.
        """
        if start_date.day != 1:
            raise ValueError(f"Niên độ phải bắt đầu từ ngày đầu tháng, nhận {start_date}")

        end_date = _add_months(start_date, PERIODS_PER_YEAR) - _ONE_DAY
        self._ensure_no_overlap(start_date=start_date, end_date=end_date)

        year = FiscalYear(
            code=code,
            start_date=start_date,
            end_date=end_date,
            accounting_scheme=accounting_scheme.value,
            base_currency=base_currency,
            inventory_valuation_method=inventory_valuation_method.value,
            vat_method=vat_method.value,
        )
        # Savepoint vì ràng buộc `EXCLUDE` của DB mới là hàng rào thật (H1): phép
        # kiểm ở trên chỉ thấy những năm đã commit, nên hai phiên song song vẫn
        # cùng lọt qua nó. Không có điểm quay lui thì `IntegrityError` làm hỏng
        # cả transaction và mọi lệnh sau đó đổ theo — cùng lý do như
        # `attachments/service.py`.
        try:
            with self._session.begin_nested():
                self._session.add(year)
                self._session.flush()
        except IntegrityError as conflict:
            if _violates(conflict, OVERLAP_CONSTRAINT):
                raise FiscalYearOverlapError(
                    "Khoảng thời gian này đã thuộc một năm tài chính khác",
                    code=code,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                ) from conflict
            raise

        for offset in range(PERIODS_PER_YEAR):
            period_start = _add_months(start_date, offset)
            period_end = _add_months(start_date, offset + 1) - _ONE_DAY
            self._session.add(
                AccountingPeriod(
                    fiscal_year_id=year.id,
                    period_no=offset + 1,
                    start_date=period_start,
                    end_date=period_end,
                )
            )
        self._session.flush()
        return year

    def periods_of(self, fiscal_year_id: int) -> Sequence[AccountingPeriod]:
        return (
            self._session.execute(
                select(AccountingPeriod)
                .where(AccountingPeriod.fiscal_year_id == fiscal_year_id)
                .order_by(AccountingPeriod.period_no)
            )
            .scalars()
            .all()
        )

    def resolve_period(self, posting_date: date) -> AccountingPeriod:
        """Kỳ chứa ngày hạch toán này.

        Kỳ 13 (điều chỉnh) trùng ngày với kỳ 12 nên phải loại khỏi phép tra tự
        động: bút toán kết chuyển đi vào kỳ 13 là một **lựa chọn** của người
        khóa sổ, không phải hệ quả của ngày ghi trên chứng từ.
        """
        period = (
            self._session.execute(
                select(AccountingPeriod)
                .where(AccountingPeriod.start_date <= posting_date)
                .where(AccountingPeriod.end_date >= posting_date)
                .where(AccountingPeriod.period_no <= PERIODS_PER_YEAR)
                .order_by(AccountingPeriod.period_no)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if period is None:
            raise PeriodNotFoundError(
                "Ngày hạch toán không thuộc kỳ kế toán nào — hãy tạo năm tài chính chứa ngày này",
                posting_date=posting_date.isoformat(),
            )
        return period

    def ensure_open(self, period: AccountingPeriod) -> None:
        """Chặn mọi thao tác ghi vào kỳ đã khóa hoặc năm đã quyết toán."""
        if period.locked_at is not None:
            raise PeriodClosedError(
                "Kỳ kế toán đã khóa sổ — muốn sửa thì phải mở lại kỳ",
                period_id=period.id,
                locked_at=period.locked_at.isoformat(),
            )
        year = self._session.get(FiscalYear, period.fiscal_year_id)
        if year is not None and year.is_closed:
            raise PeriodClosedError(
                "Năm tài chính đã quyết toán — không ghi thêm vào năm này",
                period_id=period.id,
                fiscal_year_id=year.id,
            )

    def lock_period(self, period_id: int, *, user_id: int) -> AccountingPeriod:
        """Khóa một kỳ, ghi lại ai khóa và lúc nào (FR-NFR-013).

        Từ chối khóa một kỳ **đã khóa** (sửa sau review C2): khóa đè lên vết cũ
        làm mất câu trả lời "ai khóa kỳ này" — đúng thứ duy nhất cột
        `locked_by` sinh ra để giữ. Muốn đổi người khóa thì mở rồi khóa lại, và
        mỗi bước để lại một dòng nhật ký riêng.

        Danh mục kiểm tra trước khi khóa (số dư khớp, hàng đợi tính lại rỗng —
        nguyên tắc U11) thuộc phase 10a: ở phase này chưa có chứng từ nào để
        kiểm. Cơ chế ghi vết thì có ngay từ đây vì nó là **schema**, không phải
        quy trình.
        """
        period = self._require_period(period_id)
        self._ensure_year_open(period)
        if period.locked_at is not None:
            raise PeriodClosedError(
                "Kỳ này đã khóa rồi — muốn đổi thì mở lại trước",
                period_id=period_id,
                locked_at=period.locked_at.isoformat(),
                locked_by=period.locked_by,
            )
        period.locked_at = datetime.now(UTC)
        period.locked_by = user_id
        self._session.flush()
        return period

    def unlock_period(self, period_id: int) -> AccountingPeriod:
        """Mở lại kỳ đã khóa. Vết mở nằm ở `audit_log` (mixin `Audited`).

        Chặn khi **năm** đã quyết toán (sửa sau review C2): quyết toán năm là
        cam kết với cơ quan thuế rằng số của cả niên độ đã cố định, nên mở một
        kỳ con của nó là đi vòng qua chính cam kết đó. Muốn sửa thì phải mở năm
        trước — một thao tác riêng, có vết riêng, ở cấp có thẩm quyền riêng.
        """
        period = self._require_period(period_id)
        self._ensure_year_open(period)
        period.locked_at = None
        period.locked_by = None
        self._session.flush()
        return period

    def _require_period(self, period_id: int) -> AccountingPeriod:
        period = self._session.get(AccountingPeriod, period_id)
        if period is None:
            raise PeriodNotFoundError("Không tìm thấy kỳ kế toán", period_id=period_id)
        return period

    def _ensure_year_open(self, period: AccountingPeriod) -> None:
        """Năm đã quyết toán thì mọi thao tác khóa/mở kỳ con đều bị chặn."""
        year = self._session.get(FiscalYear, period.fiscal_year_id)
        if year is not None and year.is_closed:
            raise PeriodClosedError(
                "Năm tài chính đã quyết toán — phải mở lại năm trước khi động vào kỳ",
                period_id=period.id,
                fiscal_year_id=year.id,
            )

    def ensure_scheme_changeable(self, fiscal_year_id: int, *, has_documents: bool) -> None:
        """Chặn đổi chế độ kế toán khi năm đã phát sinh chứng từ (FR-SYS-004, U14).

        `has_documents` do nơi gọi truyền vào chứ không tự đếm ở đây: kernel
        **không được** biết bảng chứng từ nào tồn tại (luật phụ thuộc #5) — và
        tới phase 9 thì "đã phát sinh" là câu hỏi trên hàng chục bảng thuộc hàng
        chục module. Cùng lý do khiến `master_data_usage` là bộ đếm chứ không
        phải một phép quét.
        """
        if has_documents:
            raise AccountingSchemeLockedError(
                "Năm tài chính đã có chứng từ nên không đổi được chế độ kế toán",
                fiscal_year_id=fiscal_year_id,
            )

    def _ensure_no_overlap(self, *, start_date: date, end_date: date) -> None:
        clash = (
            self._session.execute(
                select(FiscalYear)
                .where(FiscalYear.start_date <= end_date)
                .where(FiscalYear.end_date >= start_date)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if clash is not None:
            raise FiscalYearOverlapError(
                "Khoảng thời gian này đã thuộc một năm tài chính khác",
                fiscal_year_id=clash.id,
                fiscal_year_code=clash.code,
            )
