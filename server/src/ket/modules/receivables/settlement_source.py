"""Nguồn công nợ mở + đích đối trừ từ SỔ PHỤ `ar_ap_ledger` (lát 7A).

Bản cài thứ hai của ba Protocol công nợ, sau `posting/opening_balances/
settlement_source.py` (4C/6B). Hai nguồn sống song song có chủ đích: số dư đầu
kỳ là những khoản nợ mang sang từ trước khi hệ chạy, còn bảng này là nợ do
chính hệ sinh ra. `posting.settlements.open_invoices` **nối** kết quả của mọi
provider đã đăng ký, nên màn thu tiền của phase 6 thấy cả hai mà không phải
sửa một dòng nào trong `cash_book` — đó là điều kiện nghiệm thu "chứng minh
Protocol hoạt động" của phase 7.

**Một source cho hai loại đích.** `SALES_INVOICE` và `PURCHASE_INVOICE` cùng
nằm trên một bảng, khác nhau ở cột `target_kind`, nên cùng một đối tượng đăng
ký cho cả hai loại — `find`/`apply`/`revert` tra theo id và không cần biết
chiều. Chiều chỉ quan trọng ở `open_invoices` (liệt kê để chọn), và ở đó hai
view khóa cứng chiều của mình.

**Vì sao hai view thay vì một đối tượng trả lời theo `partner_kind`** — sửa
H-1 review 6B: một đối tượng đăng ký làm cả hai provider sẽ trả lời theo dữ
liệu bất kể nó được hỏi qua registry chiều nào, và cổng quyền phía API (kiểm
theo chiều) bị đi vòng — người chỉ có quyền xem phiếu thu đọc được công nợ
phải trả.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.protocols import PROVIDERS, OpenInvoice, SettlementTargetKind
from ket.modules.receivables.models import ArApLedgerEntry

SETTLEMENT_TARGET_MISSING_CODE = "settlement.target_missing"
SETTLEMENT_OVERPAID_CODE = "settlement.exceeds_remaining"

_RECEIVABLE_KINDS = (
    SettlementTargetKind.SALES_INVOICE,
    SettlementTargetKind.JOURNAL_RECEIVABLE,
)
"""Loại đích mang chiều PHẢI THU — `_ReceivableView` liệt kê đúng bộ này."""

_PAYABLE_KINDS = (
    SettlementTargetKind.PURCHASE_INVOICE,
    SettlementTargetKind.JOURNAL_PAYABLE,
)
"""Loại đích mang chiều PHẢI TRẢ."""

_OWNED_KINDS = _RECEIVABLE_KINDS + _PAYABLE_KINDS
"""Bốn loại đích bảng này làm chủ. `OPENING_BALANCE` thuộc về nguồn 4C.

