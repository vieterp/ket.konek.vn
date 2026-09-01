"""Sổ phụ công nợ phải thu / phải trả — bảng thuộc module `receivables`.

`ar_ap_ledger` là **sổ phụ được ghi vào**, không phải hạ tầng đọc-only: nó có
chủ (module này), có đường ghi (`ArApSubledger`, ADR-021) và có đường đối trừ
(`SettlementTargetSource`). Vì thế nó ở trong `modules/`, không ở kernel —
RT-18 phân biệt đúng chỗ này.

**Vì sao có bảng riêng thay vì đọc `gl_postings`.** Đối trừ công nợ diễn ra
theo **từng hóa đơn** (FR-OPB-003, quy trình thu nợ `docs/srs/03` §4): màn thu
tiền hỏi "hóa đơn nào còn nợ bao nhiêu", và lượt ghi phải **khóa được** đúng
dòng ấy. `gl_postings` là sổ cái theo TK + chiều phân tích — nó biết "đối tác
X còn nợ 50 triệu", không biết "hóa đơn HD-017 còn nợ 3 triệu". Suy ngược từ
sổ cái đòi ghép cặp phát sinh Nợ/Có theo thời gian, và phép ghép ấy không có
lời giải duy nhất khi khách trả gộp nhiều hóa đơn.

Đây là dữ liệu **suy ra được** (derived) từ chứng từ gốc, nên nó phải chịu một
cổng đối chiếu: check toàn vẹn `arap_matches_control` so tổng còn-nợ của bảng
này với số dư TK 131/331 trên sổ cái (BR-GLE-05). Một đường ghi đi vòng qua
`ledger_service` sẽ lộ ra ở đó.

Bốn quyết định cột đáng nói:

* **`settled_fc` + `settled` đi thành cặp**, cùng lý do với
  `opening_balance_invoices.paid_amount_fc`/`paid_amount` (review 6B H-2):
  nguyên tệ là trục đối trừ, VND là phần giá trị sổ đã giải phóng, và
  `round(settled_fc × rate)` KHÔNG tái tạo được `settled` vì mỗi lượt đối trừ
  từng phần làm tròn riêng.
* **`account_id` lưu thật**, không suy từ gói cấu hình: khoản nợ có thể treo ở
  131, 331, 1388, 3388… tùy bút toán, và dòng chênh lệch tỷ giá lúc thu/trả
  phải đâm vào đúng TK đó (FR-SYS-066).
* **`branch_id` có trên chính bảng này** (không mượn dòng cha như
  `opening_balance_invoices`): sổ phụ được truy vấn thẳng bởi màn chọn đối
  trừ, thẻ công nợ, báo cáo tuổi nợ và check toàn vẹn — bốn cửa, và một cửa
  quên join bảng cha là một lỗ cô lập im lặng. Có cột thì RLS canh được
  (`p_branch_scope`), và cổng `test_rls_policy_coverage` nhìn thấy bảng.
* **`ledger`** vì hệ chạy hai sổ (ADR-006): công nợ trên sổ quản trị có thể
  khác sổ tài chính, và một sổ phụ không mang chiều sổ sẽ trộn hai sổ vào một
  con số ngay lần đầu ai đó ghi bút toán chỉ-quản-trị.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Computed,
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
from ket.kernel.identifiers import uuid7
from ket.kernel.persistence.base import DatasetBase
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

DOCUMENT_NO_MAX_LENGTH = 50
DESCRIPTION_MAX_LENGTH = 500
CURRENCY_CODE_LENGTH = 3

EXCHANGE_RATE_PRECISION = 18
EXCHANGE_RATE_SCALE = 6


class ArApLedgerEntry(DatasetBase, Audited):
    """Một khoản công nợ còn theo dõi theo từng chứng từ gốc.

    Nguồn của dòng là **một trong hai**, loại trừ nhau và cả hai đều có thể
    rỗng ở đúng một chiều:

    * `document_id` — chứng từ mua/bán của phase 7 (`vouchers.id`). Xóa chứng
      từ thì dòng đi theo (`ON DELETE CASCADE`): sổ phụ không được sống lâu
      hơn thứ sinh ra nó.
    * `opening_invoice_id` — hóa đơn số dư đầu kỳ (4C). Dòng loại này **không
      do module này ghi**; nó là chỗ để lượt chuyển năm sau này gộp hai nguồn
      về một mặt phẳng nếu cần. Trước khi có đường ấy, cột đứng rỗng — khai
      sẵn vì thêm cột vào một bảng sổ đã có dữ liệu thật thì đắt hơn nhiều.

    `is_closed` là **cột sinh** (`GENERATED ALWAYS AS ... STORED`) chứ không
    phải cờ do ứng dụng ghi: một cờ do ứng dụng ghi sẽ lệch với `settled` ngay
    lần đầu có đường ghi quên cập nhật nó, và chỉ số bán phần `ix_arap_open`
    (thứ mà mọi màn chọn đối trừ đi qua) dựa vào nó.
    """

    __tablename__ = "ar_ap_ledger"
    __table_args__ = (
        CheckConstraint("partner_kind BETWEEN 0 AND 2", name="partner_kind_known"),
        CheckConstraint("target_kind BETWEEN 0 AND 2", name="target_kind_known"),
        CheckConstraint("ledger BETWEEN 0 AND 1", name="ledger_known"),
        CheckConstraint("amount >= 0 AND amount_fc >= 0", name="amounts_not_negative"),
        CheckConstraint("settled >= 0", name="settled_not_negative"),
        CheckConstraint("settled_fc >= 0", name="settled_fc_not_negative"),
        # Chặn chót của RT-16 chống đối trừ vượt: `ledger_service.apply` kiểm
        # trước và trả 422; nếu một đường ghi nào đó lọt qua nó thì DB nổ
        # IntegrityError chứ không lặng lẽ ghi một khoản nợ âm.
        CheckConstraint("settled <= amount", name="settled_within_amount"),
        CheckConstraint("settled_fc <= amount_fc", name="settled_fc_within_amount"),
        CheckConstraint(
            "document_id IS NOT NULL OR opening_invoice_id IS NOT NULL",
            name="has_a_source_document",
        ),
        # `branch_id` DẪN ĐẦU, không phải đuôi: mọi cửa đọc bảng này lọc chi
        # nhánh trước tiên — chính vị từ RLS `p_branch_scope`, hai dataset báo
        # cáo tuổi nợ/dự báo, và check toàn vẹn `settlement_matches_subledger`
        # (chạy mọi lượt job). Đặt `partner_kind` lên đầu thì cả ba rơi về seq
        # scan trên một bảng mọc theo số hóa đơn. Thứ tự này phục vụ CẢ HAI
        # hình dạng: tiền tố `branch_id` cho ba cửa trên, khớp đủ ba cột cho
        # màn chọn đối trừ.
        Index(
            "ix_arap_open",
            "branch_id",
            "partner_kind",
            "partner_id",
            postgresql_where=text("is_closed = FALSE"),
        ),
        Index("ix_ar_ap_ledger_document", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    """UUID chứ không BIGSERIAL như phác thảo trong `phase-07` §Architecture:
    dòng này là **đích đối trừ**, và `SettlementTargetSource.find/apply` của
    kernel (đóng băng từ phase 6) cầm `target_id: UUID`. Một khóa bigint ở đây
    thì `cash_settlements.target_id` không trỏ tới được — nghĩa là bảng mất
    đúng công dụng mà nó sinh ra để làm."""

    target_kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """`kernel.protocols.SettlementTargetKind` — phân hệ chủ của khoản nợ, và
    là thứ quyết định chiều: hóa đơn bán ⇒ phải thu, hóa đơn mua ⇒ phải trả.
    Cột này cũng là nửa còn lại của cặp `(target_kind, target_id)` mà dòng đối
    trừ ở `cash_settlements`/`bank_settlements` cầm."""

    partner_kind: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """`kernel.contracts.PartnerKind` — 0 khách hàng, 1 nhà cung cấp, 2 nhân viên."""

    partner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    """TK công nợ khoản này đang treo (131/331/1388…) — lấy từ chính bút toán
    đã dựng, không đoán theo gói cấu hình."""

    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("vouchers.id", ondelete="CASCADE"), nullable=True
    )
    opening_invoice_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("opening_balance_invoices.id", ondelete="CASCADE"), nullable=True
    )

    document_no: Mapped[str] = mapped_column(String(DOCUMENT_NO_MAX_LENGTH), nullable=False)
    document_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    currency_code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(EXCHANGE_RATE_PRECISION, EXCHANGE_RATE_SCALE), nullable=False
    )
    """Tỷ giá lúc **ghi nhận nợ** — mốc để tính chênh lệch tỷ giá khi thu/trả."""

    amount_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False)
    settled_fc: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )
    settled: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE),
        nullable=False,
        default=Decimal(0),
        server_default=text("0"),
    )

    is_closed: Mapped[bool] = mapped_column(
        # Cột sinh: PostgreSQL giữ đúng theo `settled`, ứng dụng chỉ đọc.
        # `>=` chứ không `=`: khoản 0 đồng (hóa đơn điều chỉnh về 0) phải đóng
        # ngay thay vì treo mãi trong danh sách còn-nợ.
        Computed("settled >= amount", persisted=True),
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(String(DESCRIPTION_MAX_LENGTH), nullable=True)
