"""Job `posting.opening_balances.carry_forward` — chuyển số dư sang năm sau (FR-OPB-010).

Chạy cho **cả hai sổ** trong một lượt: FR-OPB-010 nói "tự động", và một lượt
chuyển chỉ sổ tài chính sẽ để sổ quản trị năm mới trống mà không ai nhận ra tới
kỳ báo cáo quản trị đầu tiên (LD-07: hai sổ độc lập nhưng cùng nhịp năm).

Phần tính là **một câu SQL cho mỗi sổ** (`sql/carry_forward.sql`, LD-14); Python
chỉ giữ hai việc không SQL hóa được rẻ: sinh UUIDv7 cho dòng hóa đơn chuyển theo
(ADR-012 — PostgreSQL 16 không có uuid7), và quyết định dòng hóa đơn nào còn
khớp được vào dòng cha năm mới.

Per-branch dưới phạm vi RLS của người xếp hàng, như job recalc (chốt bàn giao
4A→4B): chuyển số dư của chi nhánh nào là việc của người đứng ở chi nhánh đó.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from importlib import resources
from typing import Final

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.errors import (
    DomainError,
    OpeningCarryForwardExistsError,
    OpeningCarryForwardTargetMissingError,
)
from ket.kernel.identifiers import uuid7
from ket.kernel.jobs.models import ResumeSemantics
from ket.kernel.jobs.registry import REGISTRY, JobContext, JobResult, JobType
from ket.kernel.money import ZERO
from ket.kernel.periods.models import FiscalYear
from ket.posting.balances.recalc_queue import mark_dirty
from ket.posting.engine.models import Ledger
from ket.posting.opening_balances.guards import (
    acquire_opening_write_lock,
    guard_opening_writable,
)
from ket.posting.opening_balances.import_job import OPENING_WRITE
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)

CARRY_FORWARD_CODE: Final[str] = "posting.opening_balances.carry_forward"

_CARRY_FORWARD_SQL: Final[str] = (
    resources.files("ket.posting.opening_balances")
    .joinpath("sql/carry_forward.sql")
    .read_text("utf-8")
)
"""Câu SQL đủ, không còn chỗ trống nào để `format`.

