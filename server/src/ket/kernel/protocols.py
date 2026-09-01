"""Protocol liên-module — khai TRỌN BỘ ở phase 6 để đóng băng kernel (RT-18).

Luật phụ thuộc #2 (plan §Bố cục): module cần dữ liệu của module khác thì đi qua
Protocol đặt trong kernel, được đăng ký lúc khởi động — không truy vấn thẳng
bảng của module khác. Tệp này là chỗ đặt **tập trung** các Protocol đó, vì lý
do RT-18 nêu thẳng: phase 7 và 8 chạy song song sau phase 6, và ranh giới chia
sẻ duy nhất giữa chúng là kernel + posting đã đóng băng. Protocol khai muộn ở
phase 7/8 là một lần mở kernel ra sửa — đúng thứ "đóng băng" cấm.

Sáu Protocol, ai cài — ai gọi:

* `ReceivableProvider` / `PayableProvider` — nguồn "hóa đơn còn nợ" cho đối
  trừ công nợ khi thu/chi tiền (`docs/srs/03` §4). Phase 6: nguồn duy nhất là
  số dư đầu kỳ (`posting.opening_balances` cài lúc 6B). Phase 7: module
  `sales`/`purchase` (qua `receivables`) đăng ký thêm nguồn hóa đơn thật.
  Người gọi: `cash_book.settlement_service`, và phase 7 là chính màn công nợ.
* `SettlementTargetSource` — chủ dữ liệu của một loại đích đối trừ: tra đích
  theo id lúc ghi sổ và cộng/gỡ số đã trả. Phase 6: `OPENING_BALANCE` do
  `posting.opening_balances` cài; phase 7 cài nốt hai loại hóa đơn.
* `InventoryPosting` — chứng từ mua/bán (phase 7) sinh phiếu nhập/xuất kho mà
  không import module kho; module `inventory` (phase 8) cài.
* `CommitmentProvider` — "đã hứa giao" cho cột **Có thể bán** = tồn − đã hứa
  (U7, phase 8); module `sales` cài từ đơn hàng.
* `ArApSubledger` (thêm ở lát 7A, ADR-021) — chiều GHI sổ phụ công nợ: chứng
  từ mua/bán sinh và gỡ khoản nợ mà không import module `receivables` (cài).
  Ba Protocol công nợ ở trên phủ chiều đọc và chiều đối trừ; lượt khai trước ở
  phase 6 sót chiều này, và nó lộ ra ngay khi bắt tay lát 7A.
* `TreasurerCashBook` / `TreasurerVoucherSource` (lát 6C) — cặp hai chiều giữa
  `cash_book` (chủ trạng thái thủ quỹ trên thân phiếu) và `warehousing` (chủ
  bảng sổ quỹ): hàng đợi thủ quỹ đọc phiếu chờ qua source, còn đường
  "phân hệ tắt thì ghi thẳng sổ quỹ" (FR-WHK-021) ghi qua cash-book Protocol.
  Phase 8 lặp lại đúng khuôn này cho thủ kho (nguồn phiếu nhập/xuất).

**KHÔNG** khai `InventoryValuation` — RT-18 xóa vì không có consumer thật.

Registry ở cuối tệp theo đúng khuôn `posting.documents.registry`: module đăng
ký bản cài lúc import (qua `ket.model_registry`), nơi gọi chỉ biết Protocol.
Trạng thái "chưa ai cài" là hợp lệ và **rỗng chứ không giả**: danh sách nguồn
công nợ rỗng nghĩa là chưa có hóa đơn nào — đúng sự thật của một bản cài chưa
có phase 7 — thay vì một bản no-op trả dữ liệu bịa.

Chữ ký ở đây là **bản nháp có chủ đích** cho tới bước "đóng băng" cuối phase 6
(bước 23): 6B–6F là người dùng thật đầu tiên của chúng, và chữ ký chỉ chốt sau
khi có người gọi thật. Sau bước 23, đổi chữ ký cần ADR bổ sung.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import IntEnum
from typing import Final, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind

# --------------------------------------------------------------- công nợ mở


__all__ = [
    "PROVIDERS",
    "ArApSubledger",
    "CommitmentProvider",
    "CrossModuleProviders",
    "InventoryMovementKind",
    "InventoryMovementLine",
    "InventoryPosting",
    "OpenInvoice",
    "PayableProvider",
    "ReceivableProvider",
    "SettlementTargetKind",
    "SettlementTargetSource",
    "SubledgerEntry",
    "TreasurerBookEntry",
    "TreasurerCashBook",
    "TreasurerPendingVoucher",
    "TreasurerVoucherSource",
]
"""Bề mặt liên-module ĐÃ KHAI — cũng chính là thứ bị đóng băng ở bước 23.

