"""Dịch vụ phiếu thu/chi tiền mặt (FR-QUY-001..013) — module nghiệp vụ đầu tiên.

Cùng khuôn `JournalVoucherService` (phase-04): mọi hàm nhận `Session` đang mở,
không tự commit; Cất-đồng-thời-ghi-sổ (FR-SYS-061) là một transaction thật.
Khác GLE ba điểm nghiệp vụ:

* **Nghiệp vụ định khoản** (FR-SYS-025): `operation_code` phải thuộc danh sách
  của gói hiệu lực cho loại chứng từ; nghiệp vụ đòi đối tác thì phiếu phải có
  đối tác đúng loại.
* **Đối trừ công nợ** (`docs/srs/03` §4): định giá lúc cất
  (`settlement_service.price_settlements`), cộng/gỡ số đã trả lúc ghi/bỏ ghi
  sổ — hai hook sau cũng được đăng ký vào `POSTING_DOCUMENT_REGISTRY` để đường
  hành động chung đi qua cùng một mã.
* **Bộ đếm tham chiếu danh mục** (BR-SYS-02, nợ 6A): đối tác trên phiếu và
  trên dòng nhích `master_data_usage` trong cùng transaction; nguồn đếm đối
  chiếu khai ở `posting/integrity/checks/usage_counter_accurate.sql`.
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
from ket.kernel.master_data.models.employee import EMPLOYEE_TABLE_NAME
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.usage import record_use
from ket.kernel.money import convert_currency
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.persistence.versioning import require_row_version
from ket.modules.cash_book.models import (
    CashSettlement,
    CashVoucher,
    CashVoucherKind,
    CashVoucherLine,
)
from ket.modules.cash_book.posting_mapper import build_posting_request
from ket.modules.cash_book.schemas import CashVoucherIn
from ket.modules.cash_book.settlement_service import (
    PricedSettlement,
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

RECEIPT_DOCUMENT_TYPE = "PT"
PAYMENT_DOCUMENT_TYPE = "PC"

DOCUMENT_TYPE_BY_KIND = {
    CashVoucherKind.RECEIPT: RECEIPT_DOCUMENT_TYPE,
    CashVoucherKind.PAYMENT: PAYMENT_DOCUMENT_TYPE,
}

NUMBERING_RULE_BY_KIND = {
    CashVoucherKind.RECEIPT: NumberingRule(
        document_type=RECEIPT_DOCUMENT_TYPE, prefix="PT{YY}-", reset_rule=ResetRule.YEARLY
    ),
    CashVoucherKind.PAYMENT: NumberingRule(
        document_type=PAYMENT_DOCUMENT_TYPE, prefix="PC{YY}-", reset_rule=ResetRule.YEARLY
    ),
}
"""`PT26-00001`/`PC26-00001`, quay về 1 mỗi năm — cùng khuôn `{YY}` của GLE
(năm nằm trong chính số nên không đụng `uq_vouchers_type_branch_no`)."""

OPERATION_UNKNOWN_CODE = "cash.operation_unknown"
OPERATION_PARTNER_REQUIRED_CODE = "cash.operation_partner_required"
KIND_IMMUTABLE_CODE = "cash.kind_immutable"

_USAGE_TABLE_BY_PARTNER_KIND = {
    PartnerKind.CUSTOMER: PARTNER_TABLE_NAME,
    PartnerKind.VENDOR: PARTNER_TABLE_NAME,
    PartnerKind.EMPLOYEE: EMPLOYEE_TABLE_NAME,
}


class CashVoucherService:
    """CRUD + ghi sổ phiếu thu/chi, trong transaction người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._vouchers = VoucherService(session)
        self._posting = PostingService(session)

    def create(
        self, payload: CashVoucherIn, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Cất; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction —
        `acknowledged_warnings` đi tiếp vào lượt ghi sổ đó (nợ 6A)."""
        kind = payload.kind
        scale = self._money_scale(user_id)
        self._verify_operation(payload)
        self._verify_client_amounts(payload, scale=scale)
        priced = price_settlements(self._session, payload, scale=scale)

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
            CashVoucher(
                id=voucher.id,
                kind=kind,
                operation_code=payload.operation_code,
                cash_account_id=payload.cash_account_id,
                partner_id=payload.partner_id,
                partner_kind=(
                    payload.partner_kind.value if payload.partner_kind is not None else None
                ),
                payer_receiver_name=payload.payer_receiver_name,
                attachment_count=payload.attachment_count,
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
        payload: CashVoucherIn,
        *,
        expected_row_version: int,
        user_id: int,
    ) -> Voucher:
        """Sửa phiếu Đã cất: thay trọn bộ dòng + đối trừ (cùng khuôn GLE).

        Chi nhánh và LOẠI phiếu bất biến: số chứng từ thuộc dãy của
        (loại, chi nhánh) — đổi PT thành PC là đổi dãy số, phải xóa lập lại.
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
                "Phiếu đã cất không đổi được loại thu/chi — xóa rồi lập phiếu mới",
                violations=[
                    PostingViolation(
                        KIND_IMMUTABLE_CODE,
                        "Số chứng từ thuộc dãy của loại phiếu — đổi loại là đổi dãy số",
                        current_kind=body.kind,
                        requested_kind=payload.kind,
                    )
                ],
            )

        scale = self._money_scale(user_id)
        self._verify_operation(payload)
        self._verify_client_amounts(payload, scale=scale)
        priced = price_settlements(self._session, payload, scale=scale)

        usage_before = self._usage_of_stored(body)

        voucher.document_date = payload.document_date
        if payload.posting_date != voucher.posting_date:
            self._vouchers.move_to_date(voucher, posting_date=payload.posting_date)
        voucher.currency_code = payload.currency_code
        voucher.exchange_rate = payload.exchange_rate
        voucher.description = payload.description
        voucher.cashflow_activity = payload.cashflow_activity

        body.operation_code = payload.operation_code
        body.cash_account_id = payload.cash_account_id
        body.partner_id = payload.partner_id
        body.partner_kind = payload.partner_kind.value if payload.partner_kind is not None else None
        body.payer_receiver_name = payload.payer_receiver_name
        body.attachment_count = payload.attachment_count

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
        """Ghi sổ + cộng số đã trả vào các đích đối trừ, một transaction.

        Cùng mã với hook `after_post` trong registry — đường module và đường
        hành động chung không được cho hai kết quả khác nhau.
        """
        voucher = self._posting.post(
            build_posting_request(self._session, voucher_id),
            user_id=user_id,
            acknowledged_warnings=acknowledged_warnings,
        )
        apply_settlements(self._session, voucher_id=voucher_id)
        return voucher

    def unpost(self, voucher_id: UUID, *, user_id: int) -> Voucher:
        voucher = self._posting.unpost(voucher_id, user_id=user_id)
        revert_settlements(self._session, voucher_id=voucher_id)
        return voucher

    def delete(self, voucher_id: UUID) -> None:
        """Xóa phiếu Đã cất — trả bộ đếm tham chiếu rồi để CASCADE dọn bảng con.

        Trả bộ đếm TRƯỚC vì sau `DELETE` không còn dòng nào để đếm; nếu phép
        xóa bị máy trạng thái từ chối thì cả hai cùng rollback.
        """
        body = self._session.get(CashVoucher, voucher_id)
        if body is not None:
            self.release_usage(voucher_id)
        self._vouchers.delete(voucher_id)

    def release_usage(self, voucher_id: UUID) -> None:
        """Trừ bộ đếm tham chiếu của một phiếu sắp bị xóa — hook `before_delete`."""
        body = self._require_body(voucher_id)
        counters = self._usage_of_stored(body)
        self._apply_usage(Counter({key: -count for key, count in counters.items()}))

    def get(
        self, voucher_id: UUID
    ) -> tuple[Voucher, CashVoucher, list[CashVoucherLine], list[CashSettlement]]:
        voucher = self._vouchers.require(voucher_id)
        return (
            voucher,
            self._require_body(voucher_id),
            self._lines_of(voucher_id),
            list(self._stored_settlements(voucher_id)),
        )

    # ------------------------------------------------------------- nội bộ

    def _require_body(self, voucher_id: UUID) -> CashVoucher:
        body = self._session.get(CashVoucher, voucher_id)
        if body is None:
            raise PostingValidationError(
                "Chứng từ này không phải phiếu thu/chi tiền mặt",
                violations=[
                    PostingViolation(
                        "cash.body_missing",
                        "Header tồn tại nhưng không có thân phiếu thu/chi",
                        voucher_id=str(voucher_id),
                    )
                ],
            )
        return body

    def _lines_of(self, voucher_id: UUID) -> list[CashVoucherLine]:
        return list(
            self._session.execute(
                select(CashVoucherLine)
                .where(CashVoucherLine.voucher_id == voucher_id)
                .order_by(CashVoucherLine.line_no)
            )
            .scalars()
            .all()
        )

    def _stored_settlements(self, voucher_id: UUID) -> list[CashSettlement]:
        return list(
            self._session.execute(
                select(CashSettlement).where(CashSettlement.voucher_id == voucher_id)
            )
            .scalars()
            .all()
        )

    def _write_lines(self, voucher_id: UUID, payload: CashVoucherIn) -> None:
        for index, line in enumerate(payload.lines, start=1):
            self._session.add(
                CashVoucherLine(
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
                CashSettlement(
                    voucher_id=voucher_id,
                    target_kind=row.target_kind.value,
                    target_id=row.target_id,
                    amount_fc=row.amount_fc,
                    amount=row.amount,
                    fx_diff=row.fx_diff,
                )
            )
        self._session.flush()

    def _verify_operation(self, payload: CashVoucherIn) -> None:
        """FR-SYS-025: nghiệp vụ phải thuộc gói hiệu lực; nghiệp vụ đòi đối tác
        thì phiếu phải mang đối tác đúng loại."""
        year = fiscal_year_covering(self._session, payload.posting_date)
        if year is None:
            # Không có năm tài chính thì `VoucherService.create` sẽ từ chối vì
            # không tra được kỳ — thông điệp bên đó đúng chỗ hơn.
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
        self, payload: CashVoucherIn, operation: AutoPostingOperation
    ) -> None:
        if not operation.requires_partner:
            return
        if payload.partner_id is None:
            raise PostingValidationError(
                "Nghiệp vụ này yêu cầu chọn đối tác",
                violations=[
                    PostingViolation(
                        OPERATION_PARTNER_REQUIRED_CODE,
                        "Chọn đối tác cho phiếu trước khi cất",
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

    def _verify_client_amounts(self, payload: CashVoucherIn, *, scale: int) -> None:
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
                "Số quy đổi trên phiếu không khớp luật làm tròn của hệ thống",
                violations=violations,
            )

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    # ----------------------------------------------- bộ đếm tham chiếu danh mục

    def _usage_of(self, payload: CashVoucherIn) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        if payload.partner_id is not None and payload.partner_kind is not None:
            counters[(_USAGE_TABLE_BY_PARTNER_KIND[payload.partner_kind], payload.partner_id)] += 1
        for line in payload.lines:
            if line.partner_id is not None and line.partner_kind is not None:
                counters[(_USAGE_TABLE_BY_PARTNER_KIND[line.partner_kind], line.partner_id)] += 1
        return counters

    def _usage_of_stored(self, body: CashVoucher) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
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
