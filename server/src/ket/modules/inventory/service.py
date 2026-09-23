"""Dịch vụ phiếu kho NK/XK/CK/LR/TD (FR-STK-010..017) — cùng khuôn `CashVoucherService`.

Mọi hàm nhận `Session` đang mở, không tự commit; Cất-đồng-thời-ghi-sổ
(FR-SYS-061) là một transaction thật. Việc riêng của phân hệ kho:

* **Quy đổi đơn vị** (FR-STK-006): số lượng gõ theo ĐVT nào cũng quy về đơn vị
  chính của mã hàng bằng `item_units.factor` lúc cất; ĐVT không khai cho mã
  hàng là từ chối — đoán tỷ lệ là sai số tồn kho không ai thấy.
* **Chỉ hàng qua kho**: dòng phải trỏ mã hàng `nature ∈ INVENTORY_NATURES`;
  dịch vụ / dòng diễn giải không có tồn để nhập xuất.
* **Lô** (FR-STK-013): `lot_no` gõ tay tra/tạo `lots` theo `(item_id, lot_no)`.
* **Giá nhập**: phiếu nhập gõ tay phải có `unit_cost_fc` ở mọi dòng — một lớp
  tồn không giá là một lớp FIFO không tính được; phiếu sinh từ chứng từ nguồn
  (`create_from_source`) được để trống vì giá đến từ nơi khác (trả lại hàng
  bán: FR-STK-004, 8C).
* **Phiếu sinh từ nguồn không sửa/xóa/bỏ ghi sổ độc lập** khi nguồn còn ghi
  sổ — `guards.refuse_when_source_posted` (đăng ký `EDIT_GUARDS` +
  `REFERENCE_GUARDS`); đường đúng là bỏ ghi sổ chứng từ nguồn, hook của nguồn
  gỡ phiếu qua `InventoryPosting.remove_movement`.
* **Lắp ráp / tháo dỡ** (FR-STK-005/016, lát 8C-2): một phiếu = một dòng thành
  phẩm (`is_product`) + N dòng linh kiện; chiều movement của từng dòng theo
  `models.line_issues_stock`; giá vế nhập do engine suy từ vế xuất
  (`assembly_in_legs.sql` / `disassembly_in_legs.sql`), nên cả phiếu không
  nhận giá và dòng thành phẩm không định khoản.

Sổ kho (`inventory_movements`) dựng ở `sync_after_post` và gỡ ở
`clear_after_unpost` — hai hook đăng ký vào `POSTING_DOCUMENT_REGISTRY`, nên
đường endpoint chung và đường service cho cùng kết quả (bài học 7C-3).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
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
from ket.kernel.master_data.models.item import INVENTORY_NATURES, Item
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.kernel.master_data.models.partner import PARTNER_TABLE_NAME
from ket.kernel.master_data.usage import record_use
from ket.kernel.money import round_money
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule
from ket.kernel.periods.models import InventoryValuationMethod
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.protocols import InventoryMovementKind, InventoryMovementLine
from ket.modules.inventory.costing.engine import sync_source_flags
from ket.modules.inventory.keeper import clear_after_unpost as keeper_clear_after_unpost
from ket.modules.inventory.keeper import sync_after_post as keeper_sync_after_post
from ket.modules.inventory.lots import lot_id_for
from ket.modules.inventory.models import (
    ASSEMBLY_DOCUMENT_TYPE,
    DISASSEMBLY_DOCUMENT_TYPE,
    ISSUE_DOCUMENT_TYPE,
    RECEIPT_DOCUMENT_TYPE,
    TRANSFER_DOCUMENT_TYPE,
    UNIT_COST_SCALE,
    CostState,
    InventoryMovement,
    InventoryVoucher,
    InventoryVoucherKind,
    InventoryVoucherLine,
    MovementDirection,
    line_issues_stock,
)
from ket.modules.inventory.movements import lot_key_of, record_movements, remove_movements
from ket.modules.inventory.posting_mapper import build_posting_request
from ket.modules.inventory.schemas import (
    ASSEMBLY_KINDS,
    InventoryVoucherIn,
    InventoryVoucherLineIn,
)
from ket.posting.contracts import (
    PostingService,
    Voucher,
    VoucherDraft,
    VoucherService,
)

DOCUMENT_TYPE_BY_KIND = {
    InventoryVoucherKind.RECEIPT: RECEIPT_DOCUMENT_TYPE,
    InventoryVoucherKind.ISSUE: ISSUE_DOCUMENT_TYPE,
    InventoryVoucherKind.TRANSFER: TRANSFER_DOCUMENT_TYPE,
    InventoryVoucherKind.ASSEMBLY: ASSEMBLY_DOCUMENT_TYPE,
    InventoryVoucherKind.DISASSEMBLY: DISASSEMBLY_DOCUMENT_TYPE,
}
KIND_BY_MOVEMENT_KIND = {
    InventoryMovementKind.RECEIPT: InventoryVoucherKind.RECEIPT,
    InventoryMovementKind.ISSUE: InventoryVoucherKind.ISSUE,
}

NUMBERING_RULE_BY_KIND = {
    InventoryVoucherKind.RECEIPT: NumberingRule(
        document_type=RECEIPT_DOCUMENT_TYPE, prefix="NK{YY}-", reset_rule=ResetRule.YEARLY
    ),
    InventoryVoucherKind.ISSUE: NumberingRule(
        document_type=ISSUE_DOCUMENT_TYPE, prefix="XK{YY}-", reset_rule=ResetRule.YEARLY
    ),
    InventoryVoucherKind.TRANSFER: NumberingRule(
        document_type=TRANSFER_DOCUMENT_TYPE, prefix="CK{YY}-", reset_rule=ResetRule.YEARLY
    ),
    InventoryVoucherKind.ASSEMBLY: NumberingRule(
        document_type=ASSEMBLY_DOCUMENT_TYPE, prefix="LR{YY}-", reset_rule=ResetRule.YEARLY
    ),
    InventoryVoucherKind.DISASSEMBLY: NumberingRule(
        document_type=DISASSEMBLY_DOCUMENT_TYPE, prefix="TD{YY}-", reset_rule=ResetRule.YEARLY
    ),
}
"""`NK26-00001`/`XK26-00001`/`CK26-00001`/`LR26-00001`/`TD26-00001`, quay về 1
mỗi năm — cùng khuôn PT/PC."""

SOURCE_RECEIPT_OPERATION = "nhap-mua-hang"
SOURCE_ISSUE_OPERATION = "xuat-tra-lai-hang-mua"
SOURCE_SALES_ISSUE_OPERATION = "xuat-ban-hang"
SOURCE_SALES_RETURN_OPERATION = "nhap-hang-ban-tra-lai"

OPERATION_UNKNOWN_CODE = "inventory.operation_unknown"
OPERATION_PARTNER_REQUIRED_CODE = "inventory.operation_partner_required"
KIND_IMMUTABLE_CODE = "inventory.kind_immutable"
ITEM_NOT_STOCKED_CODE = "inventory.item_not_stocked"
UNIT_NOT_DECLARED_CODE = "inventory.unit_not_declared"
RECEIPT_COST_REQUIRED_CODE = "inventory.receipt_cost_required"
ISSUE_COST_NOT_ALLOWED_CODE = "inventory.issue_cost_not_allowed"
SPECIFIC_SOURCE_REQUIRED_CODE = "inventory.specific_source_required"
SPECIFIC_SOURCE_MISMATCH_CODE = "inventory.specific_source_mismatch"
RETURN_SOURCE_MISMATCH_CODE = "inventory.return_source_mismatch"
RECEIPT_COST_CONFLICTS_SOURCE_CODE = "inventory.receipt_cost_conflicts_source"
RETURN_SOURCE_AFTER_RECEIPT_CODE = "inventory.return_source_after_receipt"
ASSEMBLY_COMPONENT_IS_PRODUCT_CODE = "inventory.assembly_component_is_product"
SOURCE_ON_RECEIPT_SIDE_CODE = "inventory.source_on_receipt_side"
CUSTODIAL_LINE_HAS_ACCOUNTS_CODE = "inventory.custodial_line_has_accounts"
CUSTODIAL_KIND_NOT_ALLOWED_CODE = "inventory.custodial_kind_not_allowed"
BODY_MISSING_CODE = "inventory.body_missing"

_USAGE_TABLE_BY_PARTNER_KIND = {
    PartnerKind.CUSTOMER: PARTNER_TABLE_NAME,
    PartnerKind.VENDOR: PARTNER_TABLE_NAME,
    PartnerKind.EMPLOYEE: EMPLOYEE_TABLE_NAME,
}

_ONE = Decimal(1)


class InventoryVoucherService:
    """CRUD + ghi sổ phiếu kho, trong transaction người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._vouchers = VoucherService(session)
        self._posting = PostingService(session)

    # ------------------------------------------------------------- vòng đời

    def create(
        self, payload: InventoryVoucherIn, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Cất; tùy chọn FR-SYS-061 bật thì ghi sổ luôn cùng transaction."""
        self._verify_operation(payload)
        voucher = self._create_voucher(payload, user_id=user_id, source_document_id=None)
        if value_of(self._session, key=SAVE_ALSO_POSTS_KEY, user_id=user_id) is True:
            self.post(voucher.id, user_id=user_id, acknowledged_warnings=acknowledged_warnings)
        return voucher

    def create_from_source(
        self,
        *,
        kind: InventoryMovementKind,
        source_voucher_id: UUID,
        branch_id: int,
        posting_date: date,
        currency_code: str,
        exchange_rate: Decimal,
        lines: Sequence[InventoryMovementLine],
        operation_code: str,
        partner_id: int | None,
        partner_kind: PartnerKind | None,
        description: str | None,
        user_id: int,
    ) -> Voucher:
        """Lập phiếu NK/XK cho một chứng từ nguồn (mua/bán) — đường của
        `InventoryPosting.create_movement`. Không ghi sổ ở đây: bridge ghi sổ
        ngay sau, với cảnh báo đã xác nhận ở chứng từ nguồn."""
        voucher_kind = KIND_BY_MOVEMENT_KIND[kind]
        warehouse_ids = {line.warehouse_id for line in lines}
        issue_movements = self._issue_movements_of_lines(
            [line.cost_from_line_id for line in lines if line.cost_from_line_id is not None]
        )
        payload = InventoryVoucherIn(
            kind=voucher_kind,
            operation_code=operation_code,
            warehouse_id=min(warehouse_ids),
            branch_id=branch_id,
            document_date=posting_date,
            posting_date=posting_date,
            currency_code=currency_code,
            exchange_rate=exchange_rate,
            partner_id=partner_id,
            partner_kind=partner_kind,
            description=description,
            lines=tuple(
                InventoryVoucherLineIn(
                    item_id=line.item_id,
                    item_variant_id=line.item_variant_id,
                    warehouse_id=line.warehouse_id,
                    lot_no=line.lot_no,
                    unit_id=line.unit_id,
                    quantity=line.quantity,
                    unit_cost_fc=line.unit_price_fc,
                    amount_fc=line.amount_fc,
                    debit_account_id=line.debit_account_id,
                    credit_account_id=line.credit_account_id,
                    # Chiều xuất: lần nhập đích danh dòng bán chỉ (8C-2, ADR-027);
                    # chiều nhập: lần xuất mà dòng trả lại quay về (8C-1).
                    source_movement_id=(
                        line.source_movement_id
                        if line.source_movement_id is not None
                        else issue_movements.get(line.cost_from_line_id)
                        if line.cost_from_line_id is not None
                        else None
                    ),
                )
                for line in lines
            ),
        )
        return self._create_voucher(
            payload,
            user_id=user_id,
            source_document_id=source_voucher_id,
            source_line_ids=[line.source_line_id for line in lines],
        )

    def update(
        self,
        voucher_id: UUID,
        payload: InventoryVoucherIn,
        *,
        expected_row_version: int,
        user_id: int,
    ) -> Voucher:
        """Sửa phiếu Đã cất: thay trọn bộ dòng. Chi nhánh và LOẠI phiếu bất biến
        (số chứng từ thuộc dãy của loại) — cùng luật PT/PC."""
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
                "Phiếu đã cất không đổi được loại nhập/xuất/chuyển — xóa rồi lập phiếu mới",
                violations=[
                    PostingViolation(
                        KIND_IMMUTABLE_CODE,
                        "Số chứng từ thuộc dãy của loại phiếu — đổi loại là đổi dãy số",
                        current_kind=body.kind,
                        requested_kind=payload.kind,
                    )
                ],
            )
        self._verify_operation(payload)
        scale = self._money_scale(user_id)
        resolved = self._resolve_lines(payload, scale=scale, requires_cost=True)
        usage_before = self._usage_of_stored(body)

        voucher.document_date = payload.document_date
        if payload.posting_date != voucher.posting_date:
            self._vouchers.move_to_date(voucher, posting_date=payload.posting_date)
        voucher.currency_code = payload.currency_code
        voucher.exchange_rate = payload.exchange_rate
        voucher.description = payload.description

        body.operation_code = payload.operation_code
        body.warehouse_id = payload.warehouse_id
        body.to_warehouse_id = payload.to_warehouse_id
        body.partner_id = payload.partner_id
        body.partner_kind = payload.partner_kind.value if payload.partner_kind is not None else None
        body.delivered_by = payload.delivered_by

        for line in self._lines_of(voucher_id):
            self._session.delete(line)
        self._session.flush()
        self._write_lines(voucher_id, payload, resolved, source_line_ids=None)

        usage_after = self._usage_of(payload)
        usage_after.subtract(usage_before)
        self._apply_usage(usage_after)
        return voucher

    def post(
        self, voucher_id: UUID, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Ghi sổ kế toán + dựng sổ kho, một transaction — cùng mã với hook."""
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
        """Hook `after_post`: dòng phiếu → `inventory_movements`.

        Dòng nhập lấy giá từ lần xuất (FR-STK-004) có thể nhận giá ngay trong
        `record_movements` — bút toán Nợ 156 / Có 632 lúc ấy đã dựng được, nên
        ghi lại chứng từ vừa ghi sổ qua `repost` (ADR-025: cùng validator, không
        hook/guard) thay vì bắt người dùng chạy job tính giá cho một con số đã
        biết; cờ `cogs_posted` của chứng từ nguồn theo đó.
        """
        voucher = self._vouchers.require(voucher_id)
        body = self._require_body(voucher_id)
        created = record_movements(
            self._session,
            voucher=voucher,
            body=body,
            lines=self._lines_of(voucher_id),
            money_scale=self._money_scale(user_id),
        )
        if any(
            movement.direction == MovementDirection.IN
            and movement.source_movement_id is not None
            and movement.cost_state == CostState.COSTED
            for movement in created
        ):
            self._posting.repost(build_posting_request(self._session, voucher_id), user_id=user_id)
            sync_source_flags(self._session, [voucher])
        # Sổ kho của thủ kho dựng SAU sổ kế toán kho: nó đọc chính các movement
        # vừa tạo (phân hệ tắt thì vào thẳng sổ, FR-WHK-021).
        keeper_sync_after_post(self._session, voucher_id, user_id)

    def clear_after_unpost(self, voucher_id: UUID) -> None:
        """Hook `after_unpost`: gỡ đúng thứ `sync_after_post` dựng."""
        voucher = self._vouchers.require(voucher_id)
        # Sổ kho thủ kho gỡ TRƯỚC movement — nó đọc movement để biết gỡ gì thì
        # thôi, nhưng thứ tự này giữ đúng chiều dựng ngược lại.
        keeper_clear_after_unpost(self._session, voucher_id)
        remove_movements(self._session, voucher=voucher)

    def delete(self, voucher_id: UUID) -> None:
        """Xóa phiếu Đã cất — trả bộ đếm tham chiếu rồi để CASCADE dọn bảng con."""
        body = self._session.get(InventoryVoucher, voucher_id)
        if body is not None:
            self.release_usage(voucher_id)
        self._vouchers.delete(voucher_id)

    def release_usage(self, voucher_id: UUID) -> None:
        """Hook `before_delete`."""
        body = self._require_body(voucher_id)
        counters = self._usage_of_stored(body)
        self._apply_usage(Counter({key: -count for key, count in counters.items()}))

    def get(self, voucher_id: UUID) -> tuple[Voucher, InventoryVoucher, list[InventoryVoucherLine]]:
        voucher = self._vouchers.require(voucher_id)
        return voucher, self._require_body(voucher_id), self._lines_of(voucher_id)

    def generated_for_source(self, source_voucher_id: UUID) -> list[Voucher]:
        """Các phiếu kho sinh từ một chứng từ nguồn (thường 0 hoặc 1)."""
        return list(
            self._session.execute(
                select(Voucher)
                .join(InventoryVoucher, InventoryVoucher.id == Voucher.id)
                .where(Voucher.source_document_id == source_voucher_id)
                .order_by(Voucher.voucher_no)
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------- nội bộ

    def _create_voucher(
        self,
        payload: InventoryVoucherIn,
        *,
        user_id: int,
        source_document_id: UUID | None,
        source_line_ids: Sequence[UUID | None] | None = None,
    ) -> Voucher:
        scale = self._money_scale(user_id)
        resolved = self._resolve_lines(
            payload, scale=scale, requires_cost=source_document_id is None
        )
        kind = payload.kind
        voucher = self._vouchers.create(
            VoucherDraft(
                document_type=DOCUMENT_TYPE_BY_KIND[kind],
                branch_id=payload.branch_id,
                document_date=payload.document_date,
                posting_date=payload.posting_date,
                currency_code=payload.currency_code,
                exchange_rate=payload.exchange_rate,
                description=payload.description,
            ),
            rule=NUMBERING_RULE_BY_KIND[kind],
            user_id=user_id,
        )
        voucher.source_document_id = source_document_id
        self._session.add(
            InventoryVoucher(
                id=voucher.id,
                kind=kind,
                operation_code=payload.operation_code,
                warehouse_id=payload.warehouse_id,
                to_warehouse_id=payload.to_warehouse_id,
                partner_id=payload.partner_id,
                partner_kind=(
                    payload.partner_kind.value if payload.partner_kind is not None else None
                ),
                delivered_by=payload.delivered_by,
            )
        )
        self._write_lines(voucher.id, payload, resolved, source_line_ids=source_line_ids)
        self._apply_usage(self._usage_of(payload))
        return voucher

    def _require_body(self, voucher_id: UUID) -> InventoryVoucher:
        body = self._session.get(InventoryVoucher, voucher_id)
        if body is None:
            raise PostingValidationError(
                "Chứng từ này không phải phiếu kho",
                violations=[
                    PostingViolation(
                        BODY_MISSING_CODE,
                        "Header tồn tại nhưng không có thân phiếu kho",
                        voucher_id=str(voucher_id),
                    )
                ],
            )
        return body

    def _lines_of(self, voucher_id: UUID) -> list[InventoryVoucherLine]:
        return list(
            self._session.execute(
                select(InventoryVoucherLine)
                .where(InventoryVoucherLine.voucher_id == voucher_id)
                .order_by(InventoryVoucherLine.line_no)
            )
            .scalars()
            .all()
        )

    def _resolve_lines(
        self, payload: InventoryVoucherIn, *, scale: int, requires_cost: bool
    ) -> list[_ResolvedLine]:
        """Kiểm mã hàng qua kho, quy đổi đơn vị, tra lô, tính thành tiền —
        trả toàn bộ vi phạm một lượt (cùng triết lý bộ kiểm ghi sổ)."""
        item_ids = sorted({line.item_id for line in payload.lines})
        items = {
            item.id: item
            for item in self._session.execute(select(Item).where(Item.id.in_(item_ids)))
            .scalars()
            .all()
        }
        factors = {
            (row.item_id, row.unit_id): row.factor
            for row in self._session.execute(select(ItemUnit).where(ItemUnit.item_id.in_(item_ids)))
            .scalars()
            .all()
        }
        violations: list[PostingViolation] = []
        resolved: list[_ResolvedLine] = []
        needs_cost = requires_cost and payload.kind == InventoryVoucherKind.RECEIPT
        # Giá xuất là việc của engine tính giá (SRS 09 §3, 4 phương pháp) — chứng
        # từ không áp giá xuống được, kể cả gõ tay; phiếu chuyển lấy giá của lớp
        # xuất. Nhận giá ở đây là để `COSTED` một con số engine sẽ không bao
        # giờ tính lại và sổ kho lệch sổ cái từ lượt ghi đầu (review 8A M-3).
        refuses_cost = payload.kind != InventoryVoucherKind.RECEIPT
        # Đích danh (phương pháp 4): dòng xuất/chuyển phải chỉ lần nhập khi năm
        # tài chính chọn `specific`; chỉ được chỉ khi cùng khóa tồn kho. Phiếu
        # sinh từ chứng từ nguồn (`requires_cost=False`) không bị đòi — dòng hóa
        # đơn bán chưa có chỗ chọn lần nhập, engine để chúng chờ giá (ghi ở
        # phase file 8B, đường hoàn thiện thuộc 8C/8G).
        year = fiscal_year_covering(self._session, payload.posting_date)
        specific_year = (
            requires_cost
            and year is not None
            and year.inventory_valuation_method == InventoryValuationMethod.SPECIFIC
        )
        sources = self._source_movements(payload)
        product_item_id = next((line.item_id for line in payload.lines if line.is_product), None)
        for index, line in enumerate(payload.lines, start=1):
            issues = line_issues_stock(payload.kind, is_product=line.is_product)
            # Đích danh chỉ đòi ở dòng XUẤT (linh kiện lắp ráp, thành phẩm tháo
            # dỡ); dòng nhập của LR/TD lấy giá từ vế kia, không có "lần nhập
            # nguồn" để chỉ.
            requires_source = specific_year and issues and not line.is_custodial
            item = items.get(line.item_id)
            if item is None or item.nature not in INVENTORY_NATURES or item.is_group:
                violations.append(
                    PostingViolation(
                        ITEM_NOT_STOCKED_CODE,
                        "Chỉ hàng hóa và thành phẩm mới nhập xuất kho được",
                        line_no=index,
                        item_id=line.item_id,
                    )
                )
                continue
            factor = (
                _ONE
                if line.unit_id == item.base_unit_id
                else factors.get((line.item_id, line.unit_id))
            )
            if factor is None:
                violations.append(
                    PostingViolation(
                        UNIT_NOT_DECLARED_CODE,
                        "Đơn vị tính chưa khai tỷ lệ quy đổi cho mã hàng này",
                        line_no=index,
                        item_id=line.item_id,
                        unit_id=line.unit_id,
                    )
                )
                continue
            if line.is_custodial and (
                line.debit_account_id is not None or line.credit_account_id is not None
            ):
                # BR-STK-07: hàng nhận giữ hộ không phải tài sản của đơn vị, nên
                # không có bút toán nào đúng cho nó. Pydantic đã chặn ở lớp
                # request; canh lần hai ở đây vì phiếu sinh tự động (kiểm kê,
                # cầu mua/bán) không đi qua lớp ấy.
                violations.append(
                    PostingViolation(
                        CUSTODIAL_LINE_HAS_ACCOUNTS_CODE,
                        "Dòng hàng giữ hộ không định khoản — hàng nhận giữ hộ không "
                        "vào giá trị tồn kho của đơn vị",
                        line_no=index,
                    )
                )
                continue
            if line.is_custodial and payload.kind in ASSEMBLY_KINDS:
                # Lắp ráp / tháo dỡ chuyển GIÁ TRỊ giữa các dòng của cùng phiếu;
                # một dòng không có giá trị thì không có gì để chuyển.
                violations.append(
                    PostingViolation(
                        CUSTODIAL_KIND_NOT_ALLOWED_CODE,
                        "Phiếu lắp ráp / tháo dỡ không nhận dòng hàng giữ hộ",
                        line_no=index,
                    )
                )
                continue
            if refuses_cost and (line.unit_cost_fc is not None or line.amount_fc is not None):
                violations.append(
                    PostingViolation(
                        ISSUE_COST_NOT_ALLOWED_CODE,
                        "Phiếu xuất / chuyển / lắp ráp / tháo dỡ không nhận giá — giá do engine tính",
                        line_no=index,
                    )
                )
                continue
            if (
                not line.is_product
                and product_item_id is not None
                and line.item_id == product_item_id
            ):
                violations.append(
                    PostingViolation(
                        ASSEMBLY_COMPONENT_IS_PRODUCT_CODE,
                        "Linh kiện không thể là chính mã hàng thành phẩm của phiếu",
                        line_no=index,
                        item_id=line.item_id,
                    )
                )
                continue
            if (
                not issues
                and payload.kind != InventoryVoucherKind.RECEIPT
                and line.source_movement_id is not None
            ):
                # Dòng nhập của LR/TD: giá do vế kia quyết, không có nguồn.
                violations.append(
                    PostingViolation(
                        SOURCE_ON_RECEIPT_SIDE_CODE,
                        "Dòng nhập của phiếu lắp ráp / tháo dỡ không chỉ lần nhập / lần xuất nguồn",
                        line_no=index,
                    )
                )
                continue
            takes_cost_from_issue = (
                payload.kind == InventoryVoucherKind.RECEIPT and line.source_movement_id is not None
            )
            if (
                needs_cost
                and not line.is_custodial
                and not takes_cost_from_issue
                and line.unit_cost_fc is None
                and line.amount_fc is None
            ):
                violations.append(
                    PostingViolation(
                        RECEIPT_COST_REQUIRED_CODE,
                        "Phiếu nhập kho phải có giá nhập ở mọi dòng",
                        line_no=index,
                    )
                )
                continue
            base_quantity = line.quantity * factor
            # Thành tiền là số chủ khi chứng từ nguồn gửi (giá trị sổ kho = giá
            # trị hóa đơn tới từng đồng); phiếu gõ tay tính từ đơn giá theo ĐVT gõ.
            if line.amount_fc is not None:
                amount_fc: Decimal | None = line.amount_fc
                unit_cost_fc: Decimal | None = round_money(
                    line.amount_fc / base_quantity, UNIT_COST_SCALE
                )
            elif line.unit_cost_fc is not None:
                amount_fc = round_money(line.quantity * line.unit_cost_fc, scale)
                unit_cost_fc = round_money(line.unit_cost_fc / factor, UNIT_COST_SCALE)
            else:
                amount_fc = None
                unit_cost_fc = None
            warehouse_id = (
                payload.warehouse_id
                if payload.kind == InventoryVoucherKind.TRANSFER or line.warehouse_id is None
                else line.warehouse_id
            )
            lot_id = self._lot_id_for(line.item_id, line.lot_no)
            if takes_cost_from_issue:
                # FR-STK-004: hàng bán trả lại "lấy từ giá xuất kho" — dòng nhập
                # chỉ LẦN XUẤT nó quay về; giá do engine chép (`return_in_legs`),
                # nên dòng không được gõ giá. Kho khác được (trả về kho khác), mã
                # hàng phải cùng; lần xuất chưa có giá cũng nhận (nhập chờ giá).
                if line.unit_cost_fc is not None or line.amount_fc is not None:
                    violations.append(
                        PostingViolation(
                            RECEIPT_COST_CONFLICTS_SOURCE_CODE,
                            "Dòng nhập lấy giá từ lần xuất thì không gõ giá — giá do engine chép",
                            line_no=index,
                        )
                    )
                    continue
                source = sources.get(line.source_movement_id or 0)
                if (
                    source is None
                    or source.direction != MovementDirection.OUT
                    or source.branch_id != payload.branch_id
                    or source.item_id != line.item_id
                ):
                    violations.append(
                        PostingViolation(
                            RETURN_SOURCE_MISMATCH_CODE,
                            "Lần xuất nguồn phải là một dòng xuất đã ghi sổ của cùng mã hàng",
                            line_no=index,
                            source_movement_id=line.source_movement_id,
                        )
                    )
                    continue
                # Lần xuất phải đứng TRƯỚC lần nhập trả lại: nhập trước xuất cùng
                # khóa là giá IN = f(giá OUT) = f(giá IN) — vòng co dần tới khi
                # vượt trần vòng của engine và chặn cả chi nhánh (review 8C-1 M-4).
                if source.posting_date > payload.posting_date:
                    violations.append(
                        PostingViolation(
                            RETURN_SOURCE_AFTER_RECEIPT_CODE,
                            "Lần xuất nguồn phải có ngày ghi sổ không muộn hơn phiếu nhập trả lại",
                            line_no=index,
                            source_movement_id=line.source_movement_id,
                            source_posting_date=source.posting_date.isoformat(),
                        )
                    )
                    continue
                resolved.append(
                    _ResolvedLine(
                        base_quantity=base_quantity,
                        unit_cost_fc=None,
                        amount_fc=None,
                        warehouse_id=warehouse_id,
                        lot_id=lot_id,
                        source_movement_id=line.source_movement_id,
                    )
                )
                continue
            if requires_source and line.source_movement_id is None:
                violations.append(
                    PostingViolation(
                        SPECIFIC_SOURCE_REQUIRED_CODE,
                        "Năm tài chính tính giá đích danh — dòng xuất phải chỉ lần nhập nguồn",
                        line_no=index,
                        item_id=line.item_id,
                    )
                )
                continue
            if line.source_movement_id is not None:
                source = sources.get(line.source_movement_id)
                if (
                    source is None
                    or source.direction != MovementDirection.IN
                    or source.unit_cost is None
                    or source.branch_id != payload.branch_id
                    or source.warehouse_id != warehouse_id
                    or source.item_id != line.item_id
                    or source.lot_key != lot_key_of(lot_id)
                ):
                    violations.append(
                        PostingViolation(
                            SPECIFIC_SOURCE_MISMATCH_CODE,
                            "Lần nhập nguồn phải là một dòng nhập đã ghi sổ, đã có giá, của "
                            "cùng kho, mã hàng và lô",
                            line_no=index,
                            source_movement_id=line.source_movement_id,
                        )
                    )
                    continue
            resolved.append(
                _ResolvedLine(
                    base_quantity=base_quantity,
                    unit_cost_fc=unit_cost_fc,
                    amount_fc=amount_fc,
                    warehouse_id=warehouse_id,
                    lot_id=lot_id,
                    source_movement_id=line.source_movement_id,
                )
            )
        if violations:
            raise PostingValidationError("Phiếu kho còn dòng chưa hợp lệ", violations=violations)
        return resolved

    def _issue_movements_of_lines(self, source_line_ids: Sequence[UUID]) -> dict[UUID, int]:
        """`{dòng chứng từ gốc: movement XUẤT}` — dòng bán gốc → dòng phiếu XK sinh
        từ nó (`source_line_id`) → movement chiều ra. Dòng gốc không kiêm xuất kho
        (không phiếu, hay phiếu chưa ghi sổ) thì vắng: phiếu nhập sinh chờ giá
        như 8B, không phải lỗi."""
        if not source_line_ids:
            return {}
        rows = self._session.execute(
            select(InventoryVoucherLine.source_line_id, InventoryMovement.id)
            .join(InventoryMovement, InventoryMovement.line_id == InventoryVoucherLine.id)
            .where(
                InventoryVoucherLine.source_line_id.in_(sorted(set(source_line_ids))),
                InventoryMovement.direction == MovementDirection.OUT,
            )
        ).all()
        return {row.source_line_id: int(row.id) for row in rows}

    def _source_movements(self, payload: InventoryVoucherIn) -> dict[int, InventoryMovement]:
        ids = sorted(
            {
                line.source_movement_id
                for line in payload.lines
                if line.source_movement_id is not None
            }
        )
        if not ids:
            return {}
        return {
            row.id: row
            for row in self._session.execute(
                select(InventoryMovement).where(InventoryMovement.id.in_(ids))
            ).scalars()
        }

    def _lot_id_for(self, item_id: int, lot_no: str | None) -> int | None:
        return lot_id_for(self._session, item_id, lot_no)

    def _write_lines(
        self,
        voucher_id: UUID,
        payload: InventoryVoucherIn,
        resolved: list[_ResolvedLine],
        *,
        source_line_ids: Sequence[UUID | None] | None,
    ) -> None:
        for index, (line, extra) in enumerate(zip(payload.lines, resolved, strict=True), start=1):
            self._session.add(
                InventoryVoucherLine(
                    voucher_id=voucher_id,
                    line_no=index,
                    description=line.description,
                    item_id=line.item_id,
                    item_variant_id=line.item_variant_id,
                    warehouse_id=extra.warehouse_id,
                    lot_id=extra.lot_id,
                    unit_id=line.unit_id,
                    quantity=line.quantity,
                    base_quantity=extra.base_quantity,
                    unit_cost_fc=extra.unit_cost_fc,
                    amount_fc=extra.amount_fc,
                    debit_account_id=line.debit_account_id,
                    credit_account_id=line.credit_account_id,
                    partner_id=line.partner_id,
                    partner_kind=(
                        line.partner_kind.value if line.partner_kind is not None else None
                    ),
                    cost_object_id=line.cost_object_id,
                    project_id=line.project_id,
                    order_id=line.order_id,
                    contract_id=line.contract_id,
                    expense_item_id=line.expense_item_id,
                    extended_dimensions=(
                        {str(value.dimension_id): value.value_id for value in line.extended} or None
                    ),
                    source_line_id=(
                        source_line_ids[index - 1] if source_line_ids is not None else None
                    ),
                    source_movement_id=extra.source_movement_id,
                    is_custodial=line.is_custodial,
                    is_product=line.is_product,
                    allocation_ratio=line.allocation_ratio,
                )
            )
        self._session.flush()

    def _verify_operation(self, payload: InventoryVoucherIn) -> None:
        """FR-SYS-025: nghiệp vụ phải thuộc gói hiệu lực cho loại phiếu."""
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
        self, payload: InventoryVoucherIn, operation: AutoPostingOperation
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

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    # ----------------------------------------------- bộ đếm tham chiếu danh mục

    def _usage_of(self, payload: InventoryVoucherIn) -> Counter[tuple[str, int]]:
        counters: Counter[tuple[str, int]] = Counter()
        if payload.partner_id is not None and payload.partner_kind is not None:
            counters[(_USAGE_TABLE_BY_PARTNER_KIND[payload.partner_kind], payload.partner_id)] += 1
        for line in payload.lines:
            if line.partner_id is not None and line.partner_kind is not None:
                counters[(_USAGE_TABLE_BY_PARTNER_KIND[line.partner_kind], line.partner_id)] += 1
        return counters

    def _usage_of_stored(self, body: InventoryVoucher) -> Counter[tuple[str, int]]:
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


class _ResolvedLine:
    """Phần server tính cho một dòng: số lượng đơn vị chính, thành tiền, kho, lô."""

    __slots__ = (
        "amount_fc",
        "base_quantity",
        "lot_id",
        "source_movement_id",
        "unit_cost_fc",
        "warehouse_id",
    )

    def __init__(
        self,
        *,
        base_quantity: Decimal,
        unit_cost_fc: Decimal | None,
        amount_fc: Decimal | None,
        warehouse_id: int,
        lot_id: int | None,
        source_movement_id: int | None = None,
    ) -> None:
        self.base_quantity = base_quantity
        self.unit_cost_fc = unit_cost_fc
        self.amount_fc = amount_fc
        self.warehouse_id = warehouse_id
        self.lot_id = lot_id
        self.source_movement_id = source_movement_id