Khai tường minh chứ không để mặc định "mọi tên công khai": không có nó thì
`BaseModel`, `Session`, `Decimal`… (tên import lại) cũng thành một phần API, và
ảnh chụp đóng băng sẽ đỏ mỗi lần pydantic lên phiên bản — một cổng kêu vì lý do
không ai quan tâm là một cổng người ta học cách bỏ qua.
"""


class SettlementTargetKind(IntEnum):
    """Loại chứng từ công nợ mà một lượt đối trừ trỏ tới.

    Giá trị đi thẳng vào `cash_settlements.target_kind` và vào mọi bảng đối
    trừ sau này (phase 7 `ar_ap_ledger`), nên khai ở kernel chứ không trong
    module: hai module ghi cùng một cột thì từ vựng phải có đúng một chỗ.
    """

    SALES_INVOICE = 0
    """Chứng từ bán hàng (module `sales`, phase 7)."""

    PURCHASE_INVOICE = 1
    """Chứng từ mua hàng (module `purchase`, phase 7)."""

    OPENING_BALANCE = 2
    """Hóa đơn số dư đầu kỳ (`opening_balance_invoices`, phase 4C)."""


class OpenInvoice(BaseModel):
    """Một chứng từ công nợ còn nợ, đã quy về hình dạng chung cho mọi nguồn.

    `remaining_fc` là số **nguyên tệ** còn nợ — đối trừ nhập theo nguyên tệ,
    còn chênh lệch tỷ giá thu/trả tiền (FR-SYS-066) tính từ `exchange_rate`
    (tỷ giá lúc ghi nhận nợ) so với tỷ giá của phiếu thu/chi tại thời điểm
    thanh toán.

    `remaining` là số **VND còn treo trên sổ** (giá trị ghi nhận − đã giải
    phóng). Trường này tồn tại vì `round(remaining_fc × rate)` KHÔNG tái tạo
    được nó: mỗi lượt đối trừ từng phần làm tròn riêng, và tổng các phần làm
    tròn có thể vượt tổng-làm-tròn-một-lần vài đồng lẻ (review 6B, H-2) — số
    VND của sổ chỉ nguồn dữ liệu mới biết, người tiêu dùng không được tự nhân.

    `account_id` là TK công nợ mà chứng từ này đang treo (131/331/1388… theo
    dòng số dư hoặc hóa đơn gốc): dòng điều chỉnh chênh lệch tỷ giá phải đâm
    vào **đúng TK đó** — đoán "131 của gói hiện hành" sẽ sai ngay khi số dư
    treo ở TK chi tiết khác.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_kind: SettlementTargetKind
    target_id: UUID
    partner_kind: PartnerKind
    partner_id: int
    branch_id: int
    account_id: int
    invoice_no: str
    invoice_date: date
    due_date: date | None = None
    currency_code: str
    exchange_rate: Decimal
    amount_fc: Decimal
    remaining_fc: Decimal
    remaining: Decimal
    description: str | None = None


