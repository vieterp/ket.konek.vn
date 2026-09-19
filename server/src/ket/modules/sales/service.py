"""Dịch vụ hóa đơn bán hàng (FR-SAL, SRS 06 §3) — lát 7C-2.

Cùng khuôn `PurchaseInvoiceService`: mọi hàm nhận `Session` đang mở, không tự
commit; Cất-đồng-thời-ghi-sổ (FR-SYS-061) là một transaction thật. Bốn việc
riêng của hóa đơn bán:

* **Nghiệp vụ định khoản** (FR-SYS-025): `operation_code` phải thuộc danh sách
  `SAL` của gói hiệu lực; mọi nghiệp vụ bán đều đòi khách hàng.
* **Hạn thanh toán theo điều khoản của khách** (FR-SAL-009): client gửi hạn
  thì giữ; không thì `document_date + due_days` của điều khoản trên chứng từ,
  và điều khoản trên chứng từ trống thì rơi về điều khoản khai trên **danh mục
  khách hàng** — vế "tự lấy theo khách hàng" mà SRS đòi và hóa đơn mua không có.
* **Giá vốn để trống** (BR-SAL-01 do phase 8 đóng): ba cột giá vốn của dòng chỉ
  được lưu, `cogs_posted` ở lại `false`, và mapper không sinh cặp 632/156.
* **Sổ phụ công nợ** (ADR-021): ghi sổ → `ArApSubledger.record`, bỏ ghi sổ →
  `remove`, qua `PROVIDERS` chứ không import `receivables` (luật C3). Chứng từ
  trả lại / giảm giá đi đường khác: cộng/gỡ số đã giảm trên hóa đơn gốc.

**Đơn giá và chiết khấu do client chốt** (quyết định user 2026-09-04): service
không gọi lại `kernel.pricing.quote_price`. Lý do ở đầu `schemas.py` — gõ tay
đơn giá là đường hợp lệ, và bộ định giá trả `0` cho mã hàng chưa khai giá.

Bộ đếm tham chiếu danh mục (BR-SYS-02): khách hàng và nhân viên bán hàng trên
hóa đơn, cộng **mã quy cách** của từng dòng (7G-2a); nguồn đối chiếu ở
`usage_counter_accurate.sql`.

Quy cách được đếm dù `item_id`/`unit_id` trên cùng dòng thì không, và đó là
một lựa chọn có lý do: `item_variants` là danh mục duy nhất mà một **báo cáo**
đọc qua `LEFT JOIN` theo id (sổ chi tiết bán hàng theo mã quy cách, 7G-2a).
Xóa hoặc gộp mất dòng danh mục ấy không làm sổ cái sai một đồng, nhưng nó đẩy
doanh thu sang nhóm "Không khai quy cách" **im lặng** — và đường gộp hai mã
hàng trùng nhau XÓA THẬT quy cách của bản nguồn khi bản đích đã có cùng mã,
tức ca thường gặp chứ không phải ca hiếm. Bộ đếm là phép canh duy nhất mở cho
kernel: `item_variant_service` không được phép đọc bảng của module (luật C1).
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from ket.kernel.config.accounts_models import DetailTracking
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.config.auto_posting_provider import AutoPostingOperation, operations_for
from ket.kernel.config.catalog import MONEY_SCALE_KEY, SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import (
    PostingValidationError,
    PostingViolation,
    SalesLineReturnedError,
    VoucherBranchImmutableError,
)
from ket.kernel.master_data.models.employee import EMPLOYEE_TABLE_NAME
from ket.kernel.master_data.models.item_variant import ITEM_VARIANT_TABLE_NAME, ItemVariant
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME, Partner
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
from ket.modules.sales.inventory_lines import planned_movement
from ket.modules.sales.models import (
    REVERSING_KINDS,
    SALES_DOCUMENT_TYPE,
    SalesInvoice,
    SalesInvoiceKind,
    SalesInvoiceLine,
    SalesSettlement,
)
from ket.modules.sales.posting_mapper import build_posting_request
from ket.modules.sales.schemas import SalesInvoiceIn
from ket.modules.sales.settlement_service import (
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
    VoucherStatus,
)

_RETURNABLE_KINDS: Final[frozenset[int]] = frozenset(
    {SalesInvoiceKind.GOODS, SalesInvoiceKind.AGENCY}
)
"""Chứng từ bán mà một dòng hàng-bán-trả-lại được chỉ về (FR-STK-004): hai
loại kiêm xuất kho được — cùng tập `_ISSUE_KINDS` của `inventory_lines`."""

NUMBERING_RULE = NumberingRule(
    document_type=SALES_DOCUMENT_TYPE, prefix="SAL{YY}-", reset_rule=ResetRule.YEARLY
)
"""`SAL26-00001`, quay về 1 mỗi năm — cùng khuôn `{YY}` của phiếu thu/chi và hóa đơn mua."""

OPERATION_UNKNOWN_CODE = "sales.operation_unknown"
OPERATION_PARTNER_REQUIRED_CODE = "sales.operation_partner_required"
KIND_IMMUTABLE_CODE = "sales.kind_immutable"
BODY_MISSING_CODE = "sales.body_missing"
RECEIVABLE_ACCOUNT_NOT_CUSTOMER_TRACKED_CODE = "sales.receivable_account_not_customer_tracked"
"""TK ghi nợ khách hàng không theo dõi chi tiết theo khách hàng — sổ phụ công
nợ sẽ có dòng mà sổ cái không đối chiếu được, và không phiếu thu nào đối trừ
nổi. Cùng phép kiểm với `purchase`, đổi chiều theo dõi."""

VARIANT_NOT_OF_ITEM_CODE = "sales.variant_not_of_item"
RETURNED_LINE_KIND_CODE = "sales.returned_line_kind"
RETURNED_LINE_MISMATCH_CODE = "sales.returned_line_mismatch"
"""Quy cách khai trên dòng không thuộc mã hàng của dòng.

