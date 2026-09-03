"""Dịch vụ hóa đơn mua hàng (FR-PUR, SRS 05 §3) — lát 7B.

Cùng khuôn `CashVoucherService`: mọi hàm nhận `Session` đang mở, không tự
commit; Cất-đồng-thời-ghi-sổ (FR-SYS-061) là một transaction thật. Bốn việc
riêng của hóa đơn mua:

* **Nghiệp vụ định khoản** (FR-SYS-025): `operation_code` phải thuộc danh sách
  `PUR` của gói hiệu lực; mọi nghiệp vụ mua đều đòi nhà cung cấp.
* **Chi phí mua hàng**: phân bổ vào từng dòng lúc cất (`landed_cost.allocate`),
  kết quả ghi vào `landed_cost_fc` để mapper và giá vốn nhập kho (phase 8) đọc
  cùng một số.
* **Thuế GTGT chỉ khi có hóa đơn** (BR-PUR-02): chứng từ đánh dấu "chưa có /
  không có hóa đơn" mà mang thuế đầu vào là tự tạo một khoản khấu trừ không
  có chứng từ gốc.
* **Sổ phụ công nợ** (ADR-021): ghi sổ → `ArApSubledger.record`, bỏ ghi sổ →
  `remove`, qua `PROVIDERS` chứ không import `receivables` (luật C3). Chứng
  từ trả lại hàng đi đường khác: cộng/gỡ số đã giảm trên hóa đơn gốc.

Bộ đếm tham chiếu danh mục (BR-SYS-02): nhà cung cấp trên hóa đơn và trên
từng khoản chi phí; nguồn đối chiếu ở `usage_counter_accurate.sql`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import DetailTracking
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.config.auto_posting_provider import AutoPostingOperation, operations_for
from ket.kernel.config.catalog import MONEY_SCALE_KEY, SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import (
    PostingValidationError,
    PostingViolation,
    VoucherBranchImmutableError,
)
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.master_data.usage import record_use
from ket.kernel.money import convert_currency
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.protocols import (
    PROVIDERS,
    ArApSubledger,
    SettlementTargetKind,
    SubledgerEntry,
)
from ket.modules.purchase.landed_cost import AllocationLine, allocate, pair_costs_with_lines
from ket.modules.purchase.models import (
    PURCHASE_DOCUMENT_TYPE,
    LandedCost,
    PurchaseInvoice,
    PurchaseInvoiceKind,
    PurchaseInvoiceLine,
    PurchaseSettlement,
    VendorInvoiceStatus,
)
from ket.modules.purchase.posting_mapper import build_posting_request
from ket.modules.purchase.schemas import PurchaseInvoiceIn
from ket.modules.purchase.settlement_service import (
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

NUMBERING_RULE = NumberingRule(
    document_type=PURCHASE_DOCUMENT_TYPE, prefix="PUR{YY}-", reset_rule=ResetRule.YEARLY
)
"""`PUR26-00001`, quay về 1 mỗi năm — cùng khuôn `{YY}` của phiếu thu/chi."""

OPERATION_UNKNOWN_CODE = "purchase.operation_unknown"
OPERATION_PARTNER_REQUIRED_CODE = "purchase.operation_partner_required"
KIND_IMMUTABLE_CODE = "purchase.kind_immutable"
VAT_REQUIRES_INVOICE_CODE = "purchase.vat_requires_invoice"
BODY_MISSING_CODE = "purchase.body_missing"
PAYABLE_ACCOUNT_NOT_VENDOR_TRACKED_CODE = "purchase.payable_account_not_vendor_tracked"
"""TK ghi nợ nhà cung cấp (TK phải trả của hóa đơn, TK Có của khoản chi phí
có `vendor_id`) không theo dõi chi tiết theo nhà cung cấp — sổ phụ công nợ sẽ
có dòng mà sổ cái không đối chiếu được, và không phiếu chi nào đối trừ nổi."""

_ZERO = Decimal(0)


class PurchaseInvoiceService:
    """CRUD + ghi sổ hóa đơn mua, trong transaction người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._vouchers = VoucherService(session)
        self._posting = PostingService(session)

    def create(
        self, payload: PurchaseInvoiceIn, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Cất; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction."""
        scale = self._money_scale(user_id)
        self._verify_operation(payload)
        self._verify_vat_backed_by_invoice(payload)
        self._verify_client_amounts(payload, scale=scale)
        self._verify_vendor_tracked_accounts(payload)
        shares = self._allocate(payload, scale=scale)
        priced = price_settlements(self._session, payload, scale=scale)

        voucher = self._vouchers.create(
            VoucherDraft(
                document_type=PURCHASE_DOCUMENT_TYPE,
                branch_id=payload.branch_id,
                document_date=payload.document_date,
                posting_date=payload.posting_date,
                currency_code=payload.currency_code,
                exchange_rate=payload.exchange_rate,
                description=payload.description,
                cashflow_activity=None,
            ),
            rule=NUMBERING_RULE,
            user_id=user_id,
        )
        body = PurchaseInvoice(id=voucher.id, kind=payload.kind)
        self._fill_body(body, payload, shares)
        self._session.add(body)
        self._write_lines(voucher.id, payload, shares)
        self._write_costs(voucher.id, payload)
        self._write_settlements(voucher.id, priced)
        self._apply_usage(self._usage_of(payload))

        if value_of(self._session, key=SAVE_ALSO_POSTS_KEY, user_id=user_id) is True:
            self.post(voucher.id, user_id=user_id, acknowledged_warnings=acknowledged_warnings)
        return voucher

    def update(
        self,
        voucher_id: UUID,
        payload: PurchaseInvoiceIn,
        *,
        expected_row_version: int,
        user_id: int,
    ) -> Voucher:
        """Sửa hóa đơn Đã cất: thay trọn bộ dòng, chi phí, đối trừ.

        Chi nhánh bất biến (dãy số theo chi nhánh). Loại chứng từ cũng bất
        biến dù cùng dãy `PUR`: đổi hóa đơn mua thành trả lại hàng là đảo
        chiều toàn bộ bút toán và đổi cách ghi sổ phụ — một chứng từ khác.
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
                "Hóa đơn đã cất không đổi được loại — xóa rồi lập chứng từ mới",
                violations=[
                    PostingViolation(
                        KIND_IMMUTABLE_CODE,
                        "Loại hóa đơn quyết định chiều bút toán và cách ghi sổ phụ",
                        current_kind=body.kind,
                        requested_kind=payload.kind,
                    )
                ],
            )

        scale = self._money_scale(user_id)
        self._verify_operation(payload)
        self._verify_vat_backed_by_invoice(payload)
        self._verify_client_amounts(payload, scale=scale)
        self._verify_vendor_tracked_accounts(payload)
        shares = self._allocate(payload, scale=scale)
        priced = price_settlements(self._session, payload, scale=scale)

        usage_before = self._usage_of_stored(body)

        voucher.document_date = payload.document_date
        if payload.posting_date != voucher.posting_date:
            self._vouchers.move_to_date(voucher, posting_date=payload.posting_date)
        voucher.currency_code = payload.currency_code
        voucher.exchange_rate = payload.exchange_rate
        voucher.description = payload.description
        self._fill_body(body, payload, shares)

        for line in self._lines_of(voucher_id):
            self._session.delete(line)
        for cost in self._costs_of(voucher_id):
            self._session.delete(cost)
        for settlement in self._stored_settlements(voucher_id):
            self._session.delete(settlement)
        self._session.flush()
        self._write_lines(voucher_id, payload, shares)
        self._write_costs(voucher_id, payload)
        self._write_settlements(voucher_id, priced)

        usage_after = self._usage_of(payload)
        usage_after.subtract(usage_before)
        self._apply_usage(usage_after)
        return voucher

    def post(
        self, voucher_id: UUID, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Ghi sổ + ghi sổ phụ công nợ (hoặc giảm nợ hóa đơn gốc), một transaction.

        Cùng mã với hook `after_post` trong registry — đường module và đường
        hành động chung không được cho hai kết quả khác nhau.
        """
        voucher = self._posting.post(
            build_posting_request(self._session, voucher_id),
            user_id=user_id,
            acknowledged_warnings=acknowledged_warnings,
        )
        self.sync_after_post(voucher_id, user_id=user_id)
        return voucher

    def unpost(self, voucher_id: UUID, *, user_id: int) -> Voucher:
        voucher = self._posting.unpost(voucher_id, user_id=user_id)
        self.clear_after_unpost(voucher_id)
        return voucher

    def sync_after_post(self, voucher_id: UUID, *, user_id: int) -> None:
        """Việc sau ghi sổ — hook `after_post`: hóa đơn thường ghi khoản phải
        trả vào sổ phụ; trả lại hàng cộng số đã giảm vào hóa đơn gốc."""
        body = self._require_body(voucher_id)
        if body.kind == PurchaseInvoiceKind.RETURN:
            apply_settlements(self._session, voucher_id=voucher_id)
            return
        voucher = self._vouchers.require(voucher_id)
        self._require_subledger().record(
            self._session,
            voucher_id=voucher_id,
            entries=self._subledger_entries(voucher, body, scale=self._money_scale(user_id)),
        )

    def clear_after_unpost(self, voucher_id: UUID) -> None:
        """Việc sau bỏ ghi sổ — hook `after_unpost`: gỡ đúng thứ `sync_after_post` ghi."""
        body = self._require_body(voucher_id)
        if body.kind == PurchaseInvoiceKind.RETURN:
            revert_settlements(self._session, voucher_id=voucher_id)
            return
        self._require_subledger().remove(self._session, voucher_id=voucher_id)

    def delete(self, voucher_id: UUID) -> None:
        """Xóa hóa đơn Đã cất — trả bộ đếm tham chiếu rồi để CASCADE dọn bảng con."""
        body = self._session.get(PurchaseInvoice, voucher_id)
        if body is not None:
            self.release_usage(voucher_id)
        self._vouchers.delete(voucher_id)

    def release_usage(self, voucher_id: UUID) -> None:
        """Trừ bộ đếm tham chiếu của một hóa đơn sắp bị xóa — hook `before_delete`."""
        body = self._require_body(voucher_id)
        counters = self._usage_of_stored(body)
        self._apply_usage(Counter({key: -count for key, count in counters.items()}))

    def get(
        self, voucher_id: UUID
    ) -> tuple[
        Voucher,
        PurchaseInvoice,
        list[PurchaseInvoiceLine],
        list[LandedCost],
        list[PurchaseSettlement],
    ]:
        voucher = self._vouchers.require(voucher_id)
        return (
            voucher,
            self._require_body(voucher_id),
            self._lines_of(voucher_id),
            self._costs_of(voucher_id),
            self._stored_settlements(voucher_id),
        )

    # ------------------------------------------------------------- nội bộ

    def _require_body(self, voucher_id: UUID) -> PurchaseInvoice:
        body = self._session.get(PurchaseInvoice, voucher_id)
        if body is None:
            raise PostingValidationError(
                "Chứng từ này không phải hóa đơn mua hàng",
                violations=[
                    PostingViolation(
                        BODY_MISSING_CODE,
                        "Header tồn tại nhưng không có thân hóa đơn mua",
                        voucher_id=str(voucher_id),
                    )
                ],
            )
        return body

    def _require_subledger(self) -> ArApSubledger:
        subledger = PROVIDERS.ar_ap_subledger()
        if subledger is None:
            # Lỗi lắp ráp, không phải dữ liệu: `model_registry` nạp module chủ
            # sổ phụ trước mọi request. Ghi sổ mà không có sổ phụ là để 331 có
            # số dư mà không hóa đơn nào đối trừ được.
            raise RuntimeError("Chưa có bản cài ArApSubledger — sổ phụ công nợ chưa được đăng ký")
        return subledger

    def _lines_of(self, voucher_id: UUID) -> list[PurchaseInvoiceLine]:
        return list(
            self._session.execute(
                select(PurchaseInvoiceLine)
                .where(PurchaseInvoiceLine.voucher_id == voucher_id)
                .order_by(PurchaseInvoiceLine.line_no)
            )
            .scalars()
            .all()
        )

    def _costs_of(self, voucher_id: UUID) -> list[LandedCost]:
        return list(
            self._session.execute(
                select(LandedCost)
                .where(LandedCost.voucher_id == voucher_id)
                .order_by(LandedCost.line_no)
            )
            .scalars()
            .all()
        )

    def _stored_settlements(self, voucher_id: UUID) -> list[PurchaseSettlement]:
        return list(
            self._session.execute(
                select(PurchaseSettlement).where(PurchaseSettlement.voucher_id == voucher_id)
            )
            .scalars()
            .all()
        )

    def _fill_body(
        self, body: PurchaseInvoice, payload: PurchaseInvoiceIn, shares: Sequence[Decimal]
    ) -> None:
        body.operation_code = payload.operation_code
        body.vendor_id = payload.vendor_id
        body.vendor_invoice_status = payload.vendor_invoice_status
        body.vendor_invoice_form = payload.vendor_invoice_form
        body.vendor_invoice_serial = payload.vendor_invoice_serial
        body.vendor_invoice_no = payload.vendor_invoice_no
        body.vendor_invoice_date = payload.vendor_invoice_date
        body.payment_term_id = payload.payment_term_id
        body.due_date = self._due_date_of(payload)
        body.payable_account_id = payload.payable_account_id
        body.landed_cost_allocation = payload.landed_cost_allocation
        body.total_before_tax_fc = sum((line.amount_fc for line in payload.lines), _ZERO)
        body.total_vat_fc = sum((line.vat_amount_fc for line in payload.lines), _ZERO) + sum(
            (cost.vat_amount_fc for cost in payload.landed_costs), _ZERO
        )
        body.total_landed_cost_fc = sum(shares, _ZERO)
        body.total_fc = body.total_before_tax_fc + body.total_vat_fc + body.total_landed_cost_fc

    def _write_lines(
        self, voucher_id: UUID, payload: PurchaseInvoiceIn, shares: Sequence[Decimal]
    ) -> None:
        for index, (line, share) in enumerate(zip(payload.lines, shares, strict=True), start=1):
            self._session.add(
                PurchaseInvoiceLine(
                    voucher_id=voucher_id,
                    line_no=index,
                    description=line.description,
                    item_id=line.item_id,
                    unit_id=line.unit_id,
                    warehouse_id=line.warehouse_id,
                    quantity=line.quantity,
                    unit_price_fc=line.unit_price_fc,
                    amount_fc=line.amount_fc,
                    vat_rate=line.vat_rate,
                    vat_amount_fc=line.vat_amount_fc,
                    landed_cost_fc=share,
                    account_id=line.account_id,
                    vat_account_id=line.vat_account_id,
                    cost_object_id=line.cost_object_id,
                    project_id=line.project_id,
                    order_id=line.order_id,
                    contract_id=line.contract_id,
                    expense_item_id=line.expense_item_id,
                    extended_dimensions=(
                        {str(value.dimension_id): value.value_id for value in line.extended} or None
                    ),
                )
            )
        self._session.flush()

    def _write_costs(self, voucher_id: UUID, payload: PurchaseInvoiceIn) -> None:
        for index, cost in enumerate(payload.landed_costs, start=1):
            self._session.add(
                LandedCost(
                    voucher_id=voucher_id,
                    line_no=index,
                    description=cost.description,
                    vendor_id=cost.vendor_id,
                    credit_account_id=cost.credit_account_id,
                    amount_fc=cost.amount_fc,
                    vat_rate=cost.vat_rate,
                    vat_amount_fc=cost.vat_amount_fc,
                    vat_account_id=cost.vat_account_id,
                )
            )
        self._session.flush()

    def _write_settlements(self, voucher_id: UUID, priced: list[PricedSettlement]) -> None:
        for row in priced:
            self._session.add(
                PurchaseSettlement(
                    voucher_id=voucher_id,
                    target_kind=row.target_kind.value,
                    target_id=row.target_id,
                    amount_fc=row.amount_fc,
                    amount=row.amount,
                    fx_diff=row.fx_diff,
                )
            )
        self._session.flush()

    def _allocate(self, payload: PurchaseInvoiceIn, *, scale: int) -> tuple[Decimal, ...]:
        """Phần chi phí mua hàng của từng dòng — theo cách phân bổ trên hóa đơn,
        làm tròn theo `money.scale` của dataset như mọi số tiền khác."""
        total = sum((cost.amount_fc for cost in payload.landed_costs), _ZERO)
        return allocate(
            method=payload.landed_cost_allocation,
            total_fc=total,
            scale=scale,
            lines=tuple(
                AllocationLine(
                    amount_fc=line.amount_fc, quantity=line.quantity, manual_fc=line.landed_cost_fc
                )
                for line in payload.lines
            ),
        )

    def _due_date_of(self, payload: PurchaseInvoiceIn) -> date | None:
        """FR-PUR-034: client gửi hạn thì giữ; không thì `document_date +
        due_days` của điều khoản thanh toán trên hóa đơn (nếu có).

        Điều khoản không tra được → để trống hạn; chính khóa ngoại
        `payment_term_id` chặn lượt cất ấy ngay sau đó (`ReferenceNotFoundError`
        ở tầng chung), cùng đường với mọi id danh mục sai khác của chứng từ.
        """
        if payload.due_date is not None or payload.payment_term_id is None:
            return payload.due_date
        term = self._session.get(PaymentTerm, payload.payment_term_id)
        if term is None:
            return None
        return payload.document_date + timedelta(days=term.due_days)

    def _verify_vendor_tracked_accounts(self, payload: PurchaseInvoiceIn) -> None:
        """Mọi TK sẽ sinh dòng sổ phụ công nợ phải theo dõi `vendor`.

        Sổ phụ ghi một dòng cho (NCC, TK) của hóa đơn và của từng khoản chi phí
        có `vendor_id`; check toàn vẹn 131/331 đối chiếu sổ phụ với sổ cái
        **trên các TK theo dõi đối tác**. Cho một khoản chi phí kèm NCC ghi Có
        111 là sinh một khoản "phải trả" mà sổ cái không có, và màn đối trừ
        thì liệt kê nó mãi. Chặn từ lúc cất, cùng chỗ với các phép kiểm khác.
        """
        wanted = {payload.payable_account_id} | {
            cost.credit_account_id for cost in payload.landed_costs if cost.vendor_id is not None
        }
        accounts = accounts_by_id(self._session, list(wanted))
        violations = [
            PostingViolation(
                PAYABLE_ACCOUNT_NOT_VENDOR_TRACKED_CODE,
                "Chọn TK theo dõi theo nhà cung cấp (331…) — hoặc bỏ nhà cung cấp "
                "khỏi khoản chi phí đã chi thẳng",
                account_id=account_id,
                account_code=accounts[account_id].code if account_id in accounts else None,
            )
            for account_id in sorted(wanted)
            if account_id not in accounts
            or DetailTracking.VENDOR not in (accounts[account_id].detail_tracking or ())
        ]
        if violations:
            raise PostingValidationError(
                "Tài khoản công nợ trên hóa đơn không theo dõi theo nhà cung cấp",
                violations=violations,
            )

    def _verify_operation(self, payload: PurchaseInvoiceIn) -> None:
        """FR-SYS-025: nghiệp vụ phải thuộc gói hiệu lực cho loại `PUR`."""
        year = fiscal_year_covering(self._session, payload.posting_date)
        if year is None:
            # Không có năm tài chính thì `VoucherService.create` sẽ từ chối vì
            # không tra được kỳ — thông điệp bên đó đúng chỗ hơn.
            return
        resolved = operations_for(
            self._session,
            document_type=PURCHASE_DOCUMENT_TYPE,
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
                        "Chọn nghiệp vụ trong danh sách của hóa đơn mua",
                        operation_code=payload.operation_code,
                        document_type=PURCHASE_DOCUMENT_TYPE,
                    )
                ],
            )
        self._verify_operation_partner(payload, operation)

    def _verify_operation_partner(
        self, payload: PurchaseInvoiceIn, operation: AutoPostingOperation
    ) -> None:
        # Hóa đơn luôn có nhà cung cấp; thứ còn phải kiểm là gói không khai
        # một nghiệp vụ PUR cho loại đối tác khác — dữ liệu gói lệch thì lộ
        # ra ở đây thay vì ở sổ phụ.
        if (
            operation.requires_partner
            and operation.partner_kind is not None
            and operation.partner_kind != PartnerKind.VENDOR.value
        ):
            raise PostingValidationError(
                "Loại đối tác không khớp với nghiệp vụ",
                violations=[
                    PostingViolation(
                        OPERATION_PARTNER_REQUIRED_CODE,
                        "Nghiệp vụ này làm việc với loại đối tác khác nhà cung cấp",
                        operation_code=payload.operation_code,
                        expected_kind=operation.partner_kind,
                        actual_kind=PartnerKind.VENDOR.value,
                    )
                ],
            )

    def _verify_vat_backed_by_invoice(self, payload: PurchaseInvoiceIn) -> None:
        """BR-PUR-02: thuế GTGT đầu vào chỉ khi đã có hóa đơn của nhà cung cấp."""
        if payload.vendor_invoice_status == VendorInvoiceStatus.RECEIVED:
            return
        has_vat = any(line.vat_amount_fc > _ZERO for line in payload.lines) or any(
            cost.vat_amount_fc > _ZERO for cost in payload.landed_costs
        )
        if has_vat:
            raise PostingValidationError(
                "Chứng từ chưa có hóa đơn của nhà cung cấp không được ghi thuế GTGT đầu vào",
                violations=[
                    PostingViolation(
                        VAT_REQUIRES_INVOICE_CODE,
                        "Đánh dấu 'Đã có hóa đơn' và nhập số hóa đơn, hoặc bỏ thuế GTGT",
                        vendor_invoice_status=payload.vendor_invoice_status,
                    )
                ],
            )

    def _verify_client_amounts(self, payload: PurchaseInvoiceIn, *, scale: int) -> None:
        """Số quy đổi client gửi phải khớp `round_money` — cùng luật với mọi chứng từ."""
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
                "Số quy đổi trên hóa đơn không khớp luật làm tròn của hệ thống",
                violations=violations,
            )

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    # ------------------------------------------------------- sổ phụ công nợ

    def _subledger_entries(
        self, voucher: Voucher, body: PurchaseInvoice, *, scale: int
    ) -> list[SubledgerEntry]:
        """Khoản phải trả của hóa đơn: một dòng cho mỗi cặp (nhà cung cấp, TK
        Có) — nhà cung cấp chính với hàng + thuế của dòng trên TK phải trả của
        hóa đơn, cộng thêm mỗi khoản chi phí mua hàng có `vendor_id`; khoản
        chi phí trùng cặp với hóa đơn thì gộp vào dòng của hóa đơn.

        Số VND cộng từ **từng phần đã quy đổi** — đúng cách engine quy đổi mỗi
        `PostingLine` — để sổ phụ và số dư 331 trên sổ cái khớp từng đồng
        thay vì lệch phần lẻ làm tròn của `round(tổng × tỷ giá)`.

        Chỉ sổ tài chính (`ledger=0`): nguồn đối trừ của `receivables` chỉ
        cộng số đã trả vào sổ ấy, nên dòng sổ quản trị sẽ không bao giờ đóng —
        báo cáo tuổi nợ sổ quản trị vì thế trống thay vì sai.
        """
        rate = voucher.exchange_rate

        def converted(*parts: Decimal) -> Decimal:
            return sum((convert_currency(part, rate, scale) for part in parts), _ZERO)

        lines = self._lines_of(voucher.id)
        costs = self._costs_of(voucher.id)
        header_fc = sum((line.amount_fc + line.vat_amount_fc for line in lines), _ZERO)
        header = sum((converted(line.amount_fc, line.vat_amount_fc) for line in lines), _ZERO)
        # Bên Có của khoản chi phí được chẻ thành các mẩu theo dòng hàng (cùng
        # `pair_costs_with_lines` với mapper), nên số VND của nó cũng cộng từ
        # từng mẩu đã quy đổi — không phải `round(tổng khoản × tỷ giá)`.
        pieces = pair_costs_with_lines(
            [cost.amount_fc for cost in costs], [line.landed_cost_fc for line in lines]
        )
        cost_amounts: dict[int, Decimal] = {}
        for piece in pieces:
            cost_amounts[piece.cost_index] = cost_amounts.get(piece.cost_index, _ZERO) + converted(
                piece.amount_fc
            )

        def entry(
            *, partner_id: int, account_id: int, amount_fc: Decimal, amount: Decimal
        ) -> SubledgerEntry:
            return SubledgerEntry(
                target_kind=SettlementTargetKind.PURCHASE_INVOICE,
                partner_kind=PartnerKind.VENDOR,
                partner_id=partner_id,
                ledger=0,
                account_id=account_id,
                document_no=voucher.voucher_no,
                document_date=voucher.document_date,
                due_date=body.due_date,
                currency_code=voucher.currency_code,
                exchange_rate=rate,
                amount_fc=amount_fc,
                amount=amount,
                description=voucher.description,
            )

        # Gom theo (nhà cung cấp, TK Có): khoản chi phí do CHÍNH nhà cung cấp
        # hóa đơn thu, ghi Có lên đúng TK phải trả của hóa đơn, nhập vào dòng
        # của họ — sổ phụ mỗi chứng từ mỗi đối tác mỗi TK một dòng, không hai.
        grouped: dict[tuple[int, int], tuple[Decimal, Decimal]] = {
            (body.vendor_id, body.payable_account_id): (header_fc, header)
        }
        for index, cost in enumerate(costs):
            if cost.vendor_id is None:
                continue
            key = (cost.vendor_id, cost.credit_account_id)
            previous_fc, previous = grouped.get(key, (_ZERO, _ZERO))
            grouped[key] = (
                previous_fc + cost.amount_fc + cost.vat_amount_fc,
                previous + cost_amounts.get(index, _ZERO) + converted(cost.vat_amount_fc),
            )
        return [
            entry(partner_id=vendor_id, account_id=account_id, amount_fc=amount_fc, amount=amount)
            for (vendor_id, account_id), (amount_fc, amount) in grouped.items()
        ]

    # ----------------------------------------------- bộ đếm tham chiếu danh mục

    def _usage_of(self, payload: PurchaseInvoiceIn) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        counters[(PARTNER_TABLE_NAME, payload.vendor_id)] += 1
        for cost in payload.landed_costs:
            if cost.vendor_id is not None:
                counters[(PARTNER_TABLE_NAME, cost.vendor_id)] += 1
        return counters

    def _usage_of_stored(self, body: PurchaseInvoice) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        counters[(PARTNER_TABLE_NAME, body.vendor_id)] += 1
        for cost in self._costs_of(body.id):
            if cost.vendor_id is not None:
                counters[(PARTNER_TABLE_NAME, cost.vendor_id)] += 1
        return counters

    def _apply_usage(self, counters: Counter[tuple[str, int]]) -> None:
        for (entity_type, entity_id), delta in sorted(counters.items()):
            if delta == 0:
                continue
            record_use(self._session, entity_type=entity_type, entity_id=entity_id, delta=delta)