class ReceivableProvider(Protocol):
    """Một nguồn hóa đơn **phải thu** còn nợ (khách hàng trả tiền — Có 131)."""

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        """Hóa đơn còn nợ của một đối tác tính đến hết ngày `as_of`."""
        ...


class PayableProvider(Protocol):
    """Một nguồn hóa đơn **phải trả** còn nợ (trả nhà cung cấp — Nợ 331).

    Cùng chữ ký với `ReceivableProvider` nhưng là Protocol riêng: hai chiều
    công nợ do hai nhóm module cài (bán vs mua), và một bên gọi nhầm registry
    của bên kia phải là lỗi kiểu, không phải lỗi dữ liệu chỉ lộ trên thẻ công nợ.
    """

    def open_invoices(
        self,
        session: Session,
        *,
        partner_kind: PartnerKind,
        partner_id: int,
        branch_id: int,
        as_of: date,
    ) -> Sequence[OpenInvoice]:
        """Hóa đơn còn nợ của một đối tác tính đến hết ngày `as_of`."""
        ...


class SettlementTargetSource(Protocol):
    """Chủ dữ liệu của MỘT loại đích đối trừ — tra theo id, ghi/gỡ số đã trả.

    Khác hai provider trên (liệt kê "còn nợ gì" để chọn), source trả lời cho
    chứng từ thu/chi **đã chọn xong**: dòng `cash_settlements` chỉ cầm cặp
    `(target_kind, target_id)`, và lúc ghi sổ phải tra lại đích để biết TK công
    nợ + tỷ giá ghi nhận, rồi cộng số đã trả vào đích trong CÙNG transaction.
    Mỗi `SettlementTargetKind` đúng một source: hai bản cài cho một loại đích
    là hai nơi tranh nhau một cột `paid`, cùng luật với `InventoryPosting`.

    `apply`/`revert` nhận cả `amount_fc` lẫn `amount` (VND theo tỷ giá **ghi
    nhận nợ** — người gọi tính, một chỗ làm tròn duy nhất) và phải tự khóa dòng
    đích (`FOR UPDATE`) + từ chối khi vượt số còn nợ: hai phiếu cùng đối trừ
    một hóa đơn là chuyện thường ngày (BR-QUY-02 kiểm lần cuối ở đây).
    """

    def find(self, session: Session, *, target_ids: Sequence[UUID]) -> Sequence[OpenInvoice]:
        """Tra các đích theo id — đích đã biến mất (nhập lại số dư…) thì vắng
        mặt trong kết quả, người gọi coi đó là vi phạm chứ không phải lỗi 500."""
        ...

    def apply(
        self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
    ) -> None:
        """Cộng số đã trả vào đích khi chứng từ ghi sổ."""
        ...

    def revert(
        self, session: Session, *, target_id: UUID, amount_fc: Decimal, amount: Decimal
    ) -> None:
        """Gỡ số đã trả khi chứng từ bỏ ghi sổ."""
        ...


