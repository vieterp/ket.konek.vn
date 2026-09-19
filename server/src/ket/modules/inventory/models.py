"""Phiếu kho và sổ kho (SRS 09, lát 8A) — bảng thuộc module `inventory`.

Hai lớp bảng, hai vòng đời:

* **Phiếu** — `inventory_vouchers` (thân một-một với header `vouchers`) và
  `inventory_voucher_lines`: thứ người dùng gõ và sửa lại, cùng khuôn
  `cash_vouchers`/`cash_voucher_lines`. Ba loại phiếu NK / XK / CK gom trong
  `kind`, cùng lối PT/PC: cùng một nghiệp vụ kho nhìn ở ba chiều.
* **Sổ kho** — `inventory_movements`: dẫn xuất của dòng phiếu **lúc ghi sổ**,
  append-only như `gl_postings` (sự thật đã ghi nằm ở đây, không ở dòng phiếu).
  Phiếu chuyển kho sinh **cặp** movement (−1 kho đi, +1 kho đến) cùng
  `voucher_id` — BR-STK-06 "chuyển kho không đổi tổng giá trị" vì thế là bất
  biến cấu trúc, không phải phép kiểm.

Bốn bảng còn lại của §Architecture phase 8 tạo **rỗng** ở migration đầu để khóa
hình dạng (LD-09): `lots`/`serials` (lô, serial — có mặt từ ngày đầu kể cả khi
UI ra ở v1.1: `lot_id`/`serial_id` NULL nghĩa "không theo dõi", thêm sau là
migrate mọi bảng tồn kho), `inventory_balances` + `stock_layers` (8B ghi),
`warehouse_book` (thủ kho 8D ghi), `inventory_recalc_queue` (8A đánh dấu, 8B đọc).

**Giá**: `unit_cost` trên movement NULL khi chưa tính (`cost_state = 0`) — phiếu
xuất ghi sổ được mà không có giá, và bút toán giá vốn thuộc engine 8B (repost).
Số lượng movement là **đơn vị chính** (`base_quantity` của dòng, FR-STK-006);
số theo đơn vị gõ chỉ sống trên dòng phiếu để vẽ lại form và in.

`sequence_in_day` (FR-STK-017, BR-STK-04): thứ tự trong ngày theo khóa tồn kho
`(chi nhánh, kho, vật tư, lô)` — cấp lúc ghi sổ, sửa được bằng lượt sắp xếp lại;
không có nó thì FIFO/BQ tức thời không xác định.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.config.auto_posting_models import OPERATION_CODE_MAX_LENGTH
from ket.kernel.contracts import PartnerKind
from ket.kernel.identifiers import uuid7
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

DESCRIPTION_MAX_LENGTH = 500
LOT_NO_MAX_LENGTH = 50
SERIAL_NO_MAX_LENGTH = 100
REASON_MAX_LENGTH = 200

UNIT_COST_PRECISION: Final[int] = 18
UNIT_COST_SCALE: Final[int] = 6
"""Đơn giá vốn — lẻ hơn tiền (một đồng chia cho một số lượng lẻ) nhưng vẫn là
tiền, nên precision của `AMOUNT`; scale 6 như tỷ giá (cùng lý do: nó bị NHÂN)."""

RECEIPT_DOCUMENT_TYPE: Final[str] = "NK"
ISSUE_DOCUMENT_TYPE: Final[str] = "XK"
TRANSFER_DOCUMENT_TYPE: Final[str] = "CK"
ASSEMBLY_DOCUMENT_TYPE: Final[str] = "LR"
DISASSEMBLY_DOCUMENT_TYPE: Final[str] = "TD"
INVENTORY_DOCUMENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        RECEIPT_DOCUMENT_TYPE,
        ISSUE_DOCUMENT_TYPE,
        TRANSFER_DOCUMENT_TYPE,
        ASSEMBLY_DOCUMENT_TYPE,
        DISASSEMBLY_DOCUMENT_TYPE,
    }
)

ALLOCATION_RATIO_PRECISION: Final[int] = 18
ALLOCATION_RATIO_SCALE: Final[int] = 6
"""Tỷ lệ phân bổ giá trị khi tháo dỡ (SRS 09 §2.3) — con số tương đối, engine
chia theo `r_i / Σr` nên đơn vị (phần trăm hay phần) không quan trọng; scale 6
như tỷ giá vì nó bị NHÂN vào tiền."""


class InventoryVoucherKind:
    """`inventory_vouchers.kind` — hằng số nguyên, cùng lối `CashVoucherKind`."""

    RECEIPT = 0
    """Phiếu nhập kho (NK, mẫu 01-VT)."""
    ISSUE = 1
    """Phiếu xuất kho (XK, mẫu 02-VT)."""
    TRANSFER = 2
    """Phiếu chuyển kho (CK): một phiếu, hai movement — đi và đến."""
    ASSEMBLY = 3
    """Phiếu lắp ráp (LR, FR-STK-016, lát 8C-2): N dòng linh kiện XUẤT theo định
    mức + một dòng thành phẩm NHẬP (`is_product`); giá thành phẩm = Σ giá trị
    linh kiện, engine chép mỗi vòng (`assembly_in_legs.sql`)."""
    DISASSEMBLY = 4
    """Phiếu tháo dỡ (TD): một dòng thành phẩm XUẤT + N dòng linh kiện NHẬP chia
    giá trị theo `allocation_ratio` (`disassembly_in_legs.sql`)."""


def line_issues_stock(kind: int, *, is_product: bool) -> bool:
    """Dòng này XUẤT kho hay không, theo loại phiếu và vai dòng.

    NK nhập; XK xuất; CK: mỗi dòng cả hai vế (hàm trả `True` — vế đi là vế mang
    giá, người gọi cần vế đến thì hỏi riêng); LR: linh kiện xuất, thành phẩm
    nhập; TD: thành phẩm xuất, linh kiện nhập. Một chỗ cho service (đích danh,
    giá), guard (tồn âm) và sổ kho (`_legs`) cùng đọc.
    """
    if kind == InventoryVoucherKind.RECEIPT:
        return False
    if kind in (InventoryVoucherKind.ISSUE, InventoryVoucherKind.TRANSFER):
        return True
    if kind == InventoryVoucherKind.ASSEMBLY:
        return not is_product
    return is_product


class KeeperStatus:
    """`inventory_vouchers.keeper_status` (SRS 17 §3.2, FR-WHK-010..014) — đúng
    khuôn `TreasurerStatus`; hàng đợi thủ kho thi hành ở lát 8D, cột có sẵn từ
    8A để 8D không phải migrate thân phiếu."""

    PENDING = 0
    """Chờ thủ kho ghi sổ kho."""
    BOOKED = 1
    """Thủ kho đã ghi sổ kho."""
    NOT_APPLICABLE = 2
    """Phân hệ thủ kho tắt — phiếu vào thẳng sổ kho lúc ghi sổ kế toán."""


class MovementDirection:
    """`inventory_movements.direction`."""

    IN = 1
    OUT = -1


class CostState:
    """`inventory_movements.cost_state` (§Architecture phase 8)."""

    PENDING = 0
    """Chưa tính giá — mọi dòng xuất ngay sau ghi sổ, và dòng nhập chưa có giá."""
    COSTED = 1
    """Đã tính; `unit_cost`/`amount` là số thật."""
    STALE = 2
    """Cần tính lại — chèn chứng từ lùi ngày hoặc đổi thứ tự trong ngày."""


class InventoryVoucher(DatasetBase, Audited):
    """Phần thân phiếu kho — một-một với header `vouchers`."""

    __tablename__ = "inventory_vouchers"
    __table_args__ = (
        CheckConstraint(
            f"kind BETWEEN {InventoryVoucherKind.RECEIPT} AND {InventoryVoucherKind.DISASSEMBLY}",
            name="kind_known",
        ),
        CheckConstraint("operation_code <> ''", name="operation_code_not_blank"),
        # Kho đến chỉ có ở phiếu chuyển, và phải khác kho đi.
        CheckConstraint(
            f"(kind = {InventoryVoucherKind.TRANSFER}) = (to_warehouse_id IS NOT NULL)",
            name="transfer_has_destination",
        ),
        CheckConstraint(
            "to_warehouse_id IS NULL OR to_warehouse_id <> warehouse_id",
            name="transfer_changes_warehouse",
        ),
        CheckConstraint(
            f"keeper_status BETWEEN {KeeperStatus.PENDING} AND {KeeperStatus.NOT_APPLICABLE}",
            name="keeper_status_known",
        ),
        CheckConstraint(
            "(keeper_posted_at IS NULL) = (keeper_posted_by IS NULL)",
            name="keeper_stamp_complete",
        ),
        CheckConstraint(
            f"(keeper_status = {KeeperStatus.BOOKED}) = (keeper_book_date IS NOT NULL)",
            name="keeper_booked_has_date",
        ),
        CheckConstraint(
            f"keeper_status <> {KeeperStatus.BOOKED} OR keeper_posted_at IS NOT NULL",
            name="keeper_booked_has_stamp",
        ),
        CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)", name="partner_pair_complete"
        ),
        CheckConstraint(
            f"partner_kind IS NULL OR partner_kind BETWEEN {PartnerKind.CUSTOMER} "
            f"AND {PartnerKind.EMPLOYEE}",
            name="partner_kind_known",
        ),
        Index("ix_inventory_vouchers_keeper_status", "keeper_status"),
        Index("ix_inventory_vouchers_warehouse", "warehouse_id"),
        # Hai câu vế lắp ráp / tháo dỡ của engine đi từ phiếu theo `kind` — chi
        # nhánh không có LR/TD trả rỗng mà không quét sổ kho.
        Index("ix_inventory_vouchers_kind", "kind"),
    )

    id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    operation_code: Mapped[str] = mapped_column(String(OPERATION_CODE_MAX_LENGTH), nullable=False)
    """Nghiệp vụ đã chọn (FR-SYS-025) — `nhap-khac`/`xuat-khac` là lối cho phiếu
    tự định khoản; phiếu sinh từ chứng từ nguồn mang mã nghiệp vụ của nguồn."""

    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    """Kho của phiếu — kho đi với phiếu chuyển. Dòng vẫn mang `warehouse_id`
    riêng (phiếu nhập từ hóa đơn mua có thể rải nhiều kho theo dòng)."""
    to_warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=True
    )
    """Kho đến — chỉ phiếu chuyển."""

    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    delivered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """"Người giao hàng"/"Người nhận hàng" trên mẫu 01-VT/02-VT — chữ tự do."""

    keeper_status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=KeeperStatus.PENDING, server_default="0"
    )
    keeper_posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keeper_posted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keeper_book_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class InventoryVoucherLine(DatasetBase, Audited):
    """Một dòng vật tư của phiếu kho: số lượng theo ĐVT gõ + quy về đơn vị
    chính, giá vốn nếu đã biết, cặp TK bút toán kèm dòng, đủ bộ chiều."""

    __tablename__ = "inventory_voucher_lines"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("base_quantity > 0", name="base_quantity_positive"),
        CheckConstraint("unit_cost_fc IS NULL OR unit_cost_fc >= 0", name="unit_cost_not_negative"),
        CheckConstraint("amount_fc IS NULL OR amount_fc >= 0", name="amount_not_negative"),
        # Bút toán kèm dòng đi cả cặp hoặc không đi: một bên TK là nửa bút toán.
        CheckConstraint(
            "(debit_account_id IS NULL) = (credit_account_id IS NULL)",
            name="account_pair_complete",
        ),
        CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)", name="partner_pair_complete"
        ),
        CheckConstraint(
            "allocation_ratio IS NULL OR allocation_ratio > 0", name="allocation_ratio_positive"
        ),
        Index("ix_inventory_voucher_lines_voucher", "voucher_id", "line_no"),
        # Tối đa MỘT dòng thành phẩm mỗi phiếu lắp ráp/tháo dỡ — service kiểm
        # "đúng một", DB canh nửa "không hai".
        Index(
            "uq_inventory_voucher_lines_product",
            "voucher_id",
            unique=True,
            postgresql_where=text("is_product"),
        ),
        Index("ix_inventory_voucher_lines_source_line", "source_line_id"),
        Index("ix_inventory_voucher_lines_source_movement", "source_movement_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("inventory_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)

    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    item_variant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Mã quy cách (FR-STK-014) — không FK, cùng lối `sales_invoice_lines.variant_id`."""
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="RESTRICT"), nullable=True
    )
    serial_id: Mapped[int | None] = mapped_column(
        ForeignKey("serials.id", ondelete="RESTRICT"), nullable=True
    )

    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units_of_measure.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    """Số lượng theo đơn vị người dùng gõ."""
    base_quantity: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    """Số lượng quy về đơn vị chính của mã hàng (FR-STK-006) — service tính lúc
    cất từ `item_units.factor`; movement chép cột này."""

    unit_cost_fc: Mapped[Decimal | None] = mapped_column(
        Numeric(UNIT_COST_PRECISION, UNIT_COST_SCALE), nullable=True
    )
    """Giá vốn một đơn vị chính, nguyên tệ — bắt buộc trên phiếu nhập gõ tay,
    NULL trên dòng xuất (engine tính) và dòng nhập chờ giá (trả lại hàng bán,
    chuyển kho)."""
    amount_fc: Mapped[Decimal | None] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=True
    )
    """`base_quantity × unit_cost_fc` làm tròn — lưu để bút toán và dòng movement
    đọc cùng một con số."""

    debit_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    credit_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    """Cặp TK của bút toán kèm dòng, điền sẵn từ nghiệp vụ và sửa được. Cả hai
    NULL = dòng không sinh bút toán (phiếu nhập từ hóa đơn mua — bút toán đã ở
    hóa đơn; chuyển kho nội bộ)."""

    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cost_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expense_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extended_dimensions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)

    is_product: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Dòng THÀNH PHẨM của phiếu lắp ráp / tháo dỡ (lát 8C-2) — chiều movement
    ngược với các dòng linh kiện còn lại của phiếu; không cặp TK (bút toán nằm
    trên dòng linh kiện) và không giá (engine suy từ vế kia). Luôn `false` trên
    NK/XK/CK."""
    allocation_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(ALLOCATION_RATIO_PRECISION, ALLOCATION_RATIO_SCALE), nullable=True
    )
    """Tỷ lệ phân bổ giá trị thành phẩm cho dòng linh kiện của phiếu THÁO DỠ —
    chép từ định mức (`item_bom_lines.allocation_ratio`) lúc lập, sửa được;
    chứng từ giữ con số của mình như `base_quantity` giữ `factor`."""

    source_line_id: Mapped[UUID | None] = mapped_column(nullable=True)
    """Dòng chứng từ nguồn (hóa đơn mua/bán) sinh ra dòng này — không FK vì
    bảng đích thuộc module khác."""
    source_movement_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # `use_alter`: dòng phiếu ↔ movement trỏ nhau (movement.line_id / dòng
        # .source_movement_id) — khai là FK thêm sau để SQLAlchemy không phải
        # sắp thứ tự hai bảng theo một vòng.
        ForeignKey("inventory_movements.id", ondelete="RESTRICT", use_alter=True),
        nullable=True,
    )
    """Đích danh (FR-STK-001 phương pháp 4, lát 8B): dòng **xuất** trỏ lần nhập
    nào — bắt buộc khi năm tài chính chọn `specific`, service kiểm cùng khóa tồn
    kho. `RESTRICT` + guard tham chiếu: lần nhập đã bị xuất đích danh trỏ tới
    thì không bỏ ghi sổ được (khác gì rút lớp dưới chân một chứng từ khác)."""


class Lot(DatasetBase, Audited):
    """Lô / hạn sử dụng của một mã hàng (FR-STK-013)."""

    __tablename__ = "lots"
    __table_args__ = (
        CheckConstraint("lot_no <> ''", name="lot_no_not_blank"),
        UniqueConstraint("item_id", "lot_no", name="uq_lots_item_lot_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    lot_no: Mapped[str] = mapped_column(String(LOT_NO_MAX_LENGTH), nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)


class Serial(DatasetBase, Audited):
    """Số serial — giữ chỗ, UI ở v1.1 (LD-09)."""

    __tablename__ = "serials"
    __table_args__ = (
        CheckConstraint("serial_no <> ''", name="serial_no_not_blank"),
        UniqueConstraint("item_id", "serial_no", name="uq_serials_item_serial_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    serial_no: Mapped[str] = mapped_column(String(SERIAL_NO_MAX_LENGTH), nullable=False)


class InventoryMovement(DatasetBase):
    """Một dòng sổ kho — nguồn sự thật của tồn kho, append-only như `gl_postings`.

    Không `Audited`: nó là dẫn xuất của phiếu (đã có nhật ký), và mọi đường
    ghi/xóa đi qua ghi sổ / bỏ ghi sổ phiếu. Mang `branch_id` → RLS chi nhánh;
    mang `period_id` → trigger kỳ khóa (BR-STK-05).
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint(
            f"direction IN ({MovementDirection.OUT}, {MovementDirection.IN})",
            name="direction_known",
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("sequence_in_day > 0", name="sequence_positive"),
        CheckConstraint(
            f"cost_state BETWEEN {CostState.PENDING} AND {CostState.STALE}", name="cost_state_known"
        ),
        # Chưa tính thì giá là NULL; đã tính (kể cả đã cũ — `STALE` giữ giá cũ
        # cho tới khi engine chạy lại) thì phải có giá. Không có "chưa tính mà
        # có giá" lẫn "đã tính mà giá trống".
        CheckConstraint(
            f"(cost_state = {CostState.PENDING}) = (unit_cost IS NULL)",
            name="costed_has_unit_cost",
        ),
        CheckConstraint("(unit_cost IS NULL) = (amount IS NULL)", name="cost_pair_complete"),
        # Một movement hoặc thuộc một dòng phiếu (cả `voucher_id` lẫn `line_id`),
        # hoặc là một lớp tồn đầu kỳ (`opening_layer_id`, 8C-1) — không ở giữa.
        CheckConstraint(
            "(voucher_id IS NULL) = (line_id IS NULL)", name="voucher_line_pair_complete"
        ),
        CheckConstraint(
            "(voucher_id IS NULL) = (opening_layer_id IS NOT NULL)",
            name="opening_layer_xor_voucher",
        ),
        # Thứ tự trong ngày là duy nhất theo khóa tồn kho — hai movement cùng
        # số thứ tự là hai phiếu không phân định được trước/sau (BR-STK-04).
        UniqueConstraint(
            "branch_id",
            "warehouse_id",
            "item_id",
            "lot_key",
            "posting_date",
            "sequence_in_day",
            name="uq_inventory_movements_day_sequence",
        ),
        # Không có chỉ mục riêng theo khóa: `uq_inventory_movements_day_sequence`
        # đã dựng một unique index đúng cột đúng thứ tự (review 8A L-1).
        Index("ix_inventory_movements_voucher", "voucher_id"),
        Index("ix_inventory_movements_period", "period_id"),
        Index("ix_inventory_movements_opening_layer", "opening_layer_id", unique=True),
        # Thứ tự "không theo kho" (FR-STK-007, 8C-1): bước LATERAL của hai câu
        # bình quân gom mọi kho của một mã hàng đi trên chỉ mục này; thứ tự trong
        # ngày giữa các kho = số thứ tự rồi kho rồi id (xem `wavg_moving_branch.sql`).
        Index(
            "ix_inventory_movements_branch_item_order",
            "branch_id",
            "item_id",
            "lot_key",
            "posting_date",
            "sequence_in_day",
            "warehouse_id",
            "id",
        ),
        Index(
            "ix_inventory_movements_needs_cost",
            "cost_state",
            # Chuỗi tĩnh (không f-string) theo cổng `test_no_sql_string_interpolation`;
            # `1` = `CostState.COSTED`, cùng lối viết số trần có chú thích của migration.
            postgresql_where=text("cost_state <> 1"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    voucher_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=True
    )
    line_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("inventory_voucher_lines.id", ondelete="RESTRICT"), nullable=True
    )
    """NULL cặp với `voucher_id` khi movement là lớp tồn đầu kỳ (8C-1) — mọi
    movement khác thuộc đúng một dòng phiếu."""
    opening_layer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("opening_balance_stock_layers.id", ondelete="RESTRICT"), nullable=True
    )
    """Lớp tồn đầu kỳ (`posting.opening_balances`) mà movement này vật chất hóa
    (8C-1): nhập, đã có giá, `posting_date` = ngày đầu năm − 1, `period_id` = kỳ
    đầu năm (khóa kỳ 1 = khóa số dư ban đầu). Engine đọc nó như mọi lần nhập
    trong lịch sử; thay-trọn / xóa nhóm 5 gỡ movement trước khi xóa lớp
    (`RESTRICT`)."""
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    """Khóa ngoại thật (khác `gl_postings`): gộp danh mục dời cột theo
    `pg_catalog` (`merge_service.foreign_keys_to`), nên tồn kho đi theo mã hàng
    đích thay vì kẹt ở id đã xóa."""
    item_variant_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="RESTRICT"), nullable=True
    )
    serial_id: Mapped[int | None] = mapped_column(
        ForeignKey("serials.id", ondelete="RESTRICT"), nullable=True
    )
    lot_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    """`COALESCE(lot_id, 0)` vật chất hóa — để khóa duy nhất và chỉ mục nói về
    cùng một cột thay vì một biểu thức (PostgreSQL không nhận biểu thức trong
    `UNIQUE`; service luôn ghi `lot_key = lot_id or 0`)."""
    is_custodial: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Hàng nhận giữ hộ / bán hộ (BR-STK-07): theo dõi số lượng, **không** tính
    vào giá trị tồn. Cột có từ 8A; nghiệp vụ dùng nó ở 8D."""

    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_id: Mapped[int] = mapped_column(
        ForeignKey("accounting_periods.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_in_day: Mapped[int] = mapped_column(Integer, nullable=False)
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    """Theo đơn vị chính, luôn dương — chiều nằm ở `direction`."""

    unit_cost: Mapped[Decimal | None] = mapped_column(
        Numeric(UNIT_COST_PRECISION, UNIT_COST_SCALE), nullable=True
    )
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=True
    )
    """VND — giá vốn là số sổ cái, không có nguyên tệ ở sổ kho."""
    cost_state: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=CostState.PENDING, server_default="0"
    )
    source_movement_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Movement mà giá của dòng này lấy theo: chiều **xuất** — lần nhập đích danh
    (8B); chiều **nhập** — lần xuất mà hàng bán trả lại quay về (FR-STK-004,
    8C-1: giá nhập = giá xuất của lần ấy, engine chép lại mỗi lượt tính)."""

    cost_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class InventoryBalance(DatasetBase):
    """Snapshot tồn theo kỳ — cùng khuôn `account_balances`; 8B ghi."""

    __tablename__ = "inventory_balances"
    __table_args__ = (
        UniqueConstraint(
            "period_id",
            "branch_id",
            "warehouse_id",
            "item_id",
            "lot_key",
            "serial_key",
            name="uq_inventory_balances_key",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_id: Mapped[int] = mapped_column(
        ForeignKey("accounting_periods.id", ondelete="RESTRICT"), nullable=False
    )
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="RESTRICT"), nullable=True
    )
    serial_id: Mapped[int | None] = mapped_column(
        ForeignKey("serials.id", ondelete="RESTRICT"), nullable=True
    )
    lot_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    serial_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    opening_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False, server_default="0"
    )
    opening_value: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, server_default="0"
    )
    in_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False, server_default="0"
    )
    in_value: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, server_default="0"
    )
    out_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False, server_default="0"
    )
    out_value: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, server_default="0"
    )
    closing_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False, server_default="0"
    )
    closing_value: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, server_default="0"
    )
    computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StockLayer(DatasetBase):
    """Lớp tồn còn lại cho FIFO / đích danh — 8B ghi."""

    __tablename__ = "stock_layers"
    __table_args__ = (
        CheckConstraint("original_qty > 0", name="original_qty_positive"),
        CheckConstraint(
            "remaining_qty >= 0 AND remaining_qty <= original_qty", name="remaining_within_original"
        ),
        Index(
            "ix_stock_layers_open",
            "branch_id",
            "warehouse_id",
            "item_id",
            "lot_key",
            "receipt_date",
            "sequence_in_day",
            postgresql_where=text("remaining_qty > 0"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="CASCADE"), nullable=False
    )
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="RESTRICT"), nullable=True
    )
    lot_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False)
    sequence_in_day: Mapped[int] = mapped_column(Integer, nullable=False)
    original_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    remaining_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False
    )
    unit_cost: Mapped[Decimal] = mapped_column(
        Numeric(UNIT_COST_PRECISION, UNIT_COST_SCALE), nullable=False
    )