Bản 6D phải ghép một dãy `VALUES` cho CTE `bank_movements` vì `gl_postings`
chưa mang chiều TK ngân hàng — phát sinh 112x phải hỏi module `bank` qua
Protocol rồi cộng-trừ hai lượt ngay trong câu. Lát 6G-1 đưa chiều ấy thành cột
thật nên cả đường vòng biến mất, và cùng nó là phép trừ sai đã đẻ ra dòng dư
đầu năm bịa (review 6G-1 H-2).
"""


_CARRIED_KINDS: Final[tuple[int, ...]] = (
    OpeningDetailKind.ACCOUNT,
    OpeningDetailKind.BANK,
    OpeningDetailKind.RECEIVABLE,
    OpeningDetailKind.PAYABLE,
    OpeningDetailKind.EMPLOYEE_ADVANCE,
)
"""Nhóm mà lượt chuyển 4C sở hữu (0–4). Nhóm 5–9 chuyển ở phase 8 cạnh schema
của chúng — cảnh báo tương ứng nằm ngay trong `sql/carry_forward.sql`."""

_INVOICE_KINDS: Final[tuple[int, ...]] = (
    OpeningDetailKind.RECEIVABLE,
    OpeningDetailKind.PAYABLE,
    OpeningDetailKind.EMPLOYEE_ADVANCE,
)


class CarryForwardParams(BaseModel):
    """Tham số lượt chuyển: từ năm nào; ghi đè phải là lựa chọn tường minh."""

    from_fiscal_year_id: int = Field(ge=1)
    overwrite: bool = False


def _target_year_of(session: Session, source: FiscalYear) -> FiscalYear:
    """Năm nhận = năm bắt đầu ngay ngày kế tiếp ngày kết thúc năm nguồn."""
    target = (
        session.execute(
            select(FiscalYear).where(FiscalYear.start_date == source.end_date + timedelta(days=1))
        )
        .scalars()
        .first()
    )
    if target is None:
        raise OpeningCarryForwardTargetMissingError(
            "Chưa có năm tài chính liền sau để nhận số dư — tạo năm mới ở màn hình "
            "năm tài chính trước",
            source_fiscal_year_id=source.id,
        )
    return target


def _carry_invoices(
    session: Session, *, source_year_id: int, target_year_id: int, ledger: int, branch_id: int
) -> tuple[int, int, int]:
    """Chuyển chi tiết hóa đơn/tạm ứng còn nợ theo dòng cha năm mới.

    Trả `(số dòng chuyển được, số dòng rơi, số dòng cha bị hóa đơn vượt dư)`.
    Một dòng hóa đơn **rơi** khi năm mới không còn dòng cha cho đối tượng đó
    (dư đã về 0 — các khoản thu/chi trong năm nguồn đã bù hết). Một dòng cha
    **bị vượt** khi tổng hóa đơn treo dưới nó lớn hơn dư bên còn-nợ của chính
    nó (review 4C, M2): `paid_amount` chưa sống tới phase 7 nên các khoản trả
    trong năm trừ vào dư cha mà không trừ vào hóa đơn nào — FR-OPB-007 sẽ lệch
    đúng bằng chỗ đó, và con số này cho người dùng thấy lệch NGAY trên kết quả
    job thay vì ở một báo cáo đối chiếu sau này. Cả ba con số phải tiến về
    hành xử đúng ở phase 7 (đối trừ theo hóa đơn thật).
    """
    parent = OpeningBalance
    rows = session.execute(
        select(
            OpeningBalanceInvoice.invoice_no,
            OpeningBalanceInvoice.invoice_date,
            OpeningBalanceInvoice.due_date,
            OpeningBalanceInvoice.amount_fc,
            OpeningBalanceInvoice.amount,
            OpeningBalanceInvoice.paid_amount,
            OpeningBalanceInvoice.paid_amount_fc,
            parent.detail_kind,
            parent.account_id,
            parent.partner_id,
            parent.currency_code,
        )
        .join(parent, OpeningBalanceInvoice.opening_balance_id == parent.id)
        .where(parent.fiscal_year_id == source_year_id)
        .where(parent.ledger == ledger)
        .where(parent.branch_id == branch_id)
        .where(parent.detail_kind.in_(_INVOICE_KINDS))
        # Lọc theo NGUYÊN TỆ — cùng trục với đối trừ (`paid_amount_fc`, lát 6B):
        # hai cơ sở lọc khác nhau cho cùng bất biến "còn nợ" là chỗ hóa đơn tỷ
        # giá lẻ trả hết FC nhưng dư vài đồng VND bị chuyển năm nhầm (L-1 6B).
        .where(OpeningBalanceInvoice.amount_fc > OpeningBalanceInvoice.paid_amount_fc)
    ).all()
    if not rows:
        return 0, 0, 0

    # Dòng cha năm mới nhận hóa đơn = dòng cùng (nhóm, TK, đối tượng, tiền tệ)
    # và KHÔNG mang chiều nào khác — dòng mang chiều (công trình, đơn hàng…)
    # sinh từ phát sinh có chiều, không phải chỗ treo chi tiết công nợ.
    targets = {
        (row.detail_kind, row.account_id, row.partner_id, row.currency_code): row
        for row in session.execute(
            select(
                parent.id,
                parent.detail_kind,
                parent.account_id,
                parent.partner_id,
                parent.currency_code,
                parent.debit,
                parent.credit,
            )
            .where(parent.fiscal_year_id == target_year_id)
            .where(parent.ledger == ledger)
            .where(parent.branch_id == branch_id)
            .where(parent.detail_kind.in_(_INVOICE_KINDS))
            .where(parent.cost_object_id.is_(None))
            .where(parent.project_id.is_(None))
            .where(parent.order_id.is_(None))
            .where(parent.contract_id.is_(None))
            .where(parent.expense_item_id.is_(None))
            .where(parent.item_id.is_(None))
            .where(parent.warehouse_id.is_(None))
            .where(parent.lot_id.is_(None))
        ).all()
    }

    # Core executemany chứ không ORM `add_all`: chuyển năm là đường ghi hàng
    # loạt như nhập liệu — H84 đã chọn một dòng nhật ký tổng thay vì mỗi hóa
    # đơn một dòng "rỗng → giá trị" chôn mất lần sửa tay thật.
    carried: list[dict[str, object]] = []
    carried_total_by_parent: dict[int, Decimal] = {}
    dropped = 0
    for row in rows:
        target = targets.get((row.detail_kind, row.account_id, row.partner_id, row.currency_code))
        if target is None:
            dropped += 1
            continue
        remaining: Decimal = row.amount - row.paid_amount
        # Nguyên tệ còn nợ đọc THẲNG từ `paid_amount_fc` (lát 6B) — trước đây
        # phải ước lượng tỷ lệ qua VND vì cột chưa tồn tại; phép chia-nhân-làm-
        # tròn đó lệch được vài xu trên hóa đơn đối trừ từng phần.
        remaining_fc: Decimal = row.amount_fc - row.paid_amount_fc
        carried.append(
            {
                "id": uuid7(),
                "opening_balance_id": target.id,
                "invoice_no": row.invoice_no,
                "invoice_date": row.invoice_date,
                "due_date": row.due_date,
                "amount_fc": remaining_fc,
                "amount": remaining,
                "paid_amount": ZERO,
                "paid_amount_fc": ZERO,
            }
        )
        carried_total_by_parent[target.id] = (
            carried_total_by_parent.get(target.id, ZERO) + remaining
        )
    if carried:
        session.execute(insert(OpeningBalanceInvoice), carried)

    parents_by_id = {row.id: row for row in targets.values()}
    overrun = 0
    for parent_id, invoice_total in carried_total_by_parent.items():
        target = parents_by_id[parent_id]
        natural_amount = (
            target.credit if target.detail_kind == OpeningDetailKind.PAYABLE else target.debit
        )
        if invoice_total > natural_amount:
            overrun += 1
    return len(carried), dropped, overrun


def run_carry_forward(context: JobContext, params: CarryForwardParams) -> JobResult:
    """Chuyển số dư (năm nguồn → năm liền sau) cho chi nhánh đang thao tác."""
    if context.branch_id is None:
        raise DomainError("Lượt chuyển số dư phải được xếp hàng từ một chi nhánh đang thao tác")
    branch_id = context.branch_id
    session = context.session

    source = session.get(FiscalYear, params.from_fiscal_year_id)
    if source is None:
        raise DomainError(
            "Năm tài chính nguồn không tồn tại", fiscal_year_id=params.from_fiscal_year_id
        )
    target = _target_year_of(session, source)

    # Khóa CẢ năm nguồn lẫn năm đích (review 4C, M1): job đọc nguồn qua nhiều
    # câu lệnh READ COMMITTED — một lượt nhập lại số dư năm nguồn chen giữa sẽ
    # cho hai sổ chuyển từ hai thời điểm khác nhau. Thứ tự cố định nguồn→đích
    # (năm nguồn luôn bắt đầu trước) nên hai lượt chuyển nối năm không deadlock.
    # Phát sinh ghi sổ vào năm nguồn thì không khóa nào chặn được — quy trình
    # chuẩn vẫn là chạy lại với ghi đè sau khi khóa sổ năm (10a).
    acquire_opening_write_lock(
        session,
        dataset_schema=context.dataset_schema,
        branch_id=branch_id,
        fiscal_year_id=source.id,
    )
    acquire_opening_write_lock(
        session,
        dataset_schema=context.dataset_schema,
        branch_id=branch_id,
        fiscal_year_id=target.id,
    )
    first_period = guard_opening_writable(session, target, for_share=True)

    existing = session.execute(
        select(func.count())
        .select_from(OpeningBalance)
        .where(OpeningBalance.fiscal_year_id == target.id)
        .where(OpeningBalance.branch_id == branch_id)
        .where(OpeningBalance.detail_kind.in_(_CARRIED_KINDS))
    ).scalar_one()
    if existing > 0 and not params.overwrite:
        raise OpeningCarryForwardExistsError(
            "Năm nhận đã có số dư ban đầu — chạy lại với xác nhận ghi đè nếu muốn "
            "thay bằng số cuối năm nguồn",
            target_fiscal_year_id=target.id,
            existing_rows=existing,
        )
    if existing > 0:
        session.execute(
            delete(OpeningBalance)
            .where(OpeningBalance.fiscal_year_id == target.id)
            .where(OpeningBalance.branch_id == branch_id)
            .where(OpeningBalance.detail_kind.in_(_CARRIED_KINDS))
        )

    rows_by_ledger: dict[int, int] = {}
    invoices_carried = 0
    invoices_dropped = 0
    parents_overrun = 0
    ledgers = (int(Ledger.FINANCIAL), int(Ledger.MANAGEMENT))
    for step, ledger in enumerate(ledgers, start=1):
        # Chiều TK ngân hàng (kind 1) đi cùng đường với mọi chiều khác: cột
        # `gl_postings.bank_account_id` (lát 6G-1). Bản 6D phải hỏi module bank
        # qua Protocol rồi cộng-trừ hai lượt trong SQL vì cột ấy chưa tồn tại —
        # và phép trừ đó sai khi dòng nguồn mang thêm chiều khác (review 6G-1
        # H-2).
        session.execute(
            text(_CARRY_FORWARD_SQL),
            {
                "source_fiscal_year_id": source.id,
                "target_fiscal_year_id": target.id,
                "ledger": ledger,
                "branch_id": branch_id,
            },
        )
        # Đếm lại thay vì tin `rowcount`: với INSERT … SELECT driver này trả
        # `-1` (cùng bài học `pipeline._insert_one_level`).
        rows_by_ledger[ledger] = int(
            session.execute(
                select(func.count())
                .select_from(OpeningBalance)
                .where(OpeningBalance.fiscal_year_id == target.id)
                .where(OpeningBalance.ledger == ledger)
                .where(OpeningBalance.branch_id == branch_id)
                .where(OpeningBalance.detail_kind.in_(_CARRIED_KINDS))
            ).scalar_one()
        )
        carried, dropped, overrun = _carry_invoices(
            session,
            source_year_id=source.id,
            target_year_id=target.id,
            ledger=ledger,
            branch_id=branch_id,
        )
        invoices_carried += carried
        invoices_dropped += dropped
        parents_overrun += overrun
        # Bàn giao 4B: mọi đường ghi `opening_balances` đánh dấu bẩn kỳ đầu năm.
        mark_dirty(
            session,
            ledger=ledger,
            branch_id=branch_id,
            from_period_id=first_period.id,
            reason=f"carry_forward {source.code}->{target.code}",
        )
        context.progress.report(step * 100 // len(ledgers), f"Đã chuyển sổ {step}/{len(ledgers)}")

    record_action(
        session,
        entity_type="opening_balances",
        entity_id=str(context.job_id),
        action=AuditAction.CREATED,
        branch_id=branch_id,
        new_values={
            "source_fiscal_year_id": source.id,
            "target_fiscal_year_id": target.id,
            "rows_financial": rows_by_ledger[int(Ledger.FINANCIAL)],
            "rows_management": rows_by_ledger[int(Ledger.MANAGEMENT)],
            "invoices_carried": invoices_carried,
            "invoices_dropped": invoices_dropped,
            "invoice_overrun_parents": parents_overrun,
            "overwrote_existing": existing,
        },
    )
    return {
        "target_fiscal_year_id": target.id,
        "rows_financial": rows_by_ledger[int(Ledger.FINANCIAL)],
        "rows_management": rows_by_ledger[int(Ledger.MANAGEMENT)],
        "invoices_carried": invoices_carried,
        "invoices_dropped": invoices_dropped,
        "invoice_overrun_parents": parents_overrun,
    }


CARRY_FORWARD_JOB: Final[JobType[CarryForwardParams]] = JobType(
    code=CARRY_FORWARD_CODE,
    permission=OPENING_WRITE,
    # Thay-trọn trong một transaction: chạy lại từ đầu luôn cho cùng kết quả;
    # lượt dở dang rollback sạch (advisory lock + hàng đợi đã lo phần tuần tự).
    resume_semantics=ResumeSemantics.IDEMPOTENT_RESTART,
    params_model=CarryForwardParams,
    handler=run_carry_forward,
    description="Chuyển số dư cuối năm thành số dư đầu năm sau, giữ chi tiết công nợ",
)

REGISTRY.register(CARRY_FORWARD_JOB)