class SubledgerEntry(BaseModel):
    """Một khoản công nợ sắp ghi vào sổ phụ, nhìn từ phía chứng từ gốc.

    Khác `OpenInvoice` ở chiều đi: `OpenInvoice` là thứ sổ phụ **trả ra** cho
    màn chọn đối trừ (đã có phần đã trả), còn đây là thứ chứng từ mua/bán
    **đưa vào** lúc ghi sổ — chưa ai trả đồng nào, nên không có `remaining`.
    Hai hình dạng không gộp được: gộp lại thì người gọi phải điền hai trường
    vô nghĩa và người đọc phải đoán trường nào có ý nghĩa ở chiều nào.

    `amount_fc` (nguyên tệ) và `amount` (VND theo `exchange_rate`) đi thành
    cặp vì cùng lý do với `paid_amount_fc`/`paid_amount` (xem `OpenInvoice`):
    nhân lại từ nguyên tệ là làm tròn lần thứ hai, và hai lần làm tròn là hai
    con số. Người gọi làm tròn đúng một lần, ở chỗ nó dựng bút toán.

    `account_id` là TK công nợ mà khoản này treo lên (131/331/1388…) — lấy từ
    chính bút toán vừa dựng, không đoán theo gói cấu hình: dòng chênh lệch tỷ
    giá lúc thu/trả phải đâm vào đúng TK đó (FR-SYS-066).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_kind: SettlementTargetKind
    """Phân hệ nào sinh ra khoản này — cũng là thứ quyết định **chiều** công
    nợ (hóa đơn bán ⇒ phải thu, hóa đơn mua ⇒ phải trả). Suy chiều từ
    `partner_kind` thì sai ngay ở nhân viên: một người vừa có thể được ứng
    tiền vừa có thể nợ công ty."""

    partner_kind: PartnerKind
    partner_id: int
    ledger: int = Field(ge=0, le=1)
    account_id: int
    document_no: str = Field(max_length=50)
    document_date: date
    due_date: date | None = None
    currency_code: str = Field(min_length=3, max_length=3)
    exchange_rate: Decimal
    amount_fc: Decimal = Field(ge=0)
    amount: Decimal = Field(ge=0)
    description: str | None = Field(default=None, max_length=500)

    # KHÔNG có `branch_id`: chi nhánh của khoản nợ là chi nhánh của CHỨNG TỪ,
    # và bản cài đọc nó từ đó. Để người gọi truyền vào thì có một trạng thái
    # biểu diễn được mà hệ thống không bao giờ đúng với nó — dòng sổ phụ ở chi
    # nhánh B dưới chứng từ chi nhánh A. Nó không đối chiếu được với sổ cái
    # (`gl_postings.branch_id` LUÔN lấy từ `vouchers.branch_id`, xem
    # `posting/engine/service.py`), và nó vô hình với chính lượt bỏ ghi sổ của
    # chứng từ — guard "đã đối trừ thì không xóa" chạy dưới RLS người gọi sẽ
    # im lặng cho qua rồi xóa nửa vời. Bỏ trường đi thì trạng thái ấy không
    # tồn tại để phải kiểm.


class ArApSubledger(Protocol):
    """Cửa duy nhất để chứng từ mua/bán ghi sổ phụ công nợ (ADR-021).

    Đối xứng với `InventoryPosting`: phase 7 mua/bán **gọi**, module
    `receivables` **cài**. Ba Protocol công nợ phía trên phủ chiều ĐỌC (còn nợ
    gì) và chiều ĐỐI TRỪ (vừa trả vào đâu); Protocol này phủ chiều GHI — sinh
    và gỡ chính dòng sổ phụ lúc chứng từ gốc ghi sổ / bỏ ghi sổ. Thiếu nó thì
    `purchase` phải import `receivables`, và C3 cấm.
    """

    def record(
        self, session: Session, *, voucher_id: UUID, entries: Sequence[SubledgerEntry]
    ) -> None:
        """Ghi các khoản công nợ của một chứng từ vừa ghi sổ.

        **Thay trọn theo `voucher_id`**, không cộng dồn (ADR-021): ghi sổ → bỏ
        ghi sổ → sửa → ghi sổ lại là đường đi thường ngày, và một bản cài cộng
        dồn sẽ nhân đôi công nợ ở lượt thứ hai — hỏng âm thầm, chỉ lộ ra ở số
        dư 131/331 nhiều kỳ sau.
        """
        ...

    def remove(self, session: Session, *, voucher_id: UUID) -> None:
        """Gỡ các khoản công nợ khi chứng từ bỏ ghi sổ.

        Từ chối (ném `PostingValidationError`) khi có dòng đã bị đối trừ một
        phần: xóa nó đi là bỏ lại phiếu thu/chi trỏ vào hư không.
        """
        ...


# ----------------------------------------------------------------- kho vận


class InventoryMovementKind(IntEnum):
    """Chiều của một phiếu kho sinh từ chứng từ mua/bán."""

    RECEIPT = 0
    """Nhập kho (mua hàng về kho)."""

    ISSUE = 1
    """Xuất kho (bán hàng giao từ kho)."""


class InventoryMovementLine(BaseModel):
    """Một dòng vật tư trên phiếu nhập/xuất sinh từ chứng từ mua/bán.

    `unit_price_fc` chỉ có nghĩa ở chiều **nhập** (giá vốn nhập theo chứng từ
    mua); chiều xuất để `None` — giá xuất do engine tính giá của module kho
    quyết định (4 phương pháp, phase 8), không phải chứng từ bán áp xuống.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: int
    item_variant_id: int | None = None
    warehouse_id: int
    lot_no: str | None = None
    quantity: Decimal
    unit_id: int
    unit_price_fc: Decimal | None = None


