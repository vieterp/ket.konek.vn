"""Vòng đời header chứng từ: Cất, sửa, xóa (SRS 00 §3.3, phase-04 §Máy trạng thái).

Module nghiệp vụ sở hữu bảng chi tiết và màn hình; header thì mọi loại chứng từ
giống nhau — cấp số trong cùng transaction (rollback trả lại số), tra kỳ theo
ngày hạch toán, chặn kỳ khóa — nên đường đó nằm ở đây một lần thay vì chép vào
mười ba module.

Ghi sổ **không** ở đây: đó là việc của `engine/service.py`, nơi giữ `FOR SHARE`
trên dòng kỳ. Tách hai tầng đúng theo ranh giới "header chung / phát sinh
chung": tầng này không biết `gl_postings` tồn tại.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.errors import VoucherNotFoundError, VoucherTransitionError
from ket.kernel.identifiers import uuid7
from ket.kernel.numbering.service import NumberingRule, NumberingService
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.periods.service import PeriodService
from ket.posting.documents.models import EntryKind, Voucher, VoucherStatus
from ket.posting.documents.state_machine import VoucherAction, transition


@dataclass(frozen=True)
class VoucherDraft:
    """Những gì một module phải khai để Cất một header chứng từ."""

    document_type: str
    branch_id: int
    document_date: date
    posting_date: date
    currency_code: str
    exchange_rate: Decimal
    description: str | None = None
    cashflow_activity: int | None = None
    entry_kind: int = EntryKind.NGHIEP_VU
    """Bản chất bút toán (LD-17). Mặc định `NGHIEP_VU` — module nghiệp vụ
    thường không cần biết cột này tồn tại; chỉ engine kết chuyển cuối kỳ (10a)
    và màn hình chứng từ nghiệp vụ khác (`GLE`) mới khai khác đi."""


class VoucherService:
    """Thao tác header trong transaction đang mở của người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._periods = PeriodService(session)

    def create(self, draft: VoucherDraft, *, rule: NumberingRule, user_id: int) -> Voucher:
        """Cất: tra kỳ, chặn kỳ khóa, cấp số, ghi header ở trạng thái Đã cất.

        Cấp số là bước **cuối** trước khi ghi — khóa dòng bộ đếm giữ tới cuối
        transaction (xem `numbering/service.py`), nên mọi phép kiểm phải chạy
        xong trước để không giữ khóa lâu hơn cần.
        """
        period = self._resolve_open_period(draft.posting_date)

        voucher_id = uuid7()
        voucher_no = NumberingService(self._session).allocate(
            rule,
            branch_id=draft.branch_id,
            on_date=draft.posting_date,
            document_id=voucher_id,
        )
        voucher = Voucher(
            id=voucher_id,
            document_type=draft.document_type,
            voucher_no=voucher_no,
            branch_id=draft.branch_id,
            document_date=draft.document_date,
            posting_date=draft.posting_date,
            period_id=period.id,
            currency_code=draft.currency_code,
            exchange_rate=draft.exchange_rate,
            description=draft.description,
            cashflow_activity=draft.cashflow_activity,
            entry_kind=draft.entry_kind,
            status=VoucherStatus.DA_CAT.value,
            created_by=user_id,
        )
        self._session.add(voucher)
        self._session.flush()
        return voucher

    def require(self, voucher_id: UUID) -> Voucher:
        voucher = self._session.get(Voucher, voucher_id)
        if voucher is None:
            raise VoucherNotFoundError("Không tìm thấy chứng từ", voucher_id=str(voucher_id))
        return voucher

    def ensure_editable(self, voucher: Voucher) -> None:
        """Sửa được khi và chỉ khi: Đã cất, và kỳ còn mở (BR-GLE-06).

        Chứng từ đã ghi sổ muốn sửa thì bỏ ghi sổ trước — thông điệp phải nói
        đúng bước đó chứ không phải một câu "không sửa được" chung chung.
        """
        status = VoucherStatus(voucher.status)
        if status is not VoucherStatus.DA_CAT:
            raise VoucherTransitionError(
                "Chứng từ đã ghi sổ — bỏ ghi sổ trước rồi mới sửa hay xóa được",
                status=status.value,
                action="edit",
            )
        period = self._session.get(AccountingPeriod, voucher.period_id)
        if period is not None:
            self._periods.ensure_open(period)

    def move_to_date(self, voucher: Voucher, *, posting_date: date) -> None:
        """Đổi ngày hạch toán — tra lại kỳ, chặn cả kỳ cũ lẫn kỳ mới đã khóa.

        Kỳ cũ đã kiểm ở `ensure_editable` (người gọi phải gọi nó trước); ở đây
        kiểm kỳ **đích**: kéo một chứng từ vào kỳ đã khóa cũng là ghi vào kỳ
        đã khóa, chỉ khác cửa vào.
        """
        period = self._resolve_open_period(posting_date)
        voucher.posting_date = posting_date
        voucher.period_id = period.id

    def delete(self, voucher_id: UUID) -> None:
        """Xóa chứng từ Đã cất. Số chứng từ **không** tái sử dụng.

        Bảng chi tiết của module đi theo bằng `ON DELETE CASCADE` — module khai
        khóa ngoại như vậy khi tạo bảng (xem `gl_journal_lines`). Trạng thái
        khác Đã cất bị máy trạng thái từ chối.
        """
        voucher = self.require(voucher_id)
        self.ensure_editable(voucher)
        # Xác nhận cạnh DELETE tồn tại cho trạng thái hiện tại — giữ mọi luật
        # chuyển ở một chỗ thay vì lặp phép so trạng thái ở đây.
        transition(VoucherStatus(voucher.status), VoucherAction.DELETE)
        self._session.delete(voucher)
        self._session.flush()

    def _resolve_open_period(self, posting_date: date) -> AccountingPeriod:
        period = self._periods.resolve_period(posting_date)
        self._periods.ensure_open(period)
        return period
