"""Kiểm kê quỹ (FR-QUY-030/031) — biên bản đối chiếu + phiếu xử lý chênh lệch.

Luồng: lập biên bản (chụp số sổ tại ngày kiểm kê) → nếu lệch, sinh phiếu
thu/chi xử lý qua ĐÚNG đường nghiệp vụ của gói cấu hình: thừa →
`thua-quy-kiem-ke` (Nợ quỹ / Có 3381), thiếu → `thieu-quy-kiem-ke` (Nợ 1381 /
Có quỹ) — cặp TK lấy từ resolver FR-SYS-025, không cứng trong code. Bên quỹ
của phiếu là chính TK trên biên bản (kiểm kê két nào xử lý két đó), không phải
TK `cash` ngầm định của gói.

Kiểm kê v1 là tiền **đồng hạch toán** (đếm theo mệnh giá VND); két ngoại tệ đi
cùng bài đánh giá lại tỷ giá — ngoài phạm vi lát này.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.config.auto_posting_provider import operations_for
from ket.kernel.errors import (
    CountSheetAdjustmentError,
    CountSheetInvalidError,
    CountSheetNotFoundError,
)
from ket.kernel.periods.service import fiscal_year_covering
from ket.modules.cash_book.balance_service import cash_balance_as_of
from ket.modules.cash_book.guards import CASH_ON_HAND_PREFIX
from ket.modules.cash_book.models import (
    CashCountSheet,
    CashCountSheetLine,
    CashVoucherKind,
)
from ket.modules.cash_book.schemas import (
    CashVoucherIn,
    CashVoucherLineIn,
    CountSheetIn,
)
from ket.posting.contracts import Ledger, Voucher

SURPLUS_OPERATION = "thua-quy-kiem-ke"
DEFICIT_OPERATION = "thieu-quy-kiem-ke"


class CashCountSheetService:
    """Biên bản kiểm kê trong transaction người gọi — cùng khuôn các service khác."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, payload: CountSheetIn, *, user_id: int) -> CashCountSheet:
        account = self._session.get(ChartOfAccount, payload.cash_account_id)
        if account is None or not account.code.startswith(CASH_ON_HAND_PREFIX):
            raise CountSheetInvalidError(
                "Kiểm kê quỹ chỉ áp dụng cho tài khoản tiền mặt (111x)",
                cash_account_id=payload.cash_account_id,
            )
        if payload.lines:
            counted = sum((line.denomination * line.quantity for line in payload.lines), Decimal(0))
            if counted != payload.counted_total:
                raise CountSheetInvalidError(
                    "Tổng theo mệnh giá không khớp số đếm được",
                    from_lines=str(counted),
                    counted_total=str(payload.counted_total),
                )

        sheet = CashCountSheet(
            branch_id=payload.branch_id,
            cash_account_id=payload.cash_account_id,
            count_date=payload.count_date,
            book_balance=cash_balance_as_of(
                self._session,
                ledger=Ledger.FINANCIAL.value,
                branch_id=payload.branch_id,
                account_code=account.code,
                as_of=payload.count_date,
            ),
            counted_total=payload.counted_total,
            note=payload.note,
            created_by=user_id,
        )
        self._session.add(sheet)
        self._session.flush()
        for index, line in enumerate(payload.lines, start=1):
            self._session.add(
                CashCountSheetLine(
                    sheet_id=sheet.id,
                    line_no=index,
                    denomination=line.denomination,
                    quantity=line.quantity,
                )
            )
        self._session.flush()
        return sheet

    def require(self, sheet_id: UUID) -> CashCountSheet:
        sheet = self._session.get(CashCountSheet, sheet_id)
        if sheet is None:
            raise CountSheetNotFoundError("Không tìm thấy biên bản kiểm kê", sheet_id=str(sheet_id))
        return sheet

    def lines_of(self, sheet_id: UUID) -> list[CashCountSheetLine]:
        return list(
            self._session.execute(
                select(CashCountSheetLine)
                .where(CashCountSheetLine.sheet_id == sheet_id)
                .order_by(CashCountSheetLine.line_no)
            )
            .scalars()
            .all()
        )

    def list_page(
        self,
        *,
        cash_account_id: int | None,
        from_date: date | None,
        to_date: date | None,
        page: int,
        page_size: int,
    ) -> tuple[list[CashCountSheet], int]:
        query = select(CashCountSheet)
        if cash_account_id is not None:
            query = query.where(CashCountSheet.cash_account_id == cash_account_id)
        if from_date is not None:
            query = query.where(CashCountSheet.count_date >= from_date)
        if to_date is not None:
            query = query.where(CashCountSheet.count_date <= to_date)
        total = self._session.scalar(select(func.count()).select_from(query.subquery())) or 0
        rows = list(
            self._session.execute(
                query.order_by(CashCountSheet.count_date.desc(), CashCountSheet.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .scalars()
            .all()
        )
        return rows, total

    def delete(self, sheet_id: UUID) -> None:
        """Xóa biên bản CHƯA xử lý chênh lệch — đã sinh phiếu thì biên bản là
        căn cứ của phiếu, xóa phiếu trước rồi mới tới biên bản."""
        sheet = self.require(sheet_id)
        if sheet.adjustment_voucher_id is not None:
            raise CountSheetAdjustmentError(
                "Biên bản đã sinh phiếu xử lý chênh lệch — xóa phiếu đó trước",
                adjustment_voucher_id=str(sheet.adjustment_voucher_id),
            )
        self._session.delete(sheet)
        self._session.flush()

    def create_adjustment(
        self, sheet_id: UUID, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Sinh phiếu thu (thừa) / phiếu chi (thiếu) cho phần chênh (FR-QUY-031).

        Đi qua `CashVoucherService.create` như một phiếu người dùng tự lập —
        cùng cấp số, cùng bộ kiểm, cùng tùy chọn Cất-đồng-thời-ghi-sổ.
        """
        # Import cục bộ tránh vòng: service phiếu đã import schemas mà module
        # này cũng dùng; vòng đời hai service tách nhau.
        from ket.modules.cash_book.service import CashVoucherService

        sheet = self.require(sheet_id)
        difference = sheet.counted_total - sheet.book_balance
        if difference == 0:
            raise CountSheetAdjustmentError("Biên bản không có chênh lệch — không có gì để xử lý")
        if sheet.adjustment_voucher_id is not None:
            raise CountSheetAdjustmentError(
                "Biên bản đã sinh phiếu xử lý chênh lệch",
                adjustment_voucher_id=str(sheet.adjustment_voucher_id),
            )

        surplus = difference > 0
        kind = CashVoucherKind.RECEIPT if surplus else CashVoucherKind.PAYMENT
        operation_code = SURPLUS_OPERATION if surplus else DEFICIT_OPERATION
        counter_account_id = self._counter_account_id(
            sheet, kind=kind, operation_code=operation_code
        )

        year = fiscal_year_covering(self._session, sheet.count_date)
        base_currency = year.base_currency if year is not None else "VND"
        magnitude = abs(difference)
        line = CashVoucherLineIn(
            debit_account_id=sheet.cash_account_id if surplus else counter_account_id,
            credit_account_id=counter_account_id if surplus else sheet.cash_account_id,
            amount_fc=magnitude,
            description=(
                f"Xử lý {'thừa' if surplus else 'thiếu'} quỹ theo biên bản kiểm kê "
                f"ngày {sheet.count_date.isoformat()}"
            ),
        )
        voucher = CashVoucherService(self._session).create(
            CashVoucherIn(
                kind=kind,
                operation_code=operation_code,
                cash_account_id=sheet.cash_account_id,
                branch_id=sheet.branch_id,
                document_date=sheet.count_date,
                posting_date=sheet.count_date,
                currency_code=base_currency,
                exchange_rate=Decimal(1),
                description=line.description,
                lines=(line,),
            ),
            user_id=user_id,
            acknowledged_warnings=acknowledged_warnings,
        )
        sheet.adjustment_voucher_id = voucher.id
        self._session.flush()
        return voucher

    def _counter_account_id(self, sheet: CashCountSheet, *, kind: int, operation_code: str) -> int:
        """TK đối ứng (3381/1381) từ nghiệp vụ của gói hiệu lực — thiếu nghiệp
        vụ hoặc chưa khai TK là khoảng trống cấu hình, báo thẳng."""
        year = fiscal_year_covering(self._session, sheet.count_date)
        if year is None:
            raise CountSheetInvalidError(
                "Chưa có năm tài chính phủ ngày kiểm kê", count_date=sheet.count_date.isoformat()
            )
        resolved = operations_for(
            self._session,
            document_type="PT" if kind == CashVoucherKind.RECEIPT else "PC",
            scheme=year.accounting_scheme,
            on_date=sheet.count_date,
        )
        operation = next(
            (item for item in resolved.items if item.operation_code == operation_code), None
        )
        counter_code = (
            (
                operation.credit_account_code
                if kind == CashVoucherKind.RECEIPT
                else operation.debit_account_code
            )
            if operation is not None
            else None
        )
        if counter_code is None:
            raise CountSheetAdjustmentError(
                "Gói cấu hình chưa khai nghiệp vụ xử lý chênh lệch kiểm kê",
                operation_code=operation_code,
            )
        account_id = self._session.execute(
            select(ChartOfAccount.id).where(
                ChartOfAccount.package_id == resolved.package_id,
                ChartOfAccount.code == counter_code,
            )
        ).scalar_one_or_none()
        if account_id is None:
            raise CountSheetAdjustmentError(
                "TK xử lý chênh lệch trong cấu hình không có trong hệ thống TK",
                account_code=counter_code,
            )
        return account_id