class InventoryPosting(Protocol):
    """Cửa duy nhất để chứng từ mua/bán tạo phiếu kho (phase 7 gọi, phase 8 cài)."""

    def create_movement(
        self,
        session: Session,
        *,
        kind: InventoryMovementKind,
        source_voucher_id: UUID,
        branch_id: int,
        posting_date: date,
        currency_code: str,
        exchange_rate: Decimal,
        lines: Sequence[InventoryMovementLine],
        user_id: int,
    ) -> UUID:
        """Tạo một phiếu kho gắn với chứng từ nguồn; trả `voucher_id` của phiếu."""
        ...


class CommitmentProvider(Protocol):
    """Số lượng "đã hứa giao" cho cột **Có thể bán** = tồn − đã hứa (U7)."""

    def committed_quantities(
        self,
        session: Session,
        *,
        item_ids: Sequence[int],
        branch_id: int,
        warehouse_id: int | None = None,
    ) -> Mapping[int, Decimal]:
        """Tổng đã hứa giao theo `item_id` — dạng batch vì lưới tồn kho hỏi
        hàng trăm mã một lượt, và N lời gọi lẻ là N truy vấn trên bảng đơn hàng."""
        ...


# ------------------------------------------------------- tiền gửi (BNK, 6D)


# ------------------------------------------------------------- thủ quỹ (WHK)


class TreasurerBookEntry(BaseModel):
    """Một dòng sổ quỹ sắp ghi — hình dạng chung giữa nguồn phiếu và sổ.

    `receipt_amount`/`payment_amount` là **VND quy đổi** (sổ quỹ đối chiếu với
    số dư TK 111 trên sổ kế toán — BR-WHK-03 — nên phải cùng đơn vị với sổ
    cái); đúng một bên > 0, bên kia bằng 0 (CHECK `exactly_one_side` của bảng).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    voucher_id: UUID
    branch_id: int
    cash_account_id: int
    book_date: date
    receipt_amount: Decimal
    payment_amount: Decimal
    posted_by: int


class TreasurerPendingVoucher(BaseModel):
    """Một phiếu chờ thủ quỹ ghi sổ quỹ — hàng đợi FR-WHK-001."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    voucher_id: UUID
    voucher_no: str
    document_type: str
    branch_id: int
    posting_date: date
    cash_account_id: int
    is_receipt: bool
    amount: Decimal
    """VND quy đổi — số thủ quỹ sẽ thu/chi thật."""

    payer_receiver_name: str | None = None
    description: str | None = None