Khoản nợ ghi thẳng bằng chứng từ nghiệp vụ khác (7C-3) vào cùng bảng và cùng
hai chiều: nó là một khoản nợ như mọi khoản khác, chỉ khác ở chỗ **không có
hóa đơn gốc** — `document_id` trỏ chính chứng từ GLE. Để nó ngoài hai view thì
phiếu thu/chi không bao giờ nhìn thấy nó, và một khoản nợ không đối trừ được
là một khoản nợ treo vĩnh viễn trên báo cáo tuổi nợ.
"""

_FINANCIAL_LEDGER = 0
"""Chỉ sổ tài chính vào màn đối trừ: tiền thật chỉ trả một lần, và liệt kê cả
hai sổ sẽ cho người dùng chọn cùng một khoản nợ hai lần."""


def _to_open_invoice(row: ArApLedgerEntry) -> OpenInvoice:
    return OpenInvoice(
        target_kind=SettlementTargetKind(row.target_kind),
        target_id=row.id,
        partner_kind=PartnerKind(row.partner_kind),
        partner_id=row.partner_id,
        branch_id=row.branch_id,
        account_id=row.account_id,
        invoice_no=row.document_no,
        invoice_date=row.document_date,
        due_date=row.due_date,
        currency_code=row.currency_code,
        exchange_rate=row.exchange_rate,
        amount_fc=row.amount_fc,
        remaining_fc=row.amount_fc - row.settled_fc,
        remaining=row.amount - row.settled,
        description=row.description,
    )


class ArApSettlementSource:
    """Cài `SettlementTargetSource` cho cả hai loại hóa đơn mua/bán."""

    def _open_invoices(
        self,
        session: Session,
        *,
        target_kinds: Sequence[SettlementTargetKind],
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        """Khoản còn nợ của MỘT đối tác theo MỘT chiều, tính đến hết `as_of`.

        `target_kinds` do view khóa, nên hỏi nhầm chiều chỉ ra danh sách rỗng
        chứ không rò dữ liệu sang chiều kia. Nhiều loại đích cho **một** chiều
        kể từ 7C-3: hóa đơn bán và khoản phải thu ghi tay đứng cùng hàng trên
        màn thu tiền, vì với người đi thu thì chúng là cùng một thứ.
        """
        rows = (
            session.execute(
                select(ArApLedgerEntry)
                .where(
                    ArApLedgerEntry.target_kind.in_([kind.value for kind in target_kinds]),
                    ArApLedgerEntry.ledger == _FINANCIAL_LEDGER,
                    ArApLedgerEntry.branch_id == branch_id,
                    ArApLedgerEntry.partner_kind == partner_kind.value,
                    ArApLedgerEntry.partner_id == partner_id,
                    ArApLedgerEntry.document_date <= as_of,
                    ArApLedgerEntry.is_closed.is_(False),
                )
                .order_by(ArApLedgerEntry.document_date, ArApLedgerEntry.document_no)
            )
            .scalars()
            .all()
        )
        return tuple(_to_open_invoice(row) for row in rows)

    def find(self, session: Session, *, target_ids: Sequence[UUID]) -> Sequence[OpenInvoice]:
        if not target_ids:
            return ()
        rows = (
            session.execute(
                select(ArApLedgerEntry).where(
                    ArApLedgerEntry.id.in_(set(target_ids)),
                    ArApLedgerEntry.target_kind.in_([kind.value for kind in _OWNED_KINDS]),
                    # Cùng bộ lọc sổ với `_open_invoices`: thiếu nó thì client
                    # gửi thẳng `target_id` của một khoản chỉ-quản-trị và đối
                    # trừ được nó bằng một phiếu thu sổ tài chính.
                    ArApLedgerEntry.ledger == _FINANCIAL_LEDGER,
                )
            )
            .scalars()
            .all()
        )
        return tuple(_to_open_invoice(row) for row in rows)

    def apply(
        self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
    ) -> None:
        entry = self._lock(session, target_id)
        remaining_fc = entry.amount_fc - entry.settled_fc
        remaining = entry.amount - entry.settled
        # Kiểm CẢ HAI phía (cùng lập luận với nguồn số dư đầu kỳ, review 6B
        # H-2): nguyên tệ là trục đối trừ, còn phía VND có thể vượt riêng vì
        # phần lẻ làm tròn của các lượt trước — để lọt thì CHECK
        # `settled_within_amount` nổ thành 500 thay vì một vi phạm 422.
        if amount_fc > remaining_fc or amount > remaining:
            raise PostingValidationError(
                "Số đối trừ vượt số còn nợ của chứng từ công nợ",
                violations=[
                    PostingViolation(
                        SETTLEMENT_OVERPAID_CODE,
                        "Chứng từ công nợ đã được đối trừ bởi phiếu khác — số còn nợ không đủ",
                        target_id=str(target_id),
                        remaining_fc=str(remaining_fc),
                        amount_fc=str(amount_fc),
                        remaining=str(remaining),
                        amount=str(amount),
                    )
                ],
            )
        entry.settled_fc += amount_fc
        entry.settled += amount
        session.flush()

    def revert(
        self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
    ) -> None:
        entry = self._lock(session, target_id)
        if amount_fc > entry.settled_fc or amount > entry.settled:
            # Gỡ nhiều hơn đã ghi = đường ghi và đường gỡ đã lệch nhau — phải
            # nổ chứ không kẹp về 0.
            raise RuntimeError(
                f"Gỡ đối trừ {amount_fc} vượt số đã ghi {entry.settled_fc} "
                f"trên khoản công nợ {target_id}"
            )
        entry.settled_fc -= amount_fc
        entry.settled -= amount
        session.flush()

    def _lock(self, session: Session, target_id: UUID) -> ArApLedgerEntry:
        """Khóa dòng đích trước khi đọc số còn nợ (RT-16).

        Không khóa thì hai phiếu thu đồng thời cùng đọc một `settled` cũ, cùng
        thấy "còn nợ đủ", và tổng đã trả vượt giá trị hóa đơn — BR-QUY-02 chỉ
        còn đúng trên giấy.
        """
        entry = session.execute(
            select(ArApLedgerEntry)
            .where(
                ArApLedgerEntry.id == target_id,
                ArApLedgerEntry.ledger == _FINANCIAL_LEDGER,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if entry is None:
            raise PostingValidationError(
                "Chứng từ công nợ được đối trừ không còn tồn tại",
                violations=[
                    PostingViolation(
                        SETTLEMENT_TARGET_MISSING_CODE,
                        "Chứng từ công nợ đã bị xóa hoặc bỏ ghi sổ — chọn lại "
                        "chứng từ đối trừ trên phiếu",
                        target_id=str(target_id),
                    )
                ],
            )
        return entry


class _ReceivableView:
    """`ReceivableProvider` khóa cứng vào CHIỀU phải thu (hóa đơn bán + khoản
    phải thu ghi tay)."""

    def __init__(self, source: ArApSettlementSource) -> None:
        self._source = source

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        return self._source._open_invoices(
            session,
            target_kinds=_RECEIVABLE_KINDS,
            partner_kind=partner_kind,
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )


class _PayableView:
    """`PayableProvider` khóa cứng vào CHIỀU phải trả (hóa đơn mua + khoản
    phải trả ghi tay)."""

    def __init__(self, source: ArApSettlementSource) -> None:
        self._source = source

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        return self._source._open_invoices(
            session,
            target_kinds=_PAYABLE_KINDS,
            partner_kind=partner_kind,
            partner_id=partner_id,
            branch_id=branch_id,
            as_of=as_of,
        )


SOURCE = ArApSettlementSource()

PROVIDERS.register_receivable(_ReceivableView(SOURCE))
PROVIDERS.register_payable(_PayableView(SOURCE))
for _kind in _OWNED_KINDS:
    PROVIDERS.register_settlement_source(_kind, SOURCE)
