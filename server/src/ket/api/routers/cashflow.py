"""BFF màn hình "Tiền vào tiền ra" (`/api/v1/cashflow/*`) — bước 16, lát 6E-1.

**Chỉ ĐỌC, và chỉ ở tầng api.** Đây là lớp gộp dữ liệu của hai module cho MỘT
màn hình, không phải một module thứ ba: ghi thì client gọi
`/api/v1/cash-book/*` hoặc `/api/v1/bank/*`. `import-linter` C3 vẫn cấm
`cash_book` ↔ `bank` import nhau — chính rủi ro "UI gộp kéo theo gộp module ở
backend" mà §Risk Assessment của phase-06 đánh giá Khả năng CAO. Tầng api đứng
trên cả hai nên gộp ở đây là chỗ duy nhất hợp lệ.

Quyền đi theo TỪNG nửa, không phải một cổng chung: thiếu quyền quỹ thì hàng thẻ
không có thẻ quỹ, thiếu quyền ngân hàng thì không có thẻ ngân hàng, thiếu CẢ
HAI mới 403. Một thủ quỹ chỉ được cấp quyền quỹ vẫn phải mở được màn hình —
và không được thấy một đồng nào của phía ngân hàng (họ hàng lỗi leo quyền 6B
H-1: quyền phải xét trên đúng thứ dữ liệu trả về, không xét trên tên màn hình).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, literal, select
from sqlalchemy.orm import Session

from ket.api.dependencies import AuthorizedRequest, SessionFactory, get_authorized_request
from ket.api.routers.cashflow_schemas import (
    BankAccountCard,
    CashAccountCard,
    CashflowOverviewResponse,
    CashflowSource,
    CashflowTransaction,
    CashflowTransactionsResponse,
)
from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.contracts import Ledger
from ket.kernel.currency.models import Currency
from ket.kernel.errors import PermissionDeniedError
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.bank import (
    BANK_PERMISSION_MODULE,
    CHEQUE_PERMISSION_CODE,
    CREDIT_ADVICE_PERMISSION_CODE,
    INTERNAL_TRANSFER_PERMISSION_CODE,
    PAYMENT_ORDER_PERMISSION_CODE,
)
from ket.modules.bank.balance_service import (
    DEPOSIT_ACCOUNT_CODE_PREFIX,
    deposit_balances_as_of,
)
from ket.modules.bank.models import BankVoucher
from ket.modules.cash_book import (
    CASH_PERMISSION_MODULE,
    PAYMENT_PERMISSION_CODE,
    RECEIPT_PERMISSION_CODE,
)
from ket.modules.cash_book.balance_service import (
    CASH_ON_HAND_PREFIX,
    cash_balances_as_of,
)
from ket.modules.cash_book.models import CashVoucher
from ket.posting.documents.models import Voucher
from ket.posting.engine.models import GlPosting
from ket.posting.opening_balances.models import OpeningBalance

router = APIRouter(prefix="/api/v1/cashflow", tags=["cashflow"])

MAX_PAGE_SIZE: Final[int] = 200

_CASH_VIEW_CODES: Final[tuple[str, ...]] = tuple(
    permission_code(CASH_PERMISSION_MODULE, code, Action.VIEW)
    for code in (RECEIPT_PERMISSION_CODE, PAYMENT_PERMISSION_CODE)
)
_BANK_VIEW_CODES: Final[tuple[str, ...]] = tuple(
    permission_code(BANK_PERMISSION_MODULE, code, Action.VIEW)
    for code in (
        CREDIT_ADVICE_PERMISSION_CODE,
        PAYMENT_ORDER_PERMISSION_CODE,
        CHEQUE_PERMISSION_CODE,
        INTERNAL_TRANSFER_PERMISSION_CODE,
    )
)


def _may_see_cash(authorized: AuthorizedRequest) -> bool:
    """Xem được PHÍA QUỸ khi có quyền xem bất kỳ loại phiếu quỹ nào.

    "Bất kỳ" chứ không "tất cả": người chỉ được cấp quyền xem phiếu thu vẫn có
    quyền chính đáng với số dư quỹ — số dư là hệ quả của những phiếu họ đọc
    được, không phải một bí mật riêng.
    """
    return any(authorized.access.has(code) for code in _CASH_VIEW_CODES)


def _may_see_bank(authorized: AuthorizedRequest) -> bool:
    return any(authorized.access.has(code) for code in _BANK_VIEW_CODES)


def _require_either(authorized: AuthorizedRequest) -> AuthorizedRequest:
    if not _may_see_cash(authorized) and not _may_see_bank(authorized):
        raise PermissionDeniedError(
            "Tài khoản không có quyền xem quỹ hay ngân hàng",
            permission=_CASH_VIEW_CODES[0],
        )
    return authorized


def _authorized_for_cashflow(
    authorized: Annotated[AuthorizedRequest, Depends(get_authorized_request)],
) -> AuthorizedRequest:
    return _require_either(authorized)


CashflowReader = Annotated[AuthorizedRequest, Depends(_authorized_for_cashflow)]


def _base_currency_code(session: Session) -> str | None:
    """Mã đồng tiền hạch toán, hoặc `None` khi dataset chưa khai đồng nào.

    KHÔNG dùng `ExchangeRateService.base_currency()`: hàm đó ném lỗi "chặn mọi
    việc" — đúng cho đường GHI sổ, sai cho một màn hình CHỈ ĐỌC. Một dữ liệu kế
    toán vừa cấp, chưa khai tiền tệ, phải mở được trang "Tiền vào tiền ra" và
    thấy mọi thẻ bằng 0 thay vì một lỗi 500 (đúng họ lỗi 6A H-1: đừng để mặc
    định tiền tệ chết cứng — hoặc chết hẳn — trên dataset chưa khai tiền tệ).
    """
    return session.execute(select(Currency.code).where(Currency.is_base)).scalar_one_or_none()


@router.get("/overview", response_model=CashflowOverviewResponse)
def cashflow_overview(
    authorized: CashflowReader,
    factory: SessionFactory,
    as_of: Annotated[date | None, Query()] = None,
    ledger: Annotated[int, Query(ge=int(Ledger.FINANCIAL), le=int(Ledger.MANAGEMENT))] = int(
        Ledger.FINANCIAL
    ),
) -> CashflowOverviewResponse:
    """Hàng thẻ: quỹ tiền mặt + từng tài khoản ngân hàng, kèm số dư tới `as_of`.

    Cộng trên đúng những chi nhánh người dùng thấy (`scope.branch_ids` — cùng
    tập mà RLS áp), nên hai người ở hai chi nhánh mở cùng màn hình thấy hai con
    số khác nhau, và cả hai đều đúng với phạm vi của họ.
    """
    with unit_of_work(factory, authorized.scope) as session:
        # Ngày địa phương của máy chủ, cùng khuôn với `api/routers/printing.py`
        # và `reporting/render_job.py`: người dùng LAN và app server ở cùng
        # múi giờ, và "hôm nay" trên một màn hình kế toán là ngày làm việc
        # chứ không phải ngày UTC.
        effective_as_of = as_of or datetime.now(UTC).astimezone().date()
        branch_ids = authorized.scope.branch_ids
        base_currency = _base_currency_code(session)

        cash_cards: tuple[CashAccountCard, ...] = ()
        if _may_see_cash(authorized):
            cash_cards = tuple(
                CashAccountCard(
                    account_code=row.account_code,
                    account_name=row.account_name,
                    currency_code=base_currency or "",
                    balance=row.balance,
                )
                for row in cash_balances_as_of(
                    session, ledger=ledger, branch_ids=branch_ids, as_of=effective_as_of
                )
            )

        bank_cards: tuple[BankAccountCard, ...] = ()
        unassigned = Decimal(0)
        if _may_see_bank(authorized):
            balances = deposit_balances_as_of(
                session, ledger=ledger, branch_ids=branch_ids, as_of=effective_as_of
            )
            accounts = {
                row.id: row
                for row in session.execute(
                    select(
                        CompanyBankAccount.id,
                        CompanyBankAccount.code,
                        CompanyBankAccount.name,
                        CompanyBankAccount.bank_id,
                    ).where(CompanyBankAccount.id.in_([b.bank_account_id for b in balances] or [0]))
                ).all()
            }
            bank_names = _bank_names(session, {row.bank_id for row in accounts.values()})
            bank_cards = tuple(
                BankAccountCard(
                    bank_account_id=balance.bank_account_id,
                    bank_account_code=account.code,
                    bank_account_name=account.name,
                    bank_name=bank_names.get(account.bank_id),
                    currency_code=balance.currency_code,
                    balance_fc=balance.balance_fc,
                    balance=balance.balance,
                )
                for balance in balances
                if (account := accounts.get(balance.bank_account_id)) is not None
            )
            unassigned = _unassigned_deposit_balance(
                session,
                ledger=ledger,
                branch_ids=branch_ids,
                as_of=effective_as_of,
                assigned=sum((balance.balance for balance in balances), Decimal(0)),
            )

        return CashflowOverviewResponse(
            as_of=effective_as_of,
            ledger=ledger,
            cash_accounts=cash_cards,
            bank_accounts=bank_cards,
            unassigned_deposit=unassigned,
        )


def _bank_names(session: Session, bank_ids: set[int]) -> dict[int, str]:
    """Tên ngân hàng của cả lô `bank_id` trong MỘT truy vấn (tránh N+1 khi hàng
    thẻ có nhiều tài khoản ở nhiều ngân hàng)."""
    if not bank_ids:
        return {}
    return {
        row.id: row.name
        for row in session.execute(select(Bank.id, Bank.name).where(Bank.id.in_(bank_ids))).all()
    }


def _unassigned_deposit_balance(
    session: Session,
    *,
    ledger: int,
    branch_ids: tuple[int, ...],
    as_of: date,
    assigned: Decimal,
) -> Decimal:
    """Số dư TK 112 KHÔNG quy được về tài khoản ngân hàng nào.

    Từ lát 6G-1 chỉ còn dòng ghi sổ TRƯỚC lát ấy mà migration không suy nổi chủ
    (chiều `bank_account` nay bắt buộc trên 112x). Con số này bằng *tổng TK 112
    − tổng các thẻ ngân hàng*, và hiện ra để người dùng đối chiếu được hàng thẻ
    với bảng cân đối TK thay vì tự hỏi vì sao lệch.

    **Khoảng hở đã biết, bàn giao 6G-2:** thẻ số dư nay gồm cả phát sinh 112x
    của phiếu quỹ và bút toán tổng hợp, còn `_transactions_query` nhánh `bank`
    vẫn chỉ liệt kê chứng từ tiền gửi — bấm vào thẻ không thấy hết những gì
    tạo ra con số. Nới lưới KHÔNG phải sửa một dòng: nhánh này đứng sau cổng
    `_may_see_bank`, nên cho phiếu quỹ lọt vào sẽ để người chỉ có quyền ngân
    hàng đọc chi tiết phiếu quỹ. Muốn nới thì phải gộp cổng theo TỪNG dòng
    (chứng từ nào thì quyền nấy) — quyết định sản phẩm, không phải lát này.

    `assigned` truyền vào chứ không tính lại: người gọi vừa dựng xong hàng thẻ
    từ CHÍNH tập số dư đó, nên tính lại vừa tốn một lượt quét vừa mở đường cho
    hai con số lệch nhau nếu một ngày nào đó hai lượt đọc khác điều kiện.
    """
    total = _deposit_totals(session, ledger=ledger, branch_ids=branch_ids, as_of=as_of)
    return total - assigned


def _deposit_totals(
    session: Session, *, ledger: int, branch_ids: tuple[int, ...], as_of: date
) -> Decimal:
    """Tổng số dư MỌI TK 112x tới `as_of` — mẫu số của phép trừ ở trên."""
    year = fiscal_year_covering(session, as_of)
    if year is None or not branch_ids:
        return Decimal(0)
    deposit_accounts = select(ChartOfAccount.id).where(
        ChartOfAccount.code.like(literal(f"{DEPOSIT_ACCOUNT_CODE_PREFIX}%"))
    )
    opening = session.execute(
        select(func.coalesce(func.sum(OpeningBalance.debit - OpeningBalance.credit), 0)).where(
            OpeningBalance.fiscal_year_id == year.id,
            OpeningBalance.ledger == ledger,
            OpeningBalance.branch_id.in_(branch_ids),
            OpeningBalance.account_id.in_(deposit_accounts),
        )
    ).scalar_one()
    movement = session.execute(
        select(func.coalesce(func.sum(GlPosting.debit - GlPosting.credit), 0)).where(
            GlPosting.ledger == ledger,
            GlPosting.branch_id.in_(branch_ids),
            GlPosting.account_id.in_(deposit_accounts),
            GlPosting.posting_date >= year.start_date,
            GlPosting.posting_date <= as_of,
        )
    ).scalar_one()
    return Decimal(opening or 0) + Decimal(movement or 0)


@router.get("/transactions", response_model=CashflowTransactionsResponse)
def cashflow_transactions(
    authorized: CashflowReader,
    factory: SessionFactory,
    source: Annotated[CashflowSource, Query()],
    cash_account_code: Annotated[str | None, Query(max_length=20)] = None,
    bank_account_id: Annotated[int | None, Query()] = None,
    from_date: Annotated[date | None, Query()] = None,
    to_date: Annotated[date | None, Query()] = None,
    status: Annotated[int | None, Query()] = None,
    ledger: Annotated[int, Query(ge=int(Ledger.FINANCIAL), le=int(Ledger.MANAGEMENT))] = int(
        Ledger.FINANCIAL
    ),
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CashflowTransactionsResponse:
    """Lưới giao dịch của MỘT thẻ (quỹ hoặc một tài khoản ngân hàng).

    `amount` là phát sinh RÒNG trên đúng tài khoản của thẻ đang chọn: dương =
    tiền vào, âm = tiền ra. Tính từ `gl_postings` chứ không từ thân chứng từ —
    cùng nguồn với thẻ số dư, nên tổng lưới và số dư thẻ không thể cãi nhau.
    """
    if source == "cash" and not _may_see_cash(authorized):
        raise PermissionDeniedError(
            "Tài khoản không có quyền xem phiếu quỹ", permission=_CASH_VIEW_CODES[0]
        )
    if source == "bank" and not _may_see_bank(authorized):
        raise PermissionDeniedError(
            "Tài khoản không có quyền xem chứng từ ngân hàng", permission=_BANK_VIEW_CODES[0]
        )
    with unit_of_work(factory, authorized.scope) as session:
        query = _transactions_query(
            source=source,
            ledger=ledger,
            cash_account_code=cash_account_code,
            bank_account_id=bank_account_id,
            from_date=from_date,
            to_date=to_date,
            status=status,
        )
        total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        rows = session.execute(
            query.order_by(Voucher.posting_date.desc(), Voucher.voucher_no.desc(), Voucher.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return CashflowTransactionsResponse(
            items=tuple(
                CashflowTransaction(
                    voucher_id=row.voucher_id,
                    voucher_no=row.voucher_no,
                    document_type=row.document_type,
                    source=source,
                    posting_date=row.posting_date,
                    document_date=row.document_date,
                    description=row.description,
                    partner_name=row.partner_name,
                    amount=Decimal(row.amount or 0),
                    status=row.status,
                )
                for row in rows
            ),
            total=total,
        )


def _transactions_query(
    *,
    source: CashflowSource,
    ledger: int,
    cash_account_code: str | None,
    bank_account_id: int | None,
    from_date: date | None,
    to_date: date | None,
    status: int | None,
) -> Select[Any]:
    """Câu truy vấn lưới cho một nguồn.

    Hai nhánh chứ không một câu `UNION`: chiều "tài khoản của thẻ" được định
    nghĩa khác nhau ở hai module (TK kế toán quỹ trên thân phiếu ↔ TK ngân hàng
    quy chủ theo chiều dòng), và ép chúng vào một câu chung sẽ phải bịa một
    khái niệm thứ ba không tồn tại ở đâu trong miền nghiệp vụ.
    """
    amount = func.sum(GlPosting.debit - GlPosting.credit).label("amount")
    base_columns = (
        Voucher.id.label("voucher_id"),
        Voucher.voucher_no,
        Voucher.document_type,
        Voucher.posting_date,
        Voucher.document_date,
        Voucher.description,
        Voucher.status,
        Partner.name.label("partner_name"),
    )

    if source == "cash":
        query = (
            select(*base_columns, amount)
            .join(CashVoucher, CashVoucher.id == Voucher.id)
            .join(
                GlPosting,
                (GlPosting.voucher_id == Voucher.id)
                & (GlPosting.account_id == CashVoucher.cash_account_id)
                & (GlPosting.ledger == ledger),
            )
            .outerjoin(Partner, Partner.id == CashVoucher.partner_id)
        )
        # Lọc theo SỐ HIỆU TK, cùng trục gộp với thẻ số dư (review 6E-1 M-4).
        # Thẻ gộp theo mã vì cùng một "1111" ở hai gói cấu hình là hai `id`;
        # nếu lưới lọc theo `id` thì sau khi dataset đổi gói, thẻ cộng cả hai id
        # còn lưới chỉ mở được một — tổng lưới ≠ số dư thẻ và không ai giải
        # thích được vì sao. Trục gộp và khóa drill-down phải là MỘT.
        #
        # Tiền tố `111` áp cho CẢ hai nhánh (review 6E-1 L-1): không chỗ nào
        # ràng `cash_vouchers.cash_account_id` phải là 111x lúc tạo, nên thiếu
        # nó thì một phiếu quỹ lỡ khai TK 112 sẽ để người CHỈ có quyền quỹ đọc
        # được dòng tiền của tài khoản ngân hàng.
        query = query.join(ChartOfAccount, ChartOfAccount.id == CashVoucher.cash_account_id).where(
            ChartOfAccount.code.like(literal(f"{CASH_ON_HAND_PREFIX}%"))
        )
        if cash_account_code is not None:
            query = query.where(ChartOfAccount.code == cash_account_code)
    else:
        deposit_accounts = select(ChartOfAccount.id).where(
            ChartOfAccount.code.like(literal(f"{DEPOSIT_ACCOUNT_CODE_PREFIX}%"))
        )
        query = (
            select(*base_columns, amount)
            .join(BankVoucher, BankVoucher.id == Voucher.id)
            .join(
                GlPosting,
                (GlPosting.voucher_id == Voucher.id)
                & (GlPosting.ledger == ledger)
                & GlPosting.account_id.in_(deposit_accounts),
            )
            .outerjoin(Partner, Partner.id == BankVoucher.partner_id)
        )
        if bank_account_id is not None:
            # Chủ sở hữu đọc cột `gl_postings.bank_account_id` (lát 6G-1) —
            # chuyển tiền nội bộ đã được quy chủ theo CHIỀU dòng ngay lúc ghi
            # sổ, nên lưới của tài khoản nguồn và tài khoản đích vẫn tách đúng.
            query = query.where(GlPosting.bank_account_id == bank_account_id)

    if from_date is not None:
        query = query.where(Voucher.posting_date >= from_date)
    if to_date is not None:
        query = query.where(Voucher.posting_date <= to_date)
    if status is not None:
        query = query.where(Voucher.status == status)
    return query.group_by(*base_columns)