class TreasurerCashBook(Protocol):
    """Sổ quỹ của thủ quỹ (bảng `treasurer_cash_book`, module `warehousing`).

    Người gọi phía `cash_book`: khi phân hệ thủ quỹ **tắt** (FR-WHK-021) phiếu
    ghi sổ kế toán xong phải vào thẳng sổ quỹ — mà bảng sổ quỹ thuộc module
    khác, nên đi qua Protocol này thay vì import chéo (luật phụ thuộc #1).
    """

    def record(self, session: Session, entry: TreasurerBookEntry) -> None:
        """Ghi một dòng sổ quỹ — trùng `voucher_id` phải ném, không ghi đôi."""
        ...

    def erase(self, session: Session, *, voucher_id: UUID) -> None:
        """Gỡ dòng sổ quỹ của một phiếu (khi bỏ ghi sổ kế toán) — không có
        dòng nào thì thôi, phiếu chưa từng lên sổ quỹ là trạng thái hợp lệ."""
        ...


class TreasurerVoucherSource(Protocol):
    """Nguồn phiếu cho hàng đợi thủ quỹ — module `cash_book` cài.

    Chiều ngược của `TreasurerCashBook`: hàng đợi và hành động Ghi sổ quỹ nằm ở
    `warehousing` (SRS 17), nhưng trạng thái thủ quỹ (`treasurer_status`) sống
    trên thân phiếu của `cash_book` — nguồn chịu trách nhiệm kiểm và lật trạng
    thái, sổ chịu trách nhiệm ghi dòng.
    """

    def pending(self, session: Session) -> Sequence[TreasurerPendingVoucher]:
        """Phiếu đã ghi sổ kế toán, chờ thủ quỹ (`treasurer_status` = chờ)."""
        ...

    def book(
        self,
        session: Session,
        *,
        voucher_id: UUID,
        book_date: date | None,
        user_id: int,
        today: date,
    ) -> TreasurerBookEntry:
        """Kiểm (đã ghi sổ, đang chờ, BR-WHK-05, ngày ghi sổ không ở tương lai)
        + lật trạng thái đã-ghi-sổ-quỹ, trả dữ liệu dòng sổ quỹ để bên sổ ghi.
        `book_date=None` = theo ngày hạch toán trên chứng từ (FR-WHK-003).

        `today` do hàng đợi cấp MỘT lần cho cả lô (đồng hồ thuộc về nơi điều
        phối; lô bắc qua nửa đêm không được nửa đậu nửa rớt) — sổ quỹ ghi việc
        ĐÃ làm nên ngày ghi sổ hiệu lực không được vượt quá nó (quyết định user
        2026-08-27)."""
        ...


# ---------------------------------------------------------------- registry


