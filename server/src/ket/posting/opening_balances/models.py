"""Số dư ban đầu — schema đủ 10 nhóm, logic nhóm 0–4 ở slice 4C (SRS 02, RT-24).

Số dư đầu kỳ không phải một con số mỗi TK: mười nhóm với chiều chi tiết riêng
(`docs/srs/02` §1.1). Bảng mang **đủ cột cho cả mười nhóm ngay từ đầu** — nhóm
5–9 (tồn kho, dở dang, CCDC, TSCĐ, trả trước) chỉ thêm bảng chi tiết + logic ở
phase 8, không thêm cột vào đây (RT-24, LD-09 cùng một bài học: cột trên bảng
đã có dữ liệu thật thì không thêm sau được).

Cột `ledger` có từ ngày đầu (FR-OPB-008, OQ#5 đã chốt): schema mang hai sổ độc
lập dù UI nhập riêng sổ quản trị hoãn v1.1.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.identifiers import uuid7
from ket.kernel.money import RATE_SCALE_DEFAULT
from ket.kernel.persistence.base import DatasetBase
from ket.posting.engine.models import AMOUNT_PRECISION, AMOUNT_SCALE, Ledger

QUANTITY_PRECISION = 18
QUANTITY_SCALE = 4


class OpeningDetailKind:
    """Mười nhóm số dư ban đầu (`docs/srs/02` §1.1) — giá trị của `detail_kind`.

    Hằng số đặt tên như `BalanceNature`: giá trị đi vào cột SMALLINT và vào
    Excel nhập liệu, thứ cần là tên cho từng số. Nhóm 0–4 thi công ở phase 4
    (slice 4C), nhóm 5–9 ở phase 8 — **schema** thì nhận cả mười từ bây giờ.
    """

    ACCOUNT = 0
    """TK thường — chỉ số dư theo TK/tiền tệ."""
    BANK = 1
    """Tiền gửi theo từng tài khoản ngân hàng."""
    RECEIVABLE = 2
    """Phải thu theo khách hàng, chi tiết từng hóa đơn (FR-OPB-003)."""
    PAYABLE = 3
    """Phải trả theo nhà cung cấp, chi tiết từng hóa đơn."""
    EMPLOYEE_ADVANCE = 4
    """Tạm ứng theo nhân viên."""
    STOCK = 5
    """Tồn kho theo kho/VTHH (+ lớp FIFO — bảng chi tiết ở phase 8)."""
    WIP = 6
    """Chi phí sản xuất dở dang theo đối tượng THCP."""
    TOOL = 7
    """CCDC đang phân bổ."""
    ASSET = 8
    """TSCĐ đang khấu hao."""
    PREPAID = 9
    """Chi phí trả trước đang phân bổ."""

    PHASE_4_KINDS = frozenset({ACCOUNT, BANK, RECEIVABLE, PAYABLE, EMPLOYEE_ADVANCE})
    """Nhóm mà slice 4C nhập/kiểm được; nhóm còn lại từ chối tới phase 8."""


class OpeningBalance(DatasetBase, Audited):
    """Một dòng số dư ban đầu của một tổ hợp (năm, sổ, chi nhánh, TK, chiều).

    `Audited` vì đây là dữ liệu người dùng nhập và sửa qua nhiều vòng đối
    chiếu — "ai sửa số dư đầu năm" là câu hỏi kiểm toán thật. Không
    `RowVersioned`: đường sửa là nhập lại theo lô từ Excel (slice 4C) chứ
    không phải form từng dòng, tranh chấp giải ở mức lô.
    """

    __tablename__ = "opening_balances"
    __table_args__ = (
        CheckConstraint(
            f"ledger IN ({Ledger.FINANCIAL}, {Ledger.MANAGEMENT})", name="ledger_known"
        ),
        CheckConstraint("detail_kind BETWEEN 0 AND 9", name="detail_kind_known"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint(
            "debit >= 0 AND credit >= 0 AND debit_fc >= 0 AND credit_fc >= 0",
            name="amounts_not_negative",
        ),
        Index(
            "ix_opening_balances_key",
            "fiscal_year_id",
            "ledger",
            "branch_id",
            "account_id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    fiscal_year_id: Mapped[int] = mapped_column(
        ForeignKey("fiscal_years.id", ondelete="RESTRICT"), nullable=False
    )
    ledger: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=False
    )

    currency_code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE_DEFAULT), nullable=False, default=Decimal(1)
    )

    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    cost_object_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    contract_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expense_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    warehouse_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Trục lô/serial của bảng tồn kho (LD-09) — dùng từ phase 8, cột có từ đầu."""

    debit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    credit_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    debit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    credit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )

    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=True
    )
    unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=True
    )

    detail_kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    detail_ref_id: Mapped[UUID | None] = mapped_column(nullable=True)
    """Trỏ vào bảng chi tiết của nhóm (lớp FIFO, thẻ tài sản…) khi nhóm đó có —
    hai đích trở lên nên không FK được; dịch vụ của từng nhóm kiểm lúc ghi."""


class OpeningBalanceInvoice(DatasetBase, Audited):
    """Chi tiết chứng từ còn nợ của số dư công nợ (FR-OPB-003, SRS 02 §1.1).

    Bảng con của một dòng `opening_balances` nhóm 2/3/4: hóa đơn của phải
    thu/phải trả, và **từng lần tạm ứng** của nhóm nhân viên — SRS đòi chi tiết
    theo lần cho cả ba. Tổng các dòng con phải khớp bên còn-nợ của dòng cha
    (BR-OPB-02 — đúng theo cách dựng ở lượt nhập; lượt chuyển năm đo lệch bằng
    `invoice_overrun_parents`). Không có `branch_id` riêng — phạm vi là phạm vi
    dòng cha, cùng lối `opening_balance_id CASCADE`.
    """

    __tablename__ = "opening_balance_invoices"
    __table_args__ = (
        CheckConstraint("amount >= 0 AND amount_fc >= 0", name="amounts_not_negative"),
        CheckConstraint("paid_amount >= 0", name="paid_not_negative"),
        CheckConstraint("paid_amount <= amount", name="paid_within_amount"),
        CheckConstraint("paid_amount_fc >= 0", name="paid_fc_not_negative"),
        CheckConstraint("paid_amount_fc <= amount_fc", name="paid_fc_within_amount"),
        Index("ix_opening_balance_invoices_parent", "opening_balance_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    opening_balance_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("opening_balances.id", ondelete="CASCADE"),
        nullable=False,
    )

    invoice_no: Mapped[str | None] = mapped_column(String(50), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    amount: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    paid_amount: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
    )
    """VND đã trả, theo tỷ giá **ghi nhận nợ** của dòng cha — phần giá trị sổ
    đã giải phóng, không phải VND thực nhận (chênh hai tỷ giá đi vào 515/635,
    FR-SYS-066)."""

    paid_amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Nguyên tệ đã trả — trục của đối trừ (lát 6B): `remaining_fc = amount_fc
    - paid_amount_fc`. Không suy được từ `paid_amount` vì phép chia ngược qua
    tỷ giá không hoàn lại đúng số đã làm tròn."""
