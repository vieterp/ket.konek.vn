"""Kiểm và ghi số dư ban đầu nhóm 0–4 (SRS 02, RT-24) — phần cần DB.

Lượt kiểm và lượt ghi chạy **cùng** đường này, khác nhau đúng tham số `commit`
(nguyên tắc H85 của khung nhập liệu: bước ghi không có đoạn mã riêng nào để bỏ
qua phép kiểm). Hình dạng dữ liệu ghi ra:

* mỗi tổ hợp `(nhóm, đối tượng, TK, tiền tệ, tỷ giá)` = **một** dòng
  `opening_balances` — dòng cha mà FR-OPB-007 đối chiếu;
* mỗi dòng Excel "còn nợ" của nhóm 2/3/4 = một dòng `opening_balance_invoices`
  treo dưới dòng cha (FR-OPB-003). Vì dòng cha được **cộng từ chính các dòng
  con**, phép đối chiếu chi tiết ↔ tổng hợp đúng theo cách dựng, không cần một
  phép kiểm sau.

Ghi bằng Core (`insert(...).returning`) chứ không ORM: một lượt nhập vài nghìn
dòng qua unit-of-work sẽ đẻ vài nghìn dòng `audit_log` "rỗng → giá trị" chôn mất
lần sửa tay thật — đúng lý do H84 đã chọn một dòng nhật ký `IMPORTED` trỏ vào
tệp trong kho định địa chỉ theo nội dung.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.config.accounts_models import (
    DEPOSIT_ACCOUNT_CODE_PREFIX,
    BalanceNature,
    ChartOfAccount,
    DetailTracking,
)
from ket.kernel.config.accounts_provider import resolve_package
from ket.kernel.currency.models import Currency
from ket.kernel.errors import OpeningBalanceSettledError
from ket.kernel.identifiers import uuid7
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.money import ZERO
from ket.kernel.periods.models import FiscalYear
from ket.posting.balances.recalc_queue import mark_dirty
from ket.posting.engine.dimensions import PartnerKind
from ket.posting.opening_balances.guards import (
    acquire_opening_write_lock,
    first_period_of,
    guard_opening_writable,
)
from ket.posting.opening_balances.models import (
    OpeningBalance,
    OpeningBalanceInvoice,
    OpeningDetailKind,
)
from ket.posting.opening_balances.parsing import ParsedOpeningRow
from ket.posting.opening_balances.report import (
    OpeningImportReport,
    OpeningImportWarning,
    OpeningRowError,
)

PARTNER_KIND_BY_DETAIL: Final[dict[int, int]] = {
    OpeningDetailKind.RECEIVABLE: PartnerKind.CUSTOMER,
    OpeningDetailKind.PAYABLE: PartnerKind.VENDOR,
    OpeningDetailKind.EMPLOYEE_ADVANCE: PartnerKind.EMPLOYEE,
}

_PARTNER_TRACKING_BY_DETAIL: Final[dict[int, str]] = {
    OpeningDetailKind.RECEIVABLE: DetailTracking.CUSTOMER,
    OpeningDetailKind.PAYABLE: DetailTracking.VENDOR,
    OpeningDetailKind.EMPLOYEE_ADVANCE: DetailTracking.EMPLOYEE,
}

_PARTNER_TRACKINGS: Final[frozenset[str]] = frozenset(
    {DetailTracking.CUSTOMER, DetailTracking.VENDOR, DetailTracking.EMPLOYEE}
)

_SHEET_BY_TRACKING: Final[dict[str, str]] = {
    DetailTracking.CUSTOMER: "Phải thu khách hàng",
    DetailTracking.VENDOR: "Phải trả nhà cung cấp",
    DetailTracking.EMPLOYEE: "Tạm ứng nhân viên",
}


@dataclass
class _AggregatedRow:
    """Một dòng `opening_balances` sẽ ghi, cộng dồn từ các dòng Excel cùng khóa."""

    kind: int
    account_id: int
    partner_id: int | None
    bank_account_id: int | None
    currency_code: str
    exchange_rate: Decimal
    debit_fc: Decimal = ZERO
    credit_fc: Decimal = ZERO
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    invoices: list[ParsedOpeningRow] = field(default_factory=list)


@dataclass(frozen=True)
class StagedOpening:
    """Kết quả kiểm đã sẵn sàng ghi — đầu vào duy nhất của `write_staged`."""

    rows: list[_AggregatedRow]
    replaced_kinds: tuple[int, ...]


class _Lookups:
    """Ba bảng tra của một lượt nhập — mỗi bảng nạp bằng **một** truy vấn.

    Bản ghi riêng chi nhánh đè bản dùng chung trùng mã (nạp theo thứ tự
    `branch_id NULLS FIRST` rồi ghi đè vào dict) — cùng luật ưu tiên mà màn
    hình danh mục hiển thị cho người đứng ở chi nhánh đó.
    """

    def __init__(
        self, session: Session, fiscal_year: FiscalYear, rows: list[ParsedOpeningRow]
    ) -> None:
        package = resolve_package(
            session, scheme=fiscal_year.accounting_scheme, on_date=fiscal_year.start_date
        )
        self.package_id = package.id
        self.accounts: dict[str, ChartOfAccount] = {
            row.code: row
            for row in session.execute(
                select(ChartOfAccount).where(ChartOfAccount.package_id == package.id)
            )
            .scalars()
            .all()
        }
        partner_codes = {
            row.partner_code
            for row in rows
            if row.partner_code is not None
            and row.kind in (OpeningDetailKind.RECEIVABLE, OpeningDetailKind.PAYABLE)
        }
        employee_codes = {
            row.partner_code
            for row in rows
            if row.partner_code is not None and row.kind == OpeningDetailKind.EMPLOYEE_ADVANCE
        }
        self.partners: dict[str, Partner] = {}
        if partner_codes:
            for partner in (
                session.execute(
                    select(Partner)
                    .where(Partner.code.in_(partner_codes))
                    .order_by(Partner.branch_id.asc().nulls_first())
                )
                .scalars()
                .all()
            ):
                self.partners[partner.code] = partner
        self.employees: dict[str, Employee] = {}
        if employee_codes:
            for employee in (
                session.execute(
                    select(Employee)
                    .where(Employee.code.in_(employee_codes))
                    .order_by(Employee.branch_id.asc().nulls_first())
                )
                .scalars()
                .all()
            ):
                self.employees[employee.code] = employee
        bank_account_codes = {
            row.bank_account_code for row in rows if row.bank_account_code is not None
        }
        self.bank_accounts: dict[str, CompanyBankAccount] = {}
        if bank_account_codes:
            for bank_account in (
                session.execute(
                    select(CompanyBankAccount)
                    .where(CompanyBankAccount.code.in_(bank_account_codes))
                    .order_by(CompanyBankAccount.branch_id.asc().nulls_first())
                )
                .scalars()
                .all()
            ):
                self.bank_accounts[bank_account.code] = bank_account
        self.base_currency = fiscal_year.base_currency
        # Tiền tệ tra danh mục như mọi mã khác (review 4C, M7): không có FK trên
        # `currency_code`, nên một mã gõ nhầm sẽ ghi vào DB và chỉ lộ ra ở phase
        # đánh giá lại tỷ giá — nơi không còn ai nhớ nó từ tệp nào.
        self.currencies: frozenset[str] = frozenset(
            session.execute(select(Currency.code)).scalars().all()
        )


def _account_errors(
    row: ParsedOpeningRow, account: ChartOfAccount | None
) -> list[tuple[str | None, str, str]]:
    """Lỗi tài khoản của một dòng: `(cột, mã lỗi, thông điệp)`."""
    header = "Số hiệu tài khoản *"
    if account is None:
        return [
            (
                header,
                "opening.account_unknown",
                "Số hiệu tài khoản không có trong hệ thống tài khoản của chế độ kế toán năm",
            )
        ]
    errors: list[tuple[str | None, str, str]] = []
    if account.is_summary:
        errors.append(
            (
                header,
                "opening.account_summary",
                "Tài khoản tổng hợp không nhập số dư — dùng tài khoản chi tiết (BR-SYS-03)",
            )
        )
    if account.is_inactive:
        errors.append((header, "opening.account_inactive", "Tài khoản đã ngừng sử dụng"))
    if account.balance_nature == BalanceNature.NONE:
        errors.append(
            (
                header,
                "opening.account_no_balance",
                "Tài khoản loại kết chuyển cuối kỳ không có số dư đầu kỳ",
            )
        )
    elif account.balance_nature == BalanceNature.DEBIT and (
        row.credit > ZERO or row.credit_fc > ZERO
    ):
        errors.append(
            (
                None,
                "opening.nature_mismatch",
                "Tài khoản tính chất Dư Nợ không nhập số dư Có (BR-OPB-03)",
            )
        )
    elif account.balance_nature == BalanceNature.CREDIT and (
        row.debit > ZERO or row.debit_fc > ZERO
    ):
        errors.append(
            (
                None,
                "opening.nature_mismatch",
                "Tài khoản tính chất Dư Có không nhập số dư Nợ (BR-OPB-03)",
            )
        )

    tracking = frozenset(account.detail_tracking or ())
    partner_tracking = tracking & _PARTNER_TRACKINGS
    if row.kind == OpeningDetailKind.ACCOUNT and partner_tracking:
        sheets = ", ".join(repr(_SHEET_BY_TRACKING[item]) for item in sorted(partner_tracking))
        errors.append(
            (
                header,
                "opening.needs_partner_sheet",
                f"Tài khoản theo dõi chi tiết theo đối tượng — nhập ở sheet {sheets}",
            )
        )
    expected_tracking = _PARTNER_TRACKING_BY_DETAIL.get(row.kind)
    if (
        expected_tracking is not None
        and partner_tracking
        and expected_tracking not in partner_tracking
    ):
        errors.append(
            (
                header,
                "opening.wrong_partner_sheet",
                "Tài khoản không theo dõi loại đối tượng của sheet này — kiểm tra lại số hiệu",
            )
        )
    return errors


def _partner_errors(
    row: ParsedOpeningRow, lookups: _Lookups
) -> tuple[int | None, list[tuple[str | None, str, str]]]:
    """`(partner_id, lỗi)` cho các sheet công nợ; sheet TK thường trả `(None, [])`."""
    if row.kind == OpeningDetailKind.ACCOUNT or row.partner_code is None:
        return None, []
    if row.kind == OpeningDetailKind.EMPLOYEE_ADVANCE:
        employee = lookups.employees.get(row.partner_code)
        header = "Mã nhân viên *"
        if employee is None:
            return None, [
                (header, "opening.employee_unknown", "Mã nhân viên không có trong danh mục")
            ]
        if employee.is_group:
            return None, [
                (header, "opening.partner_is_group", "Đây là một nhóm — chọn một nhân viên cụ thể")
            ]
        return employee.id, []
    partner = lookups.partners.get(row.partner_code)
    header = "Mã khách hàng *" if row.kind == OpeningDetailKind.RECEIVABLE else "Mã nhà cung cấp *"
    if partner is None:
        return None, [(header, "opening.partner_unknown", "Mã đối tác không có trong danh mục")]
    if partner.is_group:
        return None, [
            (header, "opening.partner_is_group", "Đây là một nhóm — chọn một đối tác cụ thể")
        ]
    if row.kind == OpeningDetailKind.RECEIVABLE and not partner.is_customer:
        return None, [
            (
                header,
                "opening.partner_not_customer",
                "Đối tác chưa đánh dấu là khách hàng (FR-SYS-031)",
            )
        ]
    if row.kind == OpeningDetailKind.PAYABLE and not partner.is_vendor:
        return None, [
            (
                header,
                "opening.partner_not_vendor",
                "Đối tác chưa đánh dấu là nhà cung cấp (FR-SYS-031)",
            )
        ]
    return partner.id, []


def _bank_account_errors(
    row: ParsedOpeningRow, account: ChartOfAccount | None, lookups: _Lookups
) -> tuple[int | None, list[tuple[str | None, str, str]]]:
    """`(bank_account_id, lỗi)` cho sheet ngân hàng (kind 1, lát 6D).

    Tiền tệ của dòng phải đúng tiền tệ của TK ngân hàng (`NULL` = đồng hạch
    toán): một TK USD mang số dư VND là dữ liệu tự mâu thuẫn — số dư ấy sẽ
    không bao giờ khớp được với sao kê của chính tài khoản đó (BR-BNK-01).
    """
    if row.kind != OpeningDetailKind.BANK:
        return None, []
    errors: list[tuple[str | None, str, str]] = []
    if account is not None and not account.code.startswith(DEPOSIT_ACCOUNT_CODE_PREFIX):
        errors.append(
            (
                "Số hiệu tài khoản *",
                "opening.bank_account_not_deposit",
                "Sheet ngân hàng chỉ nhận tài khoản nhóm 112 (tiền gửi ngân hàng)",
            )
        )
    if row.bank_account_code is None:
        return None, errors
    header = "Số tài khoản ngân hàng *"
    bank_account = lookups.bank_accounts.get(row.bank_account_code)
    if bank_account is None:
        return None, [
            *errors,
            (
                header,
                "opening.bank_account_unknown",
                "Số tài khoản không có trong danh mục tài khoản ngân hàng của doanh nghiệp",
            ),
        ]
    if bank_account.is_group:
        return None, [
            *errors,
            (header, "opening.partner_is_group", "Đây là một nhóm — chọn một tài khoản cụ thể"),
        ]
    if not bank_account.is_active:
        return None, [
            *errors,
            (header, "opening.bank_account_inactive", "Tài khoản ngân hàng đã ngừng theo dõi"),
        ]
    expected_currency = bank_account.currency_code or lookups.base_currency
    if row.currency_code != expected_currency:
        errors.append(
            (
                "Loại tiền",
                "opening.bank_account_currency_mismatch",
                f"Tài khoản ngân hàng này hạch toán bằng {expected_currency} — "
                "loại tiền của dòng phải khớp",
            )
        )
    return bank_account.id, errors


def validate_rows(
    session: Session,
    *,
    fiscal_year: FiscalYear,
    ledger: int,
    branch_id: int,
    rows: list[ParsedOpeningRow],
    failed: set[tuple[str, int]],
    report: OpeningImportReport,
) -> StagedOpening:
    """Kiểm tra cứu + gộp dòng cha; điền phần thống kê của báo cáo.

    Chạy **mọi** phép kiểm trên **mọi** dòng kể cả dòng đã hỏng hình thức —
    nguyên tắc trả-toàn-bộ-lỗi của phase 4: người dùng sửa tệp một lượt.
    """
    lookups = _Lookups(session, fiscal_year, rows)
    aggregated: dict[tuple[int, int, int | None, int | None, str, Decimal], _AggregatedRow] = {}
    seen_invoices: dict[tuple[int, str | None, str, str, str], tuple[str, int]] = {}
    tracking_warned: set[str] = set()
    deposit_warned: set[str] = set()

    for row in rows:
        account = lookups.accounts.get(row.account_code)
        row_errors = _account_errors(row, account)
        partner_id, partner_issues = _partner_errors(row, lookups)
        row_errors.extend(partner_issues)
        bank_account_id, bank_issues = _bank_account_errors(row, account, lookups)
        row_errors.extend(bank_issues)
        if row.currency_code not in lookups.currencies:
            row_errors.append(
                (
                    "Loại tiền",
                    "opening.currency_unknown",
                    "Loại tiền không có trong danh mục tiền tệ",
                )
            )
        for column, code, message in row_errors:
            report.add(
                OpeningRowError(
                    sheet=row.sheet, row=row.row, column=column, code=code, message=message
                )
            )
            failed.add((row.sheet, row.row))

        if (
            account is not None
            and row.kind == OpeningDetailKind.ACCOUNT
            and account.code.startswith(DEPOSIT_ACCOUNT_CODE_PREFIX)
            and account.code not in deposit_warned
        ):
            # Cảnh báo chứ không lỗi: dữ liệu nhập trước lát 6D nằm ở sheet Số
            # dư tài khoản là hợp lệ — nhưng phần 112 ngoài nhóm 1 thì
            # BR-BNK-01 không đối chiếu theo TK ngân hàng được.
            deposit_warned.add(account.code)
            report.warnings.append(
                OpeningImportWarning(
                    code="opening.deposit_on_account_sheet",
                    message=(
                        f"TK {account.code} là tiền gửi ngân hàng — nên nhập ở sheet "
                        "'Số dư ngân hàng' để chi tiết theo từng tài khoản (BR-BNK-01)"
                    ),
                )
            )

        if account is not None:
            uncaptured = frozenset(account.detail_tracking or ()) - _PARTNER_TRACKINGS
            if uncaptured and account.code not in tracking_warned:
                tracking_warned.add(account.code)
                report.warnings.append(
                    OpeningImportWarning(
                        code="opening.tracking_not_captured",
                        message=(
                            f"TK {account.code} theo dõi chiều {', '.join(sorted(uncaptured))} "
                            "— chiều này chưa nhập được ở số dư ban đầu nhóm 0–4"
                        ),
                    )
                )

        if row.invoice_no is not None and row.is_natural_side:
            invoice_key = (
                row.kind,
                row.partner_code,
                row.account_code,
                row.currency_code,
                row.invoice_no,
            )
            first = seen_invoices.get(invoice_key)
            if first is not None:
                report.add(
                    OpeningRowError(
                        sheet=row.sheet,
                        row=row.row,
                        column=None,
                        code="opening.duplicate_invoice",
                        message=(
                            "Số chứng từ trùng với dòng "
                            f"{first[1]} của cùng đối tượng — mỗi chứng từ một dòng"
                        ),
                        value=row.invoice_no,
                    )
                )
                failed.add((row.sheet, row.row))
            else:
                seen_invoices[invoice_key] = (row.sheet, row.row)

        if (row.sheet, row.row) in failed or account is None:
            continue

        key = (
            row.kind,
            account.id,
            partner_id,
            bank_account_id,
            row.currency_code,
            row.exchange_rate,
        )
        bucket = aggregated.get(key)
        if bucket is None:
            bucket = _AggregatedRow(
                kind=row.kind,
                account_id=account.id,
                partner_id=partner_id,
                bank_account_id=bank_account_id,
                currency_code=row.currency_code,
                exchange_rate=row.exchange_rate,
            )
            aggregated[key] = bucket
        bucket.debit_fc += row.debit_fc
        bucket.credit_fc += row.credit_fc
        bucket.debit += row.debit
        bucket.credit += row.credit
        # Chỉ nhóm công nợ (2/3/4) treo chi tiết chứng từ; nhóm ngân hàng (1)
        # có dòng dư Có hợp lệ (thấu chi) nhưng không có hóa đơn để treo.
        if row.kind in PARTNER_KIND_BY_DETAIL and row.is_natural_side:
            bucket.invoices.append(row)

    report.error_rows = len(failed)
    replaced = tuple(sorted({row.kind for row in rows}))
    staged_rows = list(aggregated.values())

    for bucket in staged_rows:
        report.rows_by_kind[bucket.kind] = report.rows_by_kind.get(bucket.kind, 0) + 1
        report.invoice_rows += len(bucket.invoices)
    report.replaced_kinds = list(replaced)

    untouched = session.execute(
        select(
            func.coalesce(func.sum(OpeningBalance.debit), 0),
            func.coalesce(func.sum(OpeningBalance.credit), 0),
        )
        .where(OpeningBalance.fiscal_year_id == fiscal_year.id)
        .where(OpeningBalance.ledger == ledger)
        .where(OpeningBalance.branch_id == branch_id)
        .where(OpeningBalance.detail_kind.not_in(replaced or (-1,)))
    ).one()
    report.total_debit = Decimal(untouched[0]) + sum((bucket.debit for bucket in staged_rows), ZERO)
    report.total_credit = Decimal(untouched[1]) + sum(
        (bucket.credit for bucket in staged_rows), ZERO
    )
    if report.total_debit != report.total_credit:
        report.warnings.append(
            OpeningImportWarning(
                code="opening.unbalanced",
                message=(
                    "Tổng dư Nợ và tổng dư Có sau lượt ghi lệch nhau "
                    f"{abs(report.total_debit - report.total_credit)} (BR-OPB-01) — "
                    "kiểm tra toàn vẹn sẽ chặn ghi sổ chứng từ tới khi cân"
                ),
            )
        )
    return StagedOpening(rows=staged_rows, replaced_kinds=replaced)


def write_staged(
    session: Session,
    *,
    staged: StagedOpening,
    fiscal_year: FiscalYear,
    ledger: int,
    branch_id: int,
    dataset_schema: str,
    job_id: UUID,
    file_name: str,
    content_hash: str,
) -> None:
    """Thay trọn các nhóm có mặt trong tệp, trong transaction của người gọi.

    Thứ tự khóa cố định cho mọi đường ghi số dư (advisory trước, `FOR SHARE`
    kỳ sau) — carry-forward dùng cùng thứ tự nên hai đường không deadlock nhau.
    """
    acquire_opening_write_lock(
        session,
        dataset_schema=dataset_schema,
        branch_id=branch_id,
        fiscal_year_id=fiscal_year.id,
    )
    first_period = guard_opening_writable(session, fiscal_year, for_share=True)
    ensure_groups_not_settled(
        session,
        fiscal_year_id=fiscal_year.id,
        ledger=ledger,
        branch_id=branch_id,
        detail_kinds=tuple(staged.replaced_kinds),
    )

    session.execute(
        delete(OpeningBalance)
        .where(OpeningBalance.fiscal_year_id == fiscal_year.id)
        .where(OpeningBalance.ledger == ledger)
        .where(OpeningBalance.branch_id == branch_id)
        .where(OpeningBalance.detail_kind.in_(staged.replaced_kinds))
    )

    if staged.rows:
        parent_values = [
            {
                "fiscal_year_id": fiscal_year.id,
                "ledger": ledger,
                "branch_id": branch_id,
                "account_id": bucket.account_id,
                "currency_code": bucket.currency_code,
                "exchange_rate": bucket.exchange_rate,
                "partner_id": bucket.partner_id,
                "partner_kind": PARTNER_KIND_BY_DETAIL.get(bucket.kind),
                "bank_account_id": bucket.bank_account_id,
                "debit_fc": bucket.debit_fc,
                "credit_fc": bucket.credit_fc,
                "debit": bucket.debit,
                "credit": bucket.credit,
                "detail_kind": bucket.kind,
            }
            for bucket in staged.rows
        ]
        inserted = session.execute(
            insert(OpeningBalance).returning(OpeningBalance.id, sort_by_parameter_order=True),
            parent_values,
        )
        parent_ids = [row[0] for row in inserted]
        invoice_values = [
            {
                "id": uuid7(),
                "opening_balance_id": parent_id,
                "branch_id": branch_id,
                "invoice_no": invoice.invoice_no,
                "invoice_date": invoice.invoice_date,
                "due_date": invoice.due_date,
                "amount_fc": invoice.debit_fc + invoice.credit_fc,
                "amount": invoice.debit + invoice.credit,
                "paid_amount": ZERO,
            }
            for parent_id, bucket in zip(parent_ids, staged.rows, strict=True)
            for invoice in bucket.invoices
        ]
        if invoice_values:
            session.execute(insert(OpeningBalanceInvoice), invoice_values)

    # Bàn giao 4B: mọi đường ghi `opening_balances` phải đánh dấu bẩn kỳ đầu
    # năm — recalc đọc số dư ban đầu cho kỳ đầu năm ngay trong SQL, nhưng chỉ
    # khi có dấu để nó biết phải tính lại.
    mark_dirty(
        session,
        ledger=ledger,
        branch_id=branch_id,
        from_period_id=first_period.id,
        reason=f"opening_balances import {job_id}",
    )
    record_action(
        session,
        entity_type="opening_balances",
        entity_id=str(job_id),
        action=AuditAction.IMPORTED,
        branch_id=branch_id,
        new_values={
            "fiscal_year_id": fiscal_year.id,
            "ledger": ledger,
            "file_name": file_name,
            "content_hash": content_hash,
            "replaced_kinds": ",".join(str(kind) for kind in staged.replaced_kinds),
            "rows": len(staged.rows),
        },
    )


__all__ = [
    "PARTNER_KIND_BY_DETAIL",
    "StagedOpening",
    "ensure_groups_not_settled",
    "first_period_of",
    "validate_rows",
    "write_staged",
]


def ensure_groups_not_settled(
    session: Session,
    *,
    fiscal_year_id: int,
    ledger: int,
    branch_id: int,
    detail_kinds: tuple[int, ...],
) -> None:
    """Chặn thay-trọn/xóa nhóm khi chứng từ con đã bị phiếu đối trừ (M-1 6B).

    Chạy SAU khi đã cầm advisory lock của đường ghi số dư: số `paid_amount_fc`
    chỉ đổi trong transaction ghi/bỏ ghi sổ phiếu, và phiếu giữ `FOR UPDATE`
    trên dòng hóa đơn — một dòng đếm được ở đây là một tham chiếu thật, không
    phải trạng thái nửa vời.
    """
    settled = session.execute(
        select(func.count())
        .select_from(OpeningBalanceInvoice)
        .join(OpeningBalance, OpeningBalance.id == OpeningBalanceInvoice.opening_balance_id)
        .where(
            OpeningBalance.fiscal_year_id == fiscal_year_id,
            OpeningBalance.ledger == ledger,
            OpeningBalance.branch_id == branch_id,
            OpeningBalance.detail_kind.in_(detail_kinds),
            OpeningBalanceInvoice.paid_amount_fc > 0,
        )
    ).scalar_one()
    if settled:
        raise OpeningBalanceSettledError(
            "Nhóm số dư có chứng từ đã được phiếu thu/chi đối trừ — bỏ ghi sổ "
            "các phiếu đó trước khi nhập lại hay xóa nhóm",
            settled_invoices=int(settled),
        )
