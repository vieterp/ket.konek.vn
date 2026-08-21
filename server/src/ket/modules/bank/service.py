"""Dịch vụ chứng từ tiền gửi (FR-BNK-001..007) — SRS 04, lát 6C.

Cùng khuôn `CashVoucherService` (lát 6B): mọi hàm nhận `Session` đang mở,
không tự commit; Cất-đồng-thời-ghi-sổ (FR-SYS-061) là một transaction thật.
Khác phiếu quỹ bốn điểm nghiệp vụ:

* **Bốn loại chứng từ** trên một thân (`docs/srs/04` §2): báo có (BC), ủy
  nhiệm chi (UNC), séc (SEC), chuyển tiền nội bộ (CTNB) — mỗi loại một dãy số
  riêng và một mã quyền riêng.
* **TK ngân hàng cụ thể** (FR-BNK-002): chứng từ gắn danh mục
  `company_bank_accounts`; **tiền tệ chứng từ phải khớp tiền tệ tài khoản**
  (tài khoản không khai tiền tệ = đồng hạch toán) — một chứng từ USD trên tài
  khoản VND là nhầm tài khoản, không phải nghiệp vụ ngoại tệ.
* **Chuyển tiền nội bộ** (FR-BNK-005): không nghiệp vụ, bắt buộc TK đích cùng
  tiền tệ; dòng định khoản 112↔112 do client điền từ purpose `bank`, cặp bút
  toán gắn đủ hai tài khoản ngân hàng trên thân (nguồn + đích) để sổ tiền gửi
  từng tài khoản đọc được cả hai chiều. Chuyển **khác tiền tệ** hoặc **khác
  chi nhánh** (qua 136/336) ngoài phạm vi lát này — từ chối có thông điệp.
* **Bộ đếm tham chiếu** (BR-SYS-02) đếm thêm `company_bank_accounts` (nợ 6A):
  tài khoản ngân hàng đang có chứng từ không xóa được khỏi danh mục.
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.auto_posting_provider import AutoPostingOperation, operations_for
from ket.kernel.config.catalog import MONEY_SCALE_KEY, SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import (
    PostingValidationError,
    PostingViolation,
    VoucherBranchImmutableError,
)
from ket.kernel.master_data.models.company_bank_account import (
    COMPANY_BANK_ACCOUNT_TABLE_NAME,
    CompanyBankAccount,
)
from ket.kernel.master_data.models.employee import EMPLOYEE_TABLE_NAME
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.usage import record_use
from ket.kernel.money import convert_currency
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.persistence.versioning import require_row_version
from ket.modules.bank.models import (
    BankSettlement,
    BankVoucher,
    BankVoucherKind,
    BankVoucherLine,
)
from ket.modules.bank.posting_mapper import build_posting_request
from ket.modules.bank.reconciliation import ensure_not_matched_to_statement
from ket.modules.bank.schemas import BankVoucherIn
from ket.modules.bank.settlement_service import (
    apply_settlements,
    price_settlements,
    revert_settlements,
)
from ket.posting.contracts import (
    PostingService,
    Voucher,
    VoucherDraft,
    VoucherService,
)
from ket.posting.settlements import PricedSettlement

CREDIT_ADVICE_DOCUMENT_TYPE = "BC"
PAYMENT_ORDER_DOCUMENT_TYPE = "UNC"
CHEQUE_DOCUMENT_TYPE = "SEC"
INTERNAL_TRANSFER_DOCUMENT_TYPE = "CTNB"

DOCUMENT_TYPE_BY_KIND = {
    BankVoucherKind.CREDIT_ADVICE: CREDIT_ADVICE_DOCUMENT_TYPE,
    BankVoucherKind.PAYMENT_ORDER: PAYMENT_ORDER_DOCUMENT_TYPE,
    BankVoucherKind.CHEQUE: CHEQUE_DOCUMENT_TYPE,
    BankVoucherKind.INTERNAL_TRANSFER: INTERNAL_TRANSFER_DOCUMENT_TYPE,
}

NUMBERING_RULE_BY_KIND = {
    kind: NumberingRule(
        document_type=document_type, prefix=f"{document_type}{{YY}}-", reset_rule=ResetRule.YEARLY
    )
    for kind, document_type in DOCUMENT_TYPE_BY_KIND.items()
}
"""`BC26-00001`/`UNC26-00001`/… — cùng khuôn `{YY}` của PT/PC (năm nằm trong
chính số nên không đụng `uq_vouchers_type_branch_no`)."""

OPERATION_UNKNOWN_CODE = "bank.operation_unknown"
OPERATION_PARTNER_REQUIRED_CODE = "bank.operation_partner_required"
KIND_IMMUTABLE_CODE = "bank.kind_immutable"
ACCOUNT_UNKNOWN_CODE = "bank.account_unknown"
ACCOUNT_CURRENCY_MISMATCH_CODE = "bank.account_currency_mismatch"
TRANSFER_CURRENCY_MISMATCH_CODE = "bank.transfer_currency_mismatch"

_USAGE_TABLE_BY_PARTNER_KIND = {
    PartnerKind.CUSTOMER: PARTNER_TABLE_NAME,
    PartnerKind.VENDOR: PARTNER_TABLE_NAME,
    PartnerKind.EMPLOYEE: EMPLOYEE_TABLE_NAME,
}


class BankVoucherService:
    """CRUD + ghi sổ chứng từ tiền gửi, trong transaction người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._vouchers = VoucherService(session)
        self._posting = PostingService(session)

    def create(
        self, payload: BankVoucherIn, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Cất; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction."""
        kind = payload.kind
        scale = self._money_scale(user_id)
        self._verify_bank_accounts(payload)
        self._verify_operation(payload)
        self._verify_client_amounts(payload, scale=scale)
        priced = self._price_settlements(payload, scale=scale)

        voucher = self._vouchers.create(
            VoucherDraft(
                document_type=DOCUMENT_TYPE_BY_KIND[kind],
                branch_id=payload.branch_id,
                document_date=payload.document_date,
                posting_date=payload.posting_date,
                currency_code=payload.currency_code,
                exchange_rate=payload.exchange_rate,
                description=payload.description,
                cashflow_activity=payload.cashflow_activity,
            ),
            rule=NUMBERING_RULE_BY_KIND[kind],
            user_id=user_id,
        )
        self._session.add(
            BankVoucher(
                id=voucher.id,
                kind=kind,
                operation_code=payload.operation_code,
                bank_account_id=payload.bank_account_id,
                counter_bank_account_id=payload.counter_bank_account_id,
                partner_id=payload.partner_id,
                partner_kind=(
                    payload.partner_kind.value if payload.partner_kind is not None else None
                ),
                beneficiary_name=payload.beneficiary_name,
                beneficiary_account_no=payload.beneficiary_account_no,
                beneficiary_bank_name=payload.beneficiary_bank_name,
                cheque_no=payload.cheque_no,
                cheque_date=payload.cheque_date,
                reference_no=payload.reference_no,
            )
        )
        self._write_lines(voucher.id, payload)
        self._write_settlements(voucher.id, priced)
        self._apply_usage(self._usage_of(payload))

        if value_of(self._session, key=SAVE_ALSO_POSTS_KEY, user_id=user_id) is True:
            self.post(voucher.id, user_id=user_id, acknowledged_warnings=acknowledged_warnings)
        return voucher

    def update(
        self,
        voucher_id: UUID,
        payload: BankVoucherIn,
        *,
        expected_row_version: int,
        user_id: int,
    ) -> Voucher:
        """Sửa chứng từ Đã cất: thay trọn bộ dòng + đối trừ (cùng khuôn PT/PC).

        Chi nhánh và LOẠI chứng từ bất biến: số chứng từ thuộc dãy của
        (loại, chi nhánh) — đổi BC thành UNC là đổi dãy số, phải xóa lập lại.
        """
        voucher = self._vouchers.require(voucher_id)
        self._vouchers.ensure_editable(voucher)
        require_row_version(
            current=voucher.row_version,
            expected=expected_row_version,
            entity=Voucher.__tablename__,
        )
        body = self._require_body(voucher_id)
        if payload.branch_id != voucher.branch_id:
            raise VoucherBranchImmutableError(
                "Chứng từ đã cất không đổi được chi nhánh — xóa rồi lập lại ở chi nhánh đúng",
                voucher_no=voucher.voucher_no,
                current_branch=voucher.branch_id,
                requested_branch=payload.branch_id,
            )
        if payload.kind != body.kind:
            raise PostingValidationError(
                "Chứng từ đã cất không đổi được loại — xóa rồi lập chứng từ mới",
                violations=[
                    PostingViolation(
                        KIND_IMMUTABLE_CODE,
                        "Số chứng từ thuộc dãy của loại chứng từ — đổi loại là đổi dãy số",
                        current_kind=body.kind,
                        requested_kind=payload.kind,
                    )
                ],
            )

        scale = self._money_scale(user_id)
        self._verify_bank_accounts(payload)
        self._verify_operation(payload)
        self._verify_client_amounts(payload, scale=scale)
        priced = self._price_settlements(payload, scale=scale)

        usage_before = self._usage_of_stored(body)

        voucher.document_date = payload.document_date
        if payload.posting_date != voucher.posting_date:
            self._vouchers.move_to_date(voucher, posting_date=payload.posting_date)
        voucher.currency_code = payload.currency_code
        voucher.exchange_rate = payload.exchange_rate
        voucher.description = payload.description
        voucher.cashflow_activity = payload.cashflow_activity

        body.operation_code = payload.operation_code
        body.bank_account_id = payload.bank_account_id
        body.counter_bank_account_id = payload.counter_bank_account_id
        body.partner_id = payload.partner_id
        body.partner_kind = payload.partner_kind.value if payload.partner_kind is not None else None
        body.beneficiary_name = payload.beneficiary_name
        body.beneficiary_account_no = payload.beneficiary_account_no
        body.beneficiary_bank_name = payload.beneficiary_bank_name
        body.cheque_no = payload.cheque_no
        body.cheque_date = payload.cheque_date
        body.reference_no = payload.reference_no

        for line in self._lines_of(voucher_id):
            self._session.delete(line)
        for settlement in self._stored_settlements(voucher_id):
            self._session.delete(settlement)
        self._session.flush()
        self._write_lines(voucher_id, payload)
        self._write_settlements(voucher_id, priced)

        usage_after = self._usage_of(payload)
        usage_after.subtract(usage_before)
        self._apply_usage(usage_after)
        return voucher

    def post(
        self, voucher_id: UUID, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Ghi sổ + cộng số đã trả vào các đích đối trừ, một transaction —
        cùng mã với hook `after_post` trong registry."""
        voucher = self._posting.post(
            build_posting_request(self._session, voucher_id),
            user_id=user_id,
            acknowledged_warnings=acknowledged_warnings,
        )
        apply_settlements(self._session, voucher_id=voucher_id)
        return voucher

    def unpost(self, voucher_id: UUID, *, user_id: int) -> Voucher:
        # H-3 review 6D: cửa dịch vụ phải canh cùng luật với hook after_unpost
        # của endpoint hành động chung — chứng từ đã khớp sao kê thì gỡ khớp
        # trước, không bỏ ghi sổ để lại một dòng "đã khớp" trỏ phiếu nháp.
        ensure_not_matched_to_statement(self._session, voucher_id=voucher_id)
        voucher = self._posting.unpost(voucher_id, user_id=user_id)
        revert_settlements(self._session, voucher_id=voucher_id)
        return voucher

    def delete(self, voucher_id: UUID) -> None:
        """Xóa chứng từ Đã cất — trả bộ đếm tham chiếu rồi để CASCADE dọn bảng con."""
        body = self._session.get(BankVoucher, voucher_id)
        if body is not None:
            self.release_usage(voucher_id)
        self._vouchers.delete(voucher_id)

    def release_usage(self, voucher_id: UUID) -> None:
        """Trừ bộ đếm tham chiếu của một chứng từ sắp bị xóa — hook `before_delete`."""
        body = self._require_body(voucher_id)
        counters = self._usage_of_stored(body)
        self._apply_usage(Counter({key: -count for key, count in counters.items()}))

    def get(
        self, voucher_id: UUID
    ) -> tuple[Voucher, BankVoucher, list[BankVoucherLine], list[BankSettlement]]:
        voucher = self._vouchers.require(voucher_id)
        return (
            voucher,
            self._require_body(voucher_id),
            self._lines_of(voucher_id),
            list(self._stored_settlements(voucher_id)),
        )

    # ------------------------------------------------------------- nội bộ

    def _require_body(self, voucher_id: UUID) -> BankVoucher:
        body = self._session.get(BankVoucher, voucher_id)
        if body is None:
            raise PostingValidationError(
                "Chứng từ này không phải chứng từ tiền gửi",
                violations=[
                    PostingViolation(
                        "bank.body_missing",
                        "Header tồn tại nhưng không có thân chứng từ tiền gửi",
                        voucher_id=str(voucher_id),
                    )
                ],
            )
        return body

    def _lines_of(self, voucher_id: UUID) -> list[BankVoucherLine]:
        return list(
            self._session.execute(
                select(BankVoucherLine)
                .where(BankVoucherLine.voucher_id == voucher_id)
                .order_by(BankVoucherLine.line_no)
            )
            .scalars()
            .all()
        )

    def _stored_settlements(self, voucher_id: UUID) -> list[BankSettlement]:
        return list(
            self._session.execute(
                select(BankSettlement).where(BankSettlement.voucher_id == voucher_id)
            )
            .scalars()
            .all()
        )

    def _write_lines(self, voucher_id: UUID, payload: BankVoucherIn) -> None:
        for index, line in enumerate(payload.lines, start=1):
            self._session.add(
                BankVoucherLine(
                    voucher_id=voucher_id,
                    line_no=index,
                    description=line.description,
                    debit_account_id=line.debit_account_id,
                    credit_account_id=line.credit_account_id,
                    amount_fc=line.amount_fc,
                    partner_id=line.partner_id,
                    partner_kind=(
                        line.partner_kind.value if line.partner_kind is not None else None
                    ),
                    cost_object_id=line.cost_object_id,
                    project_id=line.project_id,
                    order_id=line.order_id,
                    contract_id=line.contract_id,
                    expense_item_id=line.expense_item_id,
                    item_id=line.item_id,
                    warehouse_id=line.warehouse_id,
                    extended_dimensions=(
                        {str(value.dimension_id): value.value_id for value in line.extended} or None
                    ),
                )
            )
        self._session.flush()

    def _write_settlements(self, voucher_id: UUID, priced: list[PricedSettlement]) -> None:
        for row in priced:
            self._session.add(
                BankSettlement(
                    voucher_id=voucher_id,
                    target_kind=row.target_kind.value,
                    target_id=row.target_id,
                    amount_fc=row.amount_fc,
                    amount=row.amount,
                    fx_diff=row.fx_diff,
                )
            )
        self._session.flush()

    def _price_settlements(self, payload: BankVoucherIn, *, scale: int) -> list[PricedSettlement]:
        return price_settlements(self._session, payload, scale=scale)

    def _verify_bank_accounts(self, payload: BankVoucherIn) -> None:
        """FR-BNK-002 + tiền tệ: chứng từ phải khớp tiền tệ của tài khoản; chuyển
        nội bộ đòi hai tài khoản cùng tiền tệ (chéo tiền tệ ngoài phạm vi v1)."""
        violations: list[PostingViolation] = []
        base = self._base_currency(payload)
        source = self._require_bank_account(payload.bank_account_id, violations)
        if source is not None:
            source_currency = source.currency_code or base
            if source_currency != payload.currency_code:
                violations.append(
                    PostingViolation(
                        ACCOUNT_CURRENCY_MISMATCH_CODE,
                        "Loại tiền của chứng từ khác loại tiền của tài khoản ngân hàng",
                        bank_account_id=payload.bank_account_id,
                        account_currency=source_currency,
                        voucher_currency=payload.currency_code,
                    )
                )
        if payload.counter_bank_account_id is not None:
            counter = self._require_bank_account(payload.counter_bank_account_id, violations)
            if source is not None and counter is not None:
                source_currency = source.currency_code or base
                counter_currency = counter.currency_code or base
                if source_currency != counter_currency:
                    violations.append(
                        PostingViolation(
                            TRANSFER_CURRENCY_MISMATCH_CODE,
                            "Chuyển tiền nội bộ giữa hai tài khoản khác loại tiền chưa "
                            "hỗ trợ — hạch toán qua hai chứng từ mua/bán ngoại tệ",
                            source_currency=source_currency,
                            counter_currency=counter_currency,
                        )
                    )
        if violations:
            raise PostingValidationError(
                "Tài khoản ngân hàng trên chứng từ chưa hợp lệ", violations=violations
            )

    def _require_bank_account(
        self, account_id: int, violations: list[PostingViolation]
    ) -> CompanyBankAccount | None:
        account = self._session.get(CompanyBankAccount, account_id)
        if account is None:
            violations.append(
                PostingViolation(
                    ACCOUNT_UNKNOWN_CODE,
                    "Tài khoản ngân hàng không có trong danh mục",
                    bank_account_id=account_id,
                )
            )
        return account

    def _base_currency(self, payload: BankVoucherIn) -> str:
        year = fiscal_year_covering(self._session, payload.posting_date)
        # Không có năm tài chính thì `VoucherService.create` sẽ từ chối vì không
        # tra được kỳ — trước đó tài khoản không khai tiền tệ coi như VND.
        return year.base_currency if year is not None else "VND"

    def _verify_operation(self, payload: BankVoucherIn) -> None:
        """FR-SYS-025 cho BC/UNC/SEC; chuyển nội bộ không có nghiệp vụ (schema
        đã chặn `operation_code` cho loại này)."""
        if payload.kind == BankVoucherKind.INTERNAL_TRANSFER or payload.operation_code is None:
            return
        year = fiscal_year_covering(self._session, payload.posting_date)
        if year is None:
            return
        resolved = operations_for(
            self._session,
            document_type=DOCUMENT_TYPE_BY_KIND[payload.kind],
            scheme=year.accounting_scheme,
            on_date=payload.posting_date,
        )
        operation = next(
            (item for item in resolved.items if item.operation_code == payload.operation_code),
            None,
        )
        if operation is None:
            raise PostingValidationError(
                "Nghiệp vụ không có trong gói cấu hình hiệu lực",
                violations=[
                    PostingViolation(
                        OPERATION_UNKNOWN_CODE,
                        "Chọn nghiệp vụ trong danh sách của loại chứng từ này",
                        operation_code=payload.operation_code,
                        document_type=DOCUMENT_TYPE_BY_KIND[payload.kind],
                    )
                ],
            )
        self._verify_operation_partner(payload, operation)

    def _verify_operation_partner(
        self, payload: BankVoucherIn, operation: AutoPostingOperation
    ) -> None:
        if not operation.requires_partner:
            return
        if payload.partner_id is None:
            raise PostingValidationError(
                "Nghiệp vụ này yêu cầu chọn đối tác",
                violations=[
                    PostingViolation(
                        OPERATION_PARTNER_REQUIRED_CODE,
                        "Chọn đối tác cho chứng từ trước khi cất",
                        operation_code=payload.operation_code,
                    )
                ],
            )
        if (
            operation.partner_kind is not None
            and payload.partner_kind is not None
            and payload.partner_kind.value != operation.partner_kind
        ):
            raise PostingValidationError(
                "Loại đối tác không khớp với nghiệp vụ",
                violations=[
                    PostingViolation(
                        OPERATION_PARTNER_REQUIRED_CODE,
                        "Nghiệp vụ này làm việc với loại đối tác khác",
                        operation_code=payload.operation_code,
                        expected_kind=operation.partner_kind,
                        actual_kind=payload.partner_kind.value,
                    )
                ],
            )

    def _verify_client_amounts(self, payload: BankVoucherIn, *, scale: int) -> None:
        """Validator #9 (phase-04): số quy đổi client gửi phải khớp `round_money`."""
        violations: list[PostingViolation] = []
        for index, line in enumerate(payload.lines, start=1):
            if line.amount is None:
                continue
            expected = convert_currency(line.amount_fc, payload.exchange_rate, scale)
            if line.amount != expected:
                violations.append(
                    PostingViolation(
                        "posting.conversion_mismatch",
                        "Số quy đổi client gửi lệch với phép tính của server",
                        line_no=index,
                        sent=str(line.amount),
                        expected=str(expected),
                    )
                )
        if violations:
            raise PostingValidationError(
                "Số quy đổi trên chứng từ không khớp luật làm tròn của hệ thống",
                violations=violations,
            )

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    # ----------------------------------------------- bộ đếm tham chiếu danh mục

    def _usage_of(self, payload: BankVoucherIn) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        counters[(COMPANY_BANK_ACCOUNT_TABLE_NAME, payload.bank_account_id)] += 1
        if payload.counter_bank_account_id is not None:
            counters[(COMPANY_BANK_ACCOUNT_TABLE_NAME, payload.counter_bank_account_id)] += 1
        if payload.partner_id is not None and payload.partner_kind is not None:
            counters[(_USAGE_TABLE_BY_PARTNER_KIND[payload.partner_kind], payload.partner_id)] += 1
        for line in payload.lines:
            if line.partner_id is not None and line.partner_kind is not None:
                counters[(_USAGE_TABLE_BY_PARTNER_KIND[line.partner_kind], line.partner_id)] += 1
        return counters

    def _usage_of_stored(self, body: BankVoucher) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        counters[(COMPANY_BANK_ACCOUNT_TABLE_NAME, body.bank_account_id)] += 1
        if body.counter_bank_account_id is not None:
            counters[(COMPANY_BANK_ACCOUNT_TABLE_NAME, body.counter_bank_account_id)] += 1
        if body.partner_id is not None and body.partner_kind is not None:
            table = _USAGE_TABLE_BY_PARTNER_KIND[PartnerKind(body.partner_kind)]
            counters[(table, body.partner_id)] += 1
        for line in self._lines_of(body.id):
            if line.partner_id is not None and line.partner_kind is not None:
                table = _USAGE_TABLE_BY_PARTNER_KIND[PartnerKind(line.partner_kind)]
                counters[(table, line.partner_id)] += 1
        return counters

    def _apply_usage(self, counters: Counter[tuple[str, int]]) -> None:
        for (entity_type, entity_id), delta in sorted(counters.items()):
            if delta == 0:
                continue
            record_use(self._session, entity_type=entity_type, entity_id=entity_id, delta=delta)