class WarehouseBookEntry(DatasetBase):
    """Sổ kho của thủ kho (SRS 17 §3.2) — 8D ghi. Mang `branch_id` → RLS."""

    __tablename__ = "warehouse_book"
    __table_args__ = (
        CheckConstraint("in_qty >= 0 AND out_qty >= 0", name="quantities_not_negative"),
        Index("ix_warehouse_book_key", "warehouse_id", "item_id", "book_date"),
        Index("ix_warehouse_book_voucher", "voucher_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="RESTRICT"), nullable=True
    )
    book_date: Mapped[date] = mapped_column(Date, nullable=False)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=False
    )
    in_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False, server_default="0"
    )
    out_qty: Mapped[Decimal] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=False, server_default="0"
    )
    posted_by: Mapped[int] = mapped_column(Integer, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class InventoryRecalcMark(DatasetBase):
    """Dấu bẩn tính lại giá xuất (SRS 19 §9 #6) — cùng khuôn `balance_recalc_queue`.

    Một dòng = "khóa tồn kho này cần tính lại từ ngày này". Ghi ở 8A (bỏ ghi sổ,
    đổi thứ tự trong ngày), đọc ở 8B; `PeriodLockService` chặn khóa kỳ khi còn
    dấu chạm tới kỳ. Mang `branch_id` → RLS, và vì thế job tính giá 8B chạy
    per-branch như job số dư.
    """

    __tablename__ = "inventory_recalc_queue"
    __table_args__ = (
        UniqueConstraint(
            "branch_id",
            "warehouse_id",
            "item_id",
            "lot_key",
            name="uq_inventory_recalc_queue_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="RESTRICT"), nullable=False
    )
    lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("lots.id", ondelete="RESTRICT"), nullable=True
    )
    lot_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    """Ngày sớm nhất cần tính lại — upsert giữ **min** khi dấu đã có (cùng lối
    `mark_dirty` của số dư: một dấu muộn không được che dấu sớm)."""
    marked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    reason: Mapped[str | None] = mapped_column(String(REASON_MAX_LENGTH), nullable=True)