Mã quy cách chỉ duy nhất TRONG một mã hàng (`uq_item_variants_item_code`), nên
`variant_id` của hàng khác là một giá trị **tồn tại** mà vẫn sai — sổ chi tiết
bán hàng theo mã quy cách sẽ xếp doanh thu của áo vào nhóm quy cách của mũ, và
không con số nào trên tờ báo cáo ấy chỉ ra chỗ lệch."""

_ZERO = Decimal(0)


def refuse_when_lines_returned(session: Session, voucher_id: UUID) -> None:
    """Hóa đơn có dòng đang bị chứng từ hàng bán trả lại trỏ tới (`returned_line_id`)
    không sửa/xóa được — đăng ký `EDIT_GUARDS` (8C-1). FK `RESTRICT` là hàng rào
    cuối; guard nói được phải sửa chứng từ nào (review 8C-1 M-5)."""
    original = aliased(SalesInvoiceLine)
    referencing = (
        session.execute(
            select(Voucher.voucher_no)
            .join(SalesInvoiceLine, SalesInvoiceLine.voucher_id == Voucher.id)
            .join(original, original.id == SalesInvoiceLine.returned_line_id)
            .where(original.voucher_id == voucher_id)
            .distinct()
            .order_by(Voucher.voucher_no)
        )
        .scalars()
        .all()
    )
    if referencing:
        raise SalesLineReturnedError(
            "Hóa đơn có dòng đã được chứng từ hàng bán trả lại chỉ tới — sửa hoặc xóa các "
            "chứng từ trả lại đó trước",
            referenced_by=",".join(referencing[:10]),
            count=len(referencing),
        )


class SalesInvoiceService:
    """CRUD + ghi sổ hóa đơn bán, trong transaction người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._vouchers = VoucherService(session)
        self._posting = PostingService(session)

    def create(
        self, payload: SalesInvoiceIn, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Cất; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction."""
        scale = self._money_scale(user_id)
        self._verify_operation(payload)
        self._verify_client_amounts(payload, scale=scale)
        self._verify_customer_tracked_account(payload)
        self._verify_variants_belong_to_items(payload)
        self._verify_returned_lines(payload)
        priced = price_settlements(self._session, payload, scale=scale)

        voucher = self._vouchers.create(
            VoucherDraft(
                document_type=SALES_DOCUMENT_TYPE,
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
        body = SalesInvoice(id=voucher.id, kind=payload.kind)
        self._fill_body(body, payload)
        self._session.add(body)
        self._write_lines(voucher.id, payload)
        self._write_settlements(voucher.id, priced)
        self._apply_usage(self._usage_of(payload))

        if value_of(self._session, key=SAVE_ALSO_POSTS_KEY, user_id=user_id) is True:
            self.post(voucher.id, user_id=user_id, acknowledged_warnings=acknowledged_warnings)
        return voucher

    def update(
        self,
        voucher_id: UUID,
        payload: SalesInvoiceIn,
        *,
        expected_row_version: int,
        user_id: int,
    ) -> Voucher:
        """Sửa hóa đơn Đã cất: thay trọn bộ dòng và đối trừ.

        Chi nhánh bất biến (dãy số theo chi nhánh). Loại chứng từ cũng bất biến
        dù cùng dãy `SAL`: đổi hóa đơn bán thành trả lại hàng là đảo chiều toàn
        bộ bút toán và đổi cách ghi sổ phụ — một chứng từ khác.
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
        self._verify_client_amounts(payload, scale=scale)
        self._verify_customer_tracked_account(payload)
        self._verify_variants_belong_to_items(payload)
        self._verify_returned_lines(payload)
        priced = price_settlements(self._session, payload, scale=scale)

        usage_before = self._usage_of_stored(body)

        voucher.document_date = payload.document_date
        if payload.posting_date != voucher.posting_date:
            self._vouchers.move_to_date(voucher, posting_date=payload.posting_date)
        voucher.currency_code = payload.currency_code
        voucher.exchange_rate = payload.exchange_rate
        voucher.description = payload.description
        self._fill_body(body, payload)

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
        self.clear_after_unpost(voucher_id, user_id=user_id)
        return voucher

    def sync_after_post(self, voucher_id: UUID, *, user_id: int) -> None:
        """Việc sau ghi sổ — hook `after_post`: hóa đơn thường ghi khoản phải
        thu vào sổ phụ; trả lại / giảm giá cộng số đã giảm vào hóa đơn gốc.
        Chứng từ "kiêm phiếu xuất kho" sinh phiếu kho (lát 8A)."""
        body = self._require_body(voucher_id)
        voucher = self._vouchers.require(voucher_id)
        if body.kind in REVERSING_KINDS:
            apply_settlements(self._session, voucher_id=voucher_id)
        else:
            self._require_subledger().record(
                self._session,
                voucher_id=voucher_id,
                entries=self._subledger_entries(voucher, body, scale=self._money_scale(user_id)),
            )
        self._sync_inventory(voucher, user_id=user_id)

    def clear_after_unpost(self, voucher_id: UUID, *, user_id: int) -> None:
        """Việc sau bỏ ghi sổ — hook `after_unpost`: gỡ đúng thứ `sync_after_post` ghi."""
        body = self._require_body(voucher_id)
        inventory = PROVIDERS.inventory_posting()
        if inventory is not None:
            inventory.remove_movement(self._session, source_voucher_id=voucher_id, user_id=user_id)
        # Phiếu xuất sinh (và bút toán giá vốn của nó) vừa mất theo → cờ hạ
        # cùng transaction; engine 8B lật lại khi tính xong lượt ghi sổ sau.
        body.cogs_posted = False
        if body.kind in REVERSING_KINDS:
            revert_settlements(self._session, voucher_id=voucher_id)
            return
        self._require_subledger().remove(self._session, voucher_id=voucher_id)

    def _sync_inventory(self, voucher: Voucher, *, user_id: int) -> None:
        """FR-SAL-003: chứng từ kiêm phiếu xuất kho → phiếu xuất (hoặc nhập,
        với hàng bán trả lại) qua `InventoryPosting`; cùng bộ dòng mà
        `InventoryLineSource` kể cho guard (`inventory_lines.planned_movement`)."""
        inventory = PROVIDERS.inventory_posting()
        if inventory is None:
            return
        planned = planned_movement(self._session, voucher.id)
        if planned is None:
            return
        inventory.create_movement(
            self._session,
            kind=planned.kind,
            source_voucher_id=voucher.id,
            branch_id=voucher.branch_id,
            posting_date=voucher.posting_date,
            currency_code=voucher.currency_code,
            exchange_rate=voucher.exchange_rate,
            lines=planned.lines,
            user_id=user_id,
            operation_code=planned.operation_code,
            partner_id=planned.partner_id,
            partner_kind=planned.partner_kind,
            description=planned.description,
        )

    def delete(self, voucher_id: UUID) -> None:
        """Xóa hóa đơn Đã cất — trả bộ đếm tham chiếu rồi để CASCADE dọn bảng con."""
        body = self._session.get(SalesInvoice, voucher_id)
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
    ) -> tuple[Voucher, SalesInvoice, list[SalesInvoiceLine], list[SalesSettlement]]:
        voucher = self._vouchers.require(voucher_id)
        return (
            voucher,
            self._require_body(voucher_id),
            self._lines_of(voucher_id),
            self._stored_settlements(voucher_id),
        )

    # ------------------------------------------------------------- nội bộ

    def _require_body(self, voucher_id: UUID) -> SalesInvoice:
        body = self._session.get(SalesInvoice, voucher_id)
        if body is None:
            raise PostingValidationError(
                "Chứng từ này không phải hóa đơn bán hàng",
                violations=[
                    PostingViolation(
                        BODY_MISSING_CODE,
                        "Header tồn tại nhưng không có thân hóa đơn bán",
                        voucher_id=str(voucher_id),
                    )
                ],
            )
        return body

    def _require_subledger(self) -> ArApSubledger:
        subledger = PROVIDERS.ar_ap_subledger()
        if subledger is None:
            # Lỗi lắp ráp, không phải dữ liệu: `model_registry` nạp module chủ
            # sổ phụ trước mọi request. Ghi sổ mà không có sổ phụ là để 131 có
            # số dư mà không hóa đơn nào đối trừ được.
            raise RuntimeError("Chưa có bản cài ArApSubledger — sổ phụ công nợ chưa được đăng ký")
        return subledger

    def _lines_of(self, voucher_id: UUID) -> list[SalesInvoiceLine]:
        return list(
            self._session.execute(
                select(SalesInvoiceLine)
                .where(SalesInvoiceLine.voucher_id == voucher_id)
                .order_by(SalesInvoiceLine.line_no)
            )
            .scalars()
            .all()
        )

    def _stored_settlements(self, voucher_id: UUID) -> list[SalesSettlement]:
        return list(
            self._session.execute(
                select(SalesSettlement).where(SalesSettlement.voucher_id == voucher_id)
            )
            .scalars()
            .all()
        )

    def _fill_body(self, body: SalesInvoice, payload: SalesInvoiceIn) -> None:
        body.operation_code = payload.operation_code
        body.customer_id = payload.customer_id
        body.salesperson_id = payload.salesperson_id
        body.ship_to = payload.ship_to
        body.recipient_name = payload.recipient_name
        body.invoice_form = payload.invoice_form
        body.invoice_serial = payload.invoice_serial
        body.invoice_no = payload.invoice_no
        body.invoice_date = payload.invoice_date
        body.payment_term_id = payload.payment_term_id
        body.due_date = self._due_date_of(payload)
        body.receivable_account_id = payload.receivable_account_id
        body.adjusts_voucher_id = payload.adjusts_voucher_id
        body.price_list_id = payload.price_list_id
        body.is_stock_issue = payload.is_stock_issue
        body.total_before_tax_fc = sum((line.amount_fc for line in payload.lines), _ZERO)
        body.total_discount_fc = sum((line.discount_amount_fc for line in payload.lines), _ZERO)
        body.total_vat_fc = sum((line.vat_amount_fc for line in payload.lines), _ZERO)
        body.total_fc = body.total_before_tax_fc + body.total_vat_fc

    def _write_lines(self, voucher_id: UUID, payload: SalesInvoiceIn) -> None:
        for index, line in enumerate(payload.lines, start=1):
            self._session.add(
                SalesInvoiceLine(
                    voucher_id=voucher_id,
                    line_no=index,
                    description=line.description,
                    item_id=line.item_id,
                    unit_id=line.unit_id,
                    warehouse_id=line.warehouse_id,
                    variant_id=line.variant_id,
                    quantity=line.quantity,
                    unit_price_fc=line.unit_price_fc,
                    discount_percent=line.discount_percent,
                    discount_amount_fc=line.discount_amount_fc,
                    amount_fc=line.amount_fc,
                    vat_rate=line.vat_rate,
                    vat_amount_fc=line.vat_amount_fc,
                    account_id=line.account_id,
                    vat_account_id=line.vat_account_id,
                    cogs_account_id=line.cogs_account_id,
                    inventory_account_id=line.inventory_account_id,
                    unit_cost_fc=line.unit_cost_fc,
                    returned_line_id=line.returned_line_id,
                    price_list_id=line.price_list_id,
                    price_source=(
                        line.price_source.value if line.price_source is not None else None
                    ),
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

    def _write_settlements(self, voucher_id: UUID, priced: list[PricedSettlement]) -> None:
        for row in priced:
            self._session.add(
                SalesSettlement(
                    voucher_id=voucher_id,
                    target_kind=row.target_kind.value,
                    target_id=row.target_id,
                    amount_fc=row.amount_fc,
                    amount=row.amount,
                    fx_diff=row.fx_diff,
                )
            )
        self._session.flush()

    def _due_date_of(self, payload: SalesInvoiceIn) -> date | None:
        """FR-SAL-009: client gửi hạn thì giữ; không thì `document_date +
        due_days` của điều khoản trên chứng từ, và chứng từ không chọn điều
        khoản thì lấy điều khoản khai trên **danh mục khách hàng**.

        Vế cuối là chỗ hóa đơn bán khác hóa đơn mua: SRS 06 §3.1 nói thẳng
        "Điều khoản thanh toán (tự lấy theo khách hàng)". Rơi về danh mục ở
        server chứ không chỉ điền sẵn ở form vì hạn thanh toán là thứ cảnh báo
        nợ quá hạn đọc — một chứng từ lập qua API mà không có hạn sẽ không bao
        giờ kêu quá hạn.

        Điều khoản không tra được → để trống hạn; chính khóa ngoại
        `payment_term_id` chặn lượt cất ấy ngay sau đó (`ReferenceNotFoundError`
        ở tầng chung), cùng đường với mọi id danh mục sai khác của chứng từ.
        """
        if payload.due_date is not None:
            return payload.due_date
        term_id = payload.payment_term_id
        if term_id is None:
            term_id = self._session.execute(
                select(Partner.payment_term_id).where(Partner.id == payload.customer_id)
            ).scalar_one_or_none()
        if term_id is None:
            return None
        term = self._session.get(PaymentTerm, term_id)
        if term is None:
            return None
        return payload.document_date + timedelta(days=term.due_days)

    def _verify_customer_tracked_account(self, payload: SalesInvoiceIn) -> None:
        """TK sẽ sinh dòng sổ phụ công nợ phải theo dõi `customer`.

        Sổ phụ ghi một dòng cho (khách hàng, TK) của hóa đơn; check toàn vẹn
        131/331 đối chiếu sổ phụ với sổ cái **trên các TK theo dõi đối tác**.
        Cho hóa đơn bán ghi Nợ 111 là sinh một khoản "phải thu" mà sổ cái không
        có, và màn đối trừ thì liệt kê nó mãi. Chặn từ lúc cất, cùng chỗ với
        các phép kiểm khác — cùng hình dạng với `purchase`, đổi chiều theo dõi.
        """
        account_id = payload.receivable_account_id
        accounts = accounts_by_id(self._session, [account_id])
        account = accounts.get(account_id)
        if account is not None and DetailTracking.CUSTOMER in (account.detail_tracking or ()):
            return
        raise PostingValidationError(
            "Tài khoản công nợ trên hóa đơn không theo dõi theo khách hàng",
            violations=[
                PostingViolation(
                    RECEIVABLE_ACCOUNT_NOT_CUSTOMER_TRACKED_CODE,
                    "Chọn TK theo dõi theo khách hàng (131…)",
                    account_id=account_id,
                    account_code=account.code if account is not None else None,
                )
            ],
        )

    def _verify_variants_belong_to_items(self, payload: SalesInvoiceIn) -> None:
        """Quy cách trên dòng phải là quy cách của **chính** mã hàng dòng ấy khai.

        Cặp (mã hàng, quy cách) không diễn đạt được bằng một khóa ngoại một cột,
        nên phép kiểm nằm ở đây. Một truy vấn cho cả chứng từ: đọc từng dòng là
        N lượt đi DB cho một phép kiểm mà tập id đã biết trước.
        """
        # Tập các CẶP, không phải bảng tra quy cách → mã hàng. Một `dict` khóa
        # `variant_id` gộp hai dòng khai cùng quy cách với hai mã hàng khác nhau
        # thành một mục, và **dòng cuối thắng**: hóa đơn có dòng sai đứng trước
        # một dòng đúng sẽ đi qua phép kiểm này. Chính là ca vòng review bắt được.
        wanted = {
            (line.item_id, line.variant_id)
            for line in payload.lines
            if line.variant_id is not None and line.item_id is not None
        }
        if not wanted:
            return
        owner_rows = self._session.execute(
            select(ItemVariant.id, ItemVariant.item_id).where(
                ItemVariant.id.in_(sorted({variant_id for _, variant_id in wanted}))
            )
        ).all()
        owners: dict[int, int] = {row.id: row.item_id for row in owner_rows}
        violations = [
            PostingViolation(
                VARIANT_NOT_OF_ITEM_CODE,
                "Chọn quy cách thuộc đúng mã hàng của dòng",
                variant_id=variant_id,
                item_id=item_id,
            )
            # Quy cách không tồn tại cũng vào đây: `owners.get` trả `None`, và
            # "không có dòng danh mục" không bao giờ khớp mã hàng nào.
            for item_id, variant_id in sorted(wanted)
            if owners.get(variant_id) != item_id
        ]
        if violations:
            raise PostingValidationError(
                "Quy cách trên dòng không thuộc mã hàng của dòng", violations=violations
            )

    def _verify_returned_lines(self, payload: SalesInvoiceIn) -> None:
        """Dòng trả lại chỉ dòng bán gốc (FR-STK-004, 8C-1): chỉ trên chứng từ
        `RETURN`; dòng gốc thuộc hóa đơn bán hàng hóa / đại lý **đã ghi sổ**, cùng
        khách hàng, cùng mã hàng. Không kiểm Σ số lượng trả ≤ số lượng gốc — trả
        nhiều lần là thường, và số lượng thuộc màn 8G/8H. Một truy vấn cho cả
        chứng từ, cùng lối `_verify_variants_belong_to_items`."""
        wanted = {
            line.returned_line_id: line.item_id
            for line in payload.lines
            if line.returned_line_id is not None
        }
        if not wanted:
            return
        if payload.kind != SalesInvoiceKind.RETURN:
            raise PostingValidationError(
                "Chỉ chứng từ hàng bán trả lại mới chỉ dòng bán gốc",
                violations=[
                    PostingViolation(
                        RETURNED_LINE_KIND_CODE,
                        "Bỏ dòng bán gốc, hoặc lập chứng từ hàng bán trả lại",
                        kind=payload.kind,
                    )
                ],
            )
        rows = self._session.execute(
            select(
                SalesInvoiceLine.id,
                SalesInvoiceLine.item_id,
                SalesInvoice.kind,
                SalesInvoice.customer_id,
                Voucher.status,
                Voucher.posting_date,
            )
            .join(SalesInvoice, SalesInvoice.id == SalesInvoiceLine.voucher_id)
            .join(Voucher, Voucher.id == SalesInvoiceLine.voucher_id)
            .where(SalesInvoiceLine.id.in_(sorted(wanted)))
        ).all()
        found = {row.id: row for row in rows}
        violations = [
            PostingViolation(
                RETURNED_LINE_MISMATCH_CODE,
                "Dòng bán gốc phải thuộc hóa đơn bán hàng đã ghi sổ của cùng khách hàng, "
                "cùng mã hàng, ngày ghi sổ không muộn hơn chứng từ trả lại",
                returned_line_id=str(line_id),
                item_id=item_id,
            )
            for line_id, item_id in sorted(wanted.items(), key=lambda pair: str(pair[0]))
            if (row := found.get(line_id)) is None
            or row.kind not in _RETURNABLE_KINDS
            or row.customer_id != payload.customer_id
            or row.item_id != item_id
            or row.status not in (VoucherStatus.DA_GHI_SO, VoucherStatus.DA_KHOA_SO)
            # Trả lại không đứng trước lần bán: phiếu nhập lấy giá lần xuất mà
            # đứng trước nó là vòng giá không hội tụ (review 8C-1 M-4).
            or row.posting_date > payload.posting_date
        ]
        if violations:
            raise PostingValidationError(
                "Dòng bán gốc của hàng trả lại không hợp lệ", violations=violations
            )

    def _verify_operation(self, payload: SalesInvoiceIn) -> None:
        """FR-SYS-025: nghiệp vụ phải thuộc gói hiệu lực cho loại `SAL`."""
        year = fiscal_year_covering(self._session, payload.posting_date)
        if year is None:
            # Không có năm tài chính thì `VoucherService.create` sẽ từ chối vì
            # không tra được kỳ — thông điệp bên đó đúng chỗ hơn.
            return
        resolved = operations_for(
            self._session,
            document_type=SALES_DOCUMENT_TYPE,
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
                        "Chọn nghiệp vụ trong danh sách của hóa đơn bán",
                        operation_code=payload.operation_code,
                        document_type=SALES_DOCUMENT_TYPE,
                    )
                ],
            )
        self._verify_operation_partner(payload, operation)

    def _verify_operation_partner(
        self, payload: SalesInvoiceIn, operation: AutoPostingOperation
    ) -> None:
        # Hóa đơn luôn có khách hàng; thứ còn phải kiểm là gói không khai một
        # nghiệp vụ SAL cho loại đối tác khác — dữ liệu gói lệch thì lộ ra ở
        # đây thay vì ở sổ phụ.
        if (
            operation.requires_partner
            and operation.partner_kind is not None
            and operation.partner_kind != PartnerKind.CUSTOMER.value
        ):
            raise PostingValidationError(
                "Loại đối tác không khớp với nghiệp vụ",
                violations=[
                    PostingViolation(
                        OPERATION_PARTNER_REQUIRED_CODE,
                        "Nghiệp vụ này làm việc với loại đối tác khác khách hàng",
                        operation_code=payload.operation_code,
                        expected_kind=operation.partner_kind,
                        actual_kind=PartnerKind.CUSTOMER.value,
                    )
                ],
            )

    def _verify_client_amounts(self, payload: SalesInvoiceIn, *, scale: int) -> None:
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
        self, voucher: Voucher, body: SalesInvoice, *, scale: int
    ) -> list[SubledgerEntry]:
        """Khoản phải thu của hóa đơn: **một dòng duy nhất** cho (khách hàng, TK
        phải thu), gồm doanh thu sau chiết khấu + thuế GTGT đầu ra.

        Một dòng chứ không nhiều như chiều mua: hóa đơn mua có thể nợ nhiều nhà
        cung cấp qua các khoản chi phí mua hàng, còn hóa đơn bán chỉ có một
        người mua và một TK phải thu.

        Số VND cộng từ **từng phần đã quy đổi** — đúng cách engine quy đổi mỗi
        `PostingLine` — để sổ phụ và số dư 131 trên sổ cái khớp từng đồng thay
        vì lệch phần lẻ làm tròn của `round(tổng × tỷ giá)`.

        Chỉ sổ tài chính (`ledger=0`): nguồn đối trừ của `receivables` chỉ cộng
        số đã trả vào sổ ấy, nên dòng sổ quản trị sẽ không bao giờ đóng — báo
        cáo tuổi nợ sổ quản trị vì thế trống thay vì sai.
        """
        rate = voucher.exchange_rate
        lines = self._lines_of(voucher.id)
        amount_fc = sum((line.amount_fc + line.vat_amount_fc for line in lines), _ZERO)
        amount = sum(
            (
                convert_currency(line.amount_fc, rate, scale)
                + convert_currency(line.vat_amount_fc, rate, scale)
                for line in lines
            ),
            _ZERO,
        )
        return [
            SubledgerEntry(
                target_kind=SettlementTargetKind.SALES_INVOICE,
                partner_kind=PartnerKind.CUSTOMER,
                partner_id=body.customer_id,
                ledger=0,
                account_id=body.receivable_account_id,
                document_no=voucher.voucher_no,
                document_date=voucher.document_date,
                due_date=body.due_date,
                currency_code=voucher.currency_code,
                exchange_rate=rate,
                amount_fc=amount_fc,
                amount=amount,
                description=voucher.description,
            )
        ]

    # ----------------------------------------------- bộ đếm tham chiếu danh mục

    def _usage_of(self, payload: SalesInvoiceIn) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        counters[(PARTNER_TABLE_NAME, payload.customer_id)] += 1
        if payload.salesperson_id is not None:
            counters[(EMPLOYEE_TABLE_NAME, payload.salesperson_id)] += 1
        for line in payload.lines:
            if line.variant_id is not None:
                counters[(ITEM_VARIANT_TABLE_NAME, line.variant_id)] += 1
        return counters

    def _usage_of_stored(self, body: SalesInvoice) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        counters[(PARTNER_TABLE_NAME, body.customer_id)] += 1
        if body.salesperson_id is not None:
            counters[(EMPLOYEE_TABLE_NAME, body.salesperson_id)] += 1
        for line in self._lines_of(body.id):
            if line.variant_id is not None:
                counters[(ITEM_VARIANT_TABLE_NAME, line.variant_id)] += 1
        return counters

    def _apply_usage(self, counters: Counter[tuple[str, int]]) -> None:
        for (entity_type, entity_id), delta in sorted(counters.items()):
            if delta == 0:
                continue
            record_use(self._session, entity_type=entity_type, entity_id=entity_id, delta=delta)
