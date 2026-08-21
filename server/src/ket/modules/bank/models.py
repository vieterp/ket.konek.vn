"""Chi tiết chứng từ tiền gửi + sao kê ngân hàng (SRS 04) — bảng thuộc module.

Cùng khuôn `cash_book/models.py`: thân chứng từ một-một với header `vouchers`,
dòng định khoản dạng cặp Nợ/Có + nguyên tệ, posting engine không đọc.

Về đối chiếu (`docs/srs/04` §4.3): trạng thái khớp sống ở **một** chỗ —
`bank_statement_lines.matched_voucher_id` + `match_kind`. Phase file phác thêm
`reconciled_at`/`statement_line_id` trên `bank_vouchers`; bỏ có chủ đích: hai
bảng cùng ghi một sự thật là hai bảng sẽ lệch, và "chứng từ này đã khớp chưa"
trả lời được bằng một `EXISTS` trên bảng dòng sao kê (có index).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.config.auto_posting_models import OPERATION_CODE_MAX_LENGTH
from ket.kernel.contracts import PartnerKind
from ket.kernel.identifiers import uuid7
from ket.kernel.master_data.models.employee import BANK_ACCOUNT_MAX_LENGTH
from ket.kernel.persistence.base import DatasetBase
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

DESCRIPTION_MAX_LENGTH = 500
"""Cùng trần với dòng phiếu quỹ và dòng GLE — không import từ `cash_book`:
module không import module (luật phụ thuộc #1)."""

BENEFICIARY_NAME_MAX_LENGTH = 255
BANK_NAME_MAX_LENGTH = 255
REFERENCE_NO_MAX_LENGTH = 100
CHEQUE_NO_MAX_LENGTH = 50


class BankVoucherKind:
    """`bank_vouchers.kind` — bốn loại chứng từ của SRS 04 §2."""

    CREDIT_ADVICE = 0
    """Thu tiền gửi / giấy báo có (BC)."""
    PAYMENT_ORDER = 1
    """Chi tiền gửi / ủy nhiệm chi (UNC)."""
    CHEQUE = 2
    """Séc chuyển khoản / séc tiền mặt."""
    INTERNAL_TRANSFER = 3
    """Chuyển tiền nội bộ giữa hai TK ngân hàng cùng đơn vị."""


class StatementMatchKind:
    """`bank_statement_lines.match_kind` (`docs/srs/04` §4.3, U5)."""

    AUTO = 0
    MANUAL = 1
    UNMATCHED = 2


class BankVoucher(DatasetBase, Audited):
    """Phần thân chứng từ tiền gửi — một-một với header `vouchers`."""

    __tablename__ = "bank_vouchers"
    __table_args__ = (
        CheckConstraint(
            f"kind BETWEEN {BankVoucherKind.CREDIT_ADVICE} AND {BankVoucherKind.INTERNAL_TRANSFER}",
            name="kind_known",
        ),
        CheckConstraint("operation_code <> ''", name="operation_code_not_blank"),
        # Chuyển nội bộ không có "nghiệp vụ" (định khoản là 112↔112 cố định)
        # nhưng bắt buộc có TK đích; ba loại kia thì ngược lại.
        CheckConstraint(
            f"(kind = {BankVoucherKind.INTERNAL_TRANSFER}) = (operation_code IS NULL)",
            name="operation_iff_not_transfer",
        ),
        CheckConstraint(
            f"(kind = {BankVoucherKind.INTERNAL_TRANSFER}) = (counter_bank_account_id IS NOT NULL)",
            name="counter_account_iff_transfer",
        ),
        CheckConstraint(
            f"kind = {BankVoucherKind.CHEQUE} OR cheque_no IS NULL",
            name="cheque_fields_only_on_cheque",
        ),
        CheckConstraint("(cheque_no IS NULL) = (cheque_date IS NULL)", name="cheque_pair_complete"),
        CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)", name="partner_pair_complete"
        ),
        CheckConstraint(
            f"partner_kind IS NULL OR partner_kind BETWEEN {PartnerKind.CUSTOMER} "
            f"AND {PartnerKind.EMPLOYEE}",
            name="partner_kind_known",
        ),
        Index("ix_bank_vouchers_bank_account", "bank_account_id"),
        Index("ix_bank_vouchers_reference_no", "reference_no"),
    )

    id: Mapped[UUID] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    operation_code: Mapped[str | None] = mapped_column(
        String(OPERATION_CODE_MAX_LENGTH), nullable=True
    )
    """Nghiệp vụ FR-SYS-025; `NULL` đúng một trường hợp: chuyển tiền nội bộ."""

    bank_account_id: Mapped[int] = mapped_column(
        ForeignKey("company_bank_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    """FR-BNK-002: mọi chứng từ tiền gửi gắn TK ngân hàng cụ thể, không chỉ 112."""

    counter_bank_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("company_bank_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    """TK **đích** khi chuyển nội bộ (`bank_account_id` là TK nguồn)."""

    partner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    partner_kind: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    beneficiary_name: Mapped[str | None] = mapped_column(
        String(BENEFICIARY_NAME_MAX_LENGTH), nullable=True
    )
    beneficiary_account_no: Mapped[str | None] = mapped_column(
        String(BANK_ACCOUNT_MAX_LENGTH), nullable=True
    )
    beneficiary_bank_name: Mapped[str | None] = mapped_column(
        String(BANK_NAME_MAX_LENGTH), nullable=True
    )
    """Bộ ba người thụ hưởng trên ủy nhiệm chi — **chụp lại thành chữ** lúc lập
    (điền sẵn từ `partner_bank_accounts`, FR-BNK-003/FR-SYS-033) chứ không FK:
    UNC năm ngoái phải in ra đúng những gì đã gửi ngân hàng năm ngoái, kể cả
    khi danh mục tài khoản của đối tác đã đổi."""

    cheque_no: Mapped[str | None] = mapped_column(String(CHEQUE_NO_MAX_LENGTH), nullable=True)
    cheque_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    reference_no: Mapped[str | None] = mapped_column(String(REFERENCE_NO_MAX_LENGTH), nullable=True)
    """Số tham chiếu giao dịch ngân hàng — một trong ba khóa khớp tự động của
    đối chiếu (số tiền, ngày ±3, reference)."""


class BankVoucherLine(DatasetBase, Audited):
    """Một dòng định khoản của chứng từ tiền gửi — cùng hình dạng dòng phiếu quỹ.

    Bảng riêng chứ không dùng chung bảng với `cash_voucher_lines` (phase file
    phác `LIKE ... INCLUDING ALL`): khóa ngoại `voucher_id` phải trỏ đúng bảng
    thân của module mình, và một bảng dòng dùng chung là một khóa ngoại đa hình
    — thứ mà `cash_settlements` đã phải giải thích vì sao bất đắc dĩ mới dùng.
    """

    __tablename__ = "bank_voucher_lines"
    __table_args__ = (
        CheckConstraint("amount_fc >= 0", name="amount_not_negative"),
        CheckConstraint(
            "(partner_id IS NULL) = (partner_kind IS NULL)", name="partner_pair_complete"
        ),
        CheckConstraint(
            f"partner_kind IS NULL OR partner_kind BETWEEN {PartnerKind.CUSTOMER} "
            f"AND {PartnerKind.EMPLOYEE}",
            name="partner_kind_known",
        ),
        Index("ix_bank_voucher_lines_voucher", "voucher_id", "line_no"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    voucher_id: Mapped[UUID] = mapped_column(
        ForeignKey("bank_vouchers.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)

    debit_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    credit_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("chart_of_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False, default=Decimal(0)
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

    extended_dimensions: Mapped[dict[str, int] | None] = mapped_column(JSONB, nullable=True)


class BankStatement(DatasetBase, Audited):
    """Một sao kê đã nhập cho một TK ngân hàng (`docs/srs/04` §4.2, RT-26)."""

    __tablename__ = "bank_statements"
    __table_args__ = (
        Index("ix_bank_statements_account_date", "bank_account_id", "statement_date"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    bank_account_id: Mapped[int] = mapped_column(
        ForeignKey("company_bank_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    statement_date: Mapped[date] = mapped_column(Date, nullable=False)

    opening_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=True
    )
    closing_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=True
    )
    """Hai số dư `NULL` được: nhiều định dạng export ngân hàng không mang số
    dư, và bịa số 0 sẽ làm báo cáo đối chiếu "lệch" một cách bịa đặt."""

    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_statement_profiles.id", ondelete="RESTRICT"), nullable=True
    )
    """Profile per-bank đã dùng để đọc tệp (RT-26); `NULL` = nhập tay."""

    imported_by: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BankStatementLine(DatasetBase, Audited):
    """Một dòng giao dịch trên sao kê + trạng thái khớp của nó (U5)."""

    __tablename__ = "bank_statement_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts_not_negative"),
        CheckConstraint(
            f"match_kind BETWEEN {StatementMatchKind.AUTO} AND {StatementMatchKind.UNMATCHED}",
            name="match_kind_known",
        ),
        # Trạng thái và tham chiếu khớp sống chết cùng nhau: "đã khớp" phải chỉ
        # được vào một chứng từ, "chưa khớp" thì không được giữ tham chiếu mồ côi.
        CheckConstraint(
            f"(match_kind = {StatementMatchKind.UNMATCHED}) = (matched_voucher_id IS NULL)",
            name="match_pair_complete",
        ),
        Index("ix_bank_statement_lines_statement", "statement_id", "line_no"),
        Index("ix_bank_statement_lines_matched_voucher", "matched_voucher_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    statement_id: Mapped[UUID] = mapped_column(
        ForeignKey("bank_statements.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    """Thứ tự dòng trong tệp gốc — sao kê 500 dòng phải hiện đúng thứ tự ngân
    hàng in, kể cả khi nhiều dòng cùng ngày."""

    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    reference_no: Mapped[str | None] = mapped_column(String(REFERENCE_NO_MAX_LENGTH), nullable=True)
    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)

    debit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Tiền **ra** khỏi tài khoản, theo góc nhìn sao kê ngân hàng."""

    credit: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    """Tiền **vào** tài khoản."""

    matched_voucher_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=True
    )
    """`RESTRICT`: chứng từ đã khớp sao kê không xóa được — muốn xóa phải gỡ
    khớp trước, và thao tác gỡ có nhật ký."""

    match_kind: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=StatementMatchKind.UNMATCHED, server_default="2"
    )