class CrossModuleProviders:
    """Sổ đăng ký bản cài của một tiến trình — đối tượng để test dựng registry riêng.

    Công nợ và "đã hứa giao" là **danh sách**: số dư đầu kỳ, bán hàng, mua hàng
    cùng là nguồn hợp lệ và kết quả là phép nối. `InventoryPosting` và
    `ArApSubledger` là **một**: chỉ module kho ghi sổ kho, chỉ module công nợ
    ghi sổ phụ công nợ, hai bản cài là hai nơi tranh nhau một bảng — đăng ký
    trùng ném ngay, cùng luật với `PostingDocumentRegistry`.

    Chiều ĐỌC là danh sách còn chiều GHI là một: nhiều phân hệ được phép *kể*
    về công nợ của chúng, nhưng chỉ một phân hệ được *giữ* bảng.
    """

    def __init__(self) -> None:
        self._receivable: list[ReceivableProvider] = []
        self._payable: list[PayableProvider] = []
        self._commitment: list[CommitmentProvider] = []
        self._inventory: InventoryPosting | None = None
        self._ar_ap_subledger: ArApSubledger | None = None
        self._settlement_sources: dict[SettlementTargetKind, SettlementTargetSource] = {}
        self._treasurer_cash_book: TreasurerCashBook | None = None
        self._treasurer_voucher_source: TreasurerVoucherSource | None = None

    def register_receivable(self, provider: ReceivableProvider) -> None:
        self._receivable.append(provider)

    def register_payable(self, provider: PayableProvider) -> None:
        self._payable.append(provider)

    def register_commitment(self, provider: CommitmentProvider) -> None:
        self._commitment.append(provider)

    def register_inventory_posting(self, implementation: InventoryPosting) -> None:
        if self._inventory is not None:
            raise ValueError("InventoryPosting đã có bản cài — chỉ module kho được ghi sổ kho")
        self._inventory = implementation

    def register_ar_ap_subledger(self, implementation: ArApSubledger) -> None:
        if self._ar_ap_subledger is not None:
            raise ValueError(
                "ArApSubledger đã có bản cài — hai nơi ghi một sổ phụ công nợ (ADR-021)"
            )
        self._ar_ap_subledger = implementation

    def register_settlement_source(
        self, kind: SettlementTargetKind, source: SettlementTargetSource
    ) -> None:
        if kind in self._settlement_sources:
            raise ValueError(
                f"Loại đích đối trừ {kind.name} đã có source — "
                "hai bản cài là hai nơi tranh nhau một cột paid"
            )
        self._settlement_sources[kind] = source

    def register_treasurer_cash_book(self, implementation: TreasurerCashBook) -> None:
        if self._treasurer_cash_book is not None:
            raise ValueError("TreasurerCashBook đã có bản cài — chỉ module warehousing ghi sổ quỹ")
        self._treasurer_cash_book = implementation

    def register_treasurer_voucher_source(self, source: TreasurerVoucherSource) -> None:
        if self._treasurer_voucher_source is not None:
            raise ValueError(
                "TreasurerVoucherSource đã có bản cài — trạng thái thủ quỹ sống một chỗ"
            )
        self._treasurer_voucher_source = source

    def receivable_providers(self) -> tuple[ReceivableProvider, ...]:
        return tuple(self._receivable)

    def payable_providers(self) -> tuple[PayableProvider, ...]:
        return tuple(self._payable)

    def commitment_providers(self) -> tuple[CommitmentProvider, ...]:
        return tuple(self._commitment)

    def inventory_posting(self) -> InventoryPosting | None:
        """`None` = chưa có module kho (trước phase 8) — nơi gọi phải từ chối
        rõ ràng ("chưa bật phân hệ kho") thay vì giả vờ đã ghi."""
        return self._inventory

    def ar_ap_subledger(self) -> ArApSubledger | None:
        """`None` = chưa có module công nợ (trước phase 7) — nơi gọi phải từ
        chối ghi sổ chứng từ mua/bán rõ ràng, KHÔNG ghi bút toán rồi lặng lẽ
        bỏ qua sổ phụ: đó đúng là hình dạng lệch mà check 131/331 sinh ra để
        bắt, và bắt muộn hơn nhiều (ADR-021)."""
        return self._ar_ap_subledger

    def settlement_source(self, kind: SettlementTargetKind) -> SettlementTargetSource | None:
        """`None` = loại đích chưa có chủ (hóa đơn bán/mua trước phase 7) —
        nơi gọi từ chối dòng đối trừ trỏ vào loại đó, không đoán hộ."""
        return self._settlement_sources.get(kind)

    def treasurer_cash_book(self) -> TreasurerCashBook | None:
        """`None` = không có module sổ quỹ trong tiến trình — phiếu vẫn ghi sổ
        kế toán được, chỉ không có sổ quỹ song song."""
        return self._treasurer_cash_book

    def treasurer_voucher_source(self) -> TreasurerVoucherSource | None:
        """`None` = không có nguồn phiếu — hàng đợi thủ quỹ rỗng chứ không lỗi."""
        return self._treasurer_voucher_source


PROVIDERS: Final[CrossModuleProviders] = CrossModuleProviders()
"""Registry của tiến trình. Module đăng ký lúc import — cùng chỗ với đăng ký
mã quyền và loại chứng từ (xem `modules/general_ledger/journal/__init__.py`),
nên mọi điểm vào đi qua `ket.model_registry` đều thấy đủ bản cài."""
