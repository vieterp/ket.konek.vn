"""Ghi sổ và bỏ ghi sổ — đường duy nhất được chạm `gl_postings` (luật phụ thuộc #3).

Trình tự `post` (phase-04 bước 3), tất cả trong **một** transaction của người
gọi (FR-NFR-003):

1. `SELECT … FOR SHARE` trên dòng kỳ (RT-09) — giữ tới cuối transaction, nên
   lệnh khóa sổ (`FOR UPDATE`, slice 4D) phải **chờ** mọi lượt ghi đang chạy,
   và lượt ghi mới không chen được khi lệnh khóa đang giữ dòng. Snapshot
   isolation không tự chặn write-skew này.
2. Quy đổi từng dòng đúng một lần (`convert_currency`), nhân bản sổ quản trị
   khi module không đưa dòng riêng.
3. Chạy **hết** bộ kiểm, gom cả vi phạm "kỳ đã khóa" — ném một lần với đầy đủ.
4. Chuyển trạng thái qua máy trạng thái, INSERT hàng loạt, đánh dấu kỳ bẩn.

Vết kiểm toán của ghi sổ là diff `status`/`posted_at`/`posted_by` trên
`vouchers` (mixin `Audited`); dòng `gl_postings` là dữ liệu suy ra, không nhật
ký hóa (xem docstring `engine/models.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, insert, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_provider import accounts_by_id, resolve_package
from ket.kernel.config.catalog import MONEY_SCALE_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    PostingValidationError,
    PostingViolation,
    VoucherNotFoundError,
)
from ket.kernel.money import convert_currency
from ket.kernel.periods.models import AccountingPeriod, FiscalYear
from ket.posting.balances.recalc_queue import mark_dirty
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.documents.registry import REFERENCE_GUARDS
from ket.posting.documents.state_machine import VoucherAction, transition_to
from ket.posting.engine.guards import run_guards
from ket.posting.engine.models import GlPosting, Ledger, PostingDimensionValue
from ket.posting.engine.prepared import PreparedLine
from ket.posting.engine.requests import PostingLine, PostingRequest
from ket.posting.engine.validators import run_validators

PERIOD_LOCKED_CODE = "period.locked"


def period_share_lock_statement(period_id: int) -> Select[tuple[AccountingPeriod]]:
    """Câu đọc dòng kỳ kèm `FOR SHARE` — tách riêng để test khóa được nó.

    `with_for_update(read=True)` là toàn bộ cơ chế RT-09 phía ghi sổ, và bỏ nó
    đi thì mọi test hành vi một-luồng vẫn xanh (mutation M2 của review 4A —
    UPDATE `vouchers` mang FK tới kỳ đã tạo đủ khóa ngầm che mất phép đo bằng
    `NOWAIT`). Test đơn vị đọc thẳng SQL biên dịch ra để canh; test hành vi
    interleaving thật nằm ở slice 4D cùng `lock_service` (phase-04 bước 18).
    """
    return (
        select(AccountingPeriod).where(AccountingPeriod.id == period_id).with_for_update(read=True)
    )


class PostingService:
    """Ghi/bỏ ghi sổ trong transaction đang mở của người gọi — không tự commit."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def post(
        self, request: PostingRequest, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        voucher = self._require_voucher(request.voucher_id)
        period, year = self._share_lock_period(voucher.period_id)

        violations: list[PostingViolation] = []
        if period.locked_at is not None:
            violations.append(
                PostingViolation(
                    PERIOD_LOCKED_CODE,
                    "Kỳ kế toán đã khóa sổ — muốn ghi thêm thì mở lại kỳ",
                    period_id=period.id,
                )
            )
        elif year.is_closed:
            violations.append(
                PostingViolation(
                    PERIOD_LOCKED_CODE,
                    "Năm tài chính đã quyết toán — không ghi thêm vào năm này",
                    period_id=period.id,
                    fiscal_year_id=year.id,
                )
            )

        scale = self._money_scale(user_id)
        lines = _prepare_lines(request, scale=scale)
        accounts = accounts_by_id(self._session, [line.source.account_id for line in lines])
        package = resolve_package(
            self._session, scheme=year.accounting_scheme, on_date=voucher.posting_date
        )
        violations.extend(
            run_validators(
                self._session,
                lines=lines,
                accounts=accounts,
                active_package_id=package.id,
            )
        )
        if violations:
            raise PostingValidationError("Chứng từ chưa đủ điều kiện ghi sổ", violations=violations)

        # Guard chạy SAU khi bộ kiểm hợp lệ đã sạch (số liệu của chứng từ không
        # hợp lệ không đáng đọc) và TRƯỚC máy trạng thái + INSERT — xem
        # `engine/guards.py` về ba mức của FR-SYS-062.
        guard_blocking, guard_warnings = run_guards(self._session, voucher=voucher, lines=lines)
        if guard_blocking:
            raise PostingValidationError(
                "Chứng từ chưa đủ điều kiện ghi sổ", violations=guard_blocking + guard_warnings
            )
        if guard_warnings and not acknowledged_warnings:
            raise PostingValidationError(
                "Ghi sổ cần người dùng xác nhận cảnh báo", violations=guard_warnings
            )

        # Máy trạng thái chạy TRƯỚC khi INSERT: chứng từ đã ghi sổ mà bị post
        # lần nữa phải dừng ở đây, không phải ở ràng buộc duy nhất của bảng.
        voucher.status = transition_to(VoucherStatus(voucher.status), VoucherAction.POST).value

        self._insert_postings(voucher, lines)

        voucher.posted_at = datetime.now(UTC)
        voucher.posted_by = user_id
        self._session.flush()

        for ledger in sorted({line.ledger for line in lines}):
            mark_dirty(
                self._session,
                ledger=ledger,
                branch_id=voucher.branch_id,
                from_period_id=voucher.period_id,
                reason=f"post {voucher.voucher_no}",
            )
        return voucher

    def unpost(self, voucher_id: UUID, *, user_id: int) -> Voucher:
        """Bỏ ghi sổ: xóa dòng phát sinh, chứng từ về Đã cất (SRS 00 §3.3).

        Xóa chứ không bút toán đảo — lệch có chủ đích so với research, đã ghi ở
        phase-04 §Bảng phát sinh chung. Kỳ đã khóa thì không tới được đây.
        """
        voucher = self._require_voucher(voucher_id)
        # Ai đó còn trỏ vào chứng từ này? (dòng sao kê đã khớp, …). Kiểm ở ĐÂY
        # chứ không ở router: service của từng module cũng gọi thẳng hàm này.
        REFERENCE_GUARDS.check(self._session, voucher_id)
        period, year = self._share_lock_period(voucher.period_id)
        if period.locked_at is not None or year.is_closed:
            raise PostingValidationError(
                "Kỳ đã khóa sổ — không bỏ ghi sổ được, phải mở lại kỳ trước",
                violations=[
                    PostingViolation(
                        PERIOD_LOCKED_CODE,
                        "Kỳ kế toán đã khóa sổ",
                        period_id=period.id,
                    )
                ],
            )

        touched_ledgers = set(
            self._session.execute(
                select(GlPosting.ledger).where(GlPosting.voucher_id == voucher.id).distinct()
            )
            .scalars()
            .all()
        )

        voucher.status = transition_to(VoucherStatus(voucher.status), VoucherAction.UNPOST).value

        # `posting_dimension_values` đi theo bằng `ON DELETE CASCADE`.
        self._session.execute(delete(GlPosting).where(GlPosting.voucher_id == voucher.id))
        voucher.posted_at = None
        voucher.posted_by = None
        self._session.flush()

        for ledger in sorted(touched_ledgers):
            mark_dirty(
                self._session,
                ledger=ledger,
                branch_id=voucher.branch_id,
                from_period_id=voucher.period_id,
                reason=f"unpost {voucher.voucher_no}",
            )
        return voucher

    def _require_voucher(self, voucher_id: UUID) -> Voucher:
        voucher = self._session.get(Voucher, voucher_id)
        if voucher is None:
            raise VoucherNotFoundError("Không tìm thấy chứng từ", voucher_id=str(voucher_id))
        return voucher

    def _share_lock_period(self, period_id: int) -> tuple[AccountingPeriod, FiscalYear]:
        """Đọc dòng kỳ với `FOR SHARE` và giữ khóa tới cuối transaction (RT-09).

        `with_for_update(read=True)` là `FOR SHARE` của PostgreSQL: nhiều lượt
        ghi sổ cùng kỳ chạy song song thoải mái (share tương thích share),
        nhưng lệnh khóa kỳ cầm `FOR UPDATE` thì loại trừ cả hai chiều.
        """
        period = self._session.execute(period_share_lock_statement(period_id)).scalar_one()
        year = self._session.get(FiscalYear, period.fiscal_year_id)
        if year is None:  # pragma: no cover - FK bảo đảm
            raise RuntimeError(f"Kỳ {period_id} trỏ vào năm tài chính không tồn tại")
        return period, year

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    def _insert_postings(self, voucher: Voucher, lines: list[PreparedLine]) -> None:
        """INSERT hàng loạt — một câu cho dòng sổ, một câu cho chiều mở rộng.

        `RETURNING id` giữ đúng thứ tự dòng đưa vào (SQLAlchemy insertmanyvalues
        bảo đảm thứ tự khi có returning), nên ghép ngược `id` cho bảng chiều mở
        rộng không cần truy vấn lại.
        """
        rows: list[dict[str, Any]] = []
        for line in lines:
            dims = line.source.dimensions
            rows.append(
                {
                    "voucher_id": voucher.id,
                    "line_no": line.line_no,
                    "source_line_id": line.source.source_line_id,
                    "ledger": line.ledger,
                    "branch_id": voucher.branch_id,
                    "posting_date": voucher.posting_date,
                    "period_id": voucher.period_id,
                    # Sao chép từ header (LD-17) — cùng nhóm denormalize với ba
                    # cột ngay trên. Đây là đường ghi DUY NHẤT vào `gl_postings`
                    # (luật phụ thuộc #3) nên hai bên không thể lệch nhau.
                    "entry_kind": voucher.entry_kind,
                    "account_id": line.source.account_id,
                    "corresponding_account_id": line.source.corresponding_account_id,
                    "currency_code": line.source.currency,
                    "exchange_rate": line.source.rate,
                    "debit_fc": line.source.debit_fc,
                    "credit_fc": line.source.credit_fc,
                    "debit": line.debit,
                    "credit": line.credit,
                    "partner_id": dims.partner_id,
                    "partner_kind": (
                        dims.partner_kind.value if dims.partner_kind is not None else None
                    ),
                    "cost_object_id": dims.cost_object_id,
                    "project_id": dims.project_id,
                    "order_id": dims.order_id,
                    "contract_id": dims.contract_id,
                    "expense_item_id": dims.expense_item_id,
                    "item_id": dims.item_id,
                    "warehouse_id": dims.warehouse_id,
                    "bank_account_id": dims.bank_account_id,
                    "description": line.source.description,
                }
            )

        inserted_ids = (
            self._session.execute(insert(GlPosting).returning(GlPosting.id), rows).scalars().all()
        )

        dimension_rows: list[dict[str, Any]] = []
        for posting_id, line in zip(inserted_ids, lines, strict=True):
            for value in line.source.dimensions.extended:
                dimension_rows.append(
                    {
                        "posting_id": posting_id,
                        "dimension_id": value.dimension_id,
                        "value_id": value.value_id,
                    }
                )
        if dimension_rows:
            self._session.execute(insert(PostingDimensionValue), dimension_rows)


def _prepare_lines(request: PostingRequest, *, scale: int) -> list[PreparedLine]:
    """Quy đổi và trải hai sổ thành một danh sách dòng phẳng.

    `management_lines is None` → sổ quản trị chép nguyên sổ tài chính (điểm để
    giá thành/lương ghi khác nhau giữa hai sổ mà không phải hai luồng); danh
    sách rỗng → chứng từ không lên sổ quản trị.
    """
    prepared: list[PreparedLine] = []

    def extend(ledger: Ledger, source_lines: tuple[PostingLine, ...]) -> None:
        for index, line in enumerate(source_lines, start=1):
            prepared.append(
                PreparedLine(
                    ledger=ledger.value,
                    line_no=index,
                    source=line,
                    debit=convert_currency(line.debit_fc, line.rate, scale),
                    credit=convert_currency(line.credit_fc, line.rate, scale),
                )
            )

    extend(Ledger.FINANCIAL, request.financial_lines)
    management = (
        request.management_lines
        if request.management_lines is not None
        else request.financial_lines
    )
    extend(Ledger.MANAGEMENT, management)
    return prepared
