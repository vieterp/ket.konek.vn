"""Luật chung cho dòng chứng từ chạm tài khoản công nợ (lát 7C-4).

Tách từ `modules/general_ledger/journal/settlement_service.py` cùng lý do đã
tách `posting/settlements.py` ở 6C: ba phân hệ ghi cùng một sổ phụ theo cùng
một luật — chứng từ nghiệp vụ khác (7C-3), phiếu thu/chi và chứng từ ngân hàng
(7C-4) — mà luật phụ thuộc #1 cấm module gọi module. Ba bản sao của một luật
phân loại là ba chỗ để lệch nhau, và lệch ở đây nghĩa là sổ cái nhích mà sổ
phụ đứng yên.

**Dòng chạm công nợ** = TK có `detail_tracking` khai đúng loại đối tác của
dòng (`customer` với khách hàng, `vendor` với nhà cung cấp). Đọc theo cấu hình
chứ không theo số hiệu `131`/`331`: số hiệu thuộc gói, và gói tự dựng của
khách đặt công nợ ở số khác là chuyện bình thường (SRS 19 §9 #1). Nhân viên
đứng ngoài — tạm ứng chưa có đường đối trừ theo từng lần trong v1, và check
toàn vẹn `arap_matches_control` cũng để nhân viên ngoài đúng vì lẽ ấy.

**Luật bốn ca, viết lại ở dạng hai trục vuông góc** (lát 7C-4 tổng quát hóa
bản 7C-3, quyết định user 2026-09-06):

* **BÊN của dòng** quyết định nó đối trừ được thứ gì. Bên THUẬN tính chất công
  nợ (Nợ với phải thu, Có với phải trả) làm khoản nợ lớn lên, nên thứ duy nhất
  nó tất toán được là một khoản **ứng trước**; bên NGƯỢC làm khoản nợ nhỏ đi,
  nên nó tất toán một khoản **nợ**.
* **Có trỏ đích hay không** quyết định dòng có sinh khoản mới hay không. Không
  trỏ đích thì dòng sinh một dòng sổ phụ: bên thuận ⇒ khoản nợ mới, bên ngược
  ⇒ **khoản ứng trước**.

Bản 7C-3 viết bốn ca ấy thành bốn nhánh, trong đó ca "bên ngược + không đích"
không sinh gì — lỗ ứng trước, điều kiện #2 chặn `arap_matches_control` từ 7A.
Hai trục trên đóng lỗ ấy mà không thêm nhánh nào: nó chỉ là ô thứ tư của một
bảng 2×2 vốn đã đầy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import DetailTracking
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.contracts import PartnerKind
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.money import convert_currency
from ket.kernel.protocols import PROVIDERS, ArApSubledger, SettlementTargetKind, SubledgerEntry
from ket.posting.documents.models import Voucher

__all__ = [
    "FINANCIAL_LEDGER",
    "SETTLEMENT_LINE_ACCOUNT_SPREAD_CODE",
    "SETTLEMENT_LINE_DIRECTION_MISMATCH_CODE",
    "SETTLEMENT_LINE_PARTNER_MISMATCH_CODE",
    "DebtLine",
    "DebtSide",
    "PairLine",
    "SettlementScope",
    "UnnumberedPairLine",
    "advance_kind",
    "classify",
    "due_date_of",
    "is_advance",
    "money_voucher_settles_advance",
    "open_debt_kind",
    "record_pair_voucher_debt",
    "remove_voucher_debt",
    "settlement_scope_of",
    "sides_of_pairs",
    "sides_of_payload_lines",
    "subledger_entries",
]

_ZERO = Decimal(0)

FINANCIAL_LEDGER: Final = 0
"""Sổ phụ công nợ chỉ ghi sổ tài chính, cùng luật với `purchase`/`sales`:
nguồn đối trừ của `receivables` chỉ cộng số đã trả vào sổ ấy, nên dòng sổ quản
trị sẽ không bao giờ đóng — báo cáo tuổi nợ sổ quản trị vì thế trống thay vì
sai. Chính vì thế `arap_matches_control` đo trong phạm vi `ledger = 0` (điều
kiện #6, quyết định user 2026-09-06)."""

_TRACKING_BY_PARTNER_KIND: Final[dict[PartnerKind, str]] = {
    PartnerKind.CUSTOMER: DetailTracking.CUSTOMER,
    PartnerKind.VENDOR: DetailTracking.VENDOR,
}
"""Nhân viên cố ý vắng mặt — xem docstring đầu tệp."""

_NATURAL_SIDE_IS_DEBIT: Final[dict[PartnerKind, bool]] = {
    PartnerKind.CUSTOMER: True,
    PartnerKind.VENDOR: False,
}

_OPEN_DEBT_KIND: Final[dict[PartnerKind, SettlementTargetKind]] = {
    PartnerKind.CUSTOMER: SettlementTargetKind.JOURNAL_RECEIVABLE,
    PartnerKind.VENDOR: SettlementTargetKind.JOURNAL_PAYABLE,
}

_ADVANCE_KIND: Final[dict[PartnerKind, SettlementTargetKind]] = {
    PartnerKind.CUSTOMER: SettlementTargetKind.ADVANCE_FROM_CUSTOMER,
    PartnerKind.VENDOR: SettlementTargetKind.ADVANCE_TO_VENDOR,
}

_ADVANCE_KINDS: Final[frozenset[SettlementTargetKind]] = frozenset(
    (*_ADVANCE_KIND.values(), SettlementTargetKind.OPENING_ADVANCE)
)
"""Mọi loại đích mang chiều NGƯỢC với chiều nợ của đối tác.

`OPENING_ADVANCE` không có trong `_ADVANCE_KIND`: bản đồ ấy trả loại đích mà
một DÒNG chứng từ sinh ra, còn khoản ứng trước đầu kỳ đến từ lượt nhập số dư
(lát 7C-5). Nó phải có mặt ở đây vì phép kiểm chiều đọc tập này — thiếu nó thì
phiếu THU tất toán được đúng khoản khách vừa ứng trước cho ta."""


def open_debt_kind(partner_kind: PartnerKind) -> SettlementTargetKind:
    """Loại đích của một khoản nợ ghi thẳng, không có hóa đơn gốc."""
    return _OPEN_DEBT_KIND[partner_kind]


def advance_kind(partner_kind: PartnerKind) -> SettlementTargetKind:
    """Loại đích của một khoản ứng trước — chiều NGƯỢC với chiều nợ của đối tác."""
    return _ADVANCE_KIND[partner_kind]


def is_advance(kind: SettlementTargetKind) -> bool:
    """Đích này là khoản ứng trước chứ không phải khoản nợ.

    Phân biệt được hai thứ ấy là việc của riêng hàm này: khoản ứng trước của
    một khách hàng nằm trên **cùng TK** (131) và **cùng đối tác** với hóa đơn
    của chính khách ấy, nên bốn phép kiểm đích của `posting.settlements`
    (đối tác / chi nhánh / tiền tệ / tài khoản) không tách được chúng.
    """
    return kind in _ADVANCE_KINDS


@dataclass(frozen=True)
class DebtSide:
    """Một BÊN của một dòng chứng từ, đã tách khỏi hình dạng riêng của phân hệ.

    Chứng từ nghiệp vụ khác lưu dòng **một bên** (`debit_fc`/`credit_fc`), còn
    phiếu thu/chi và chứng từ ngân hàng lưu **cặp Nợ/Có**; đưa cả hai về đây
    thì luật phân loại chỉ phải viết một lần. `sides_of_pairs` làm phép quy đổi
    cho hình dạng cặp.
    """

    line_no: int
    account_id: int
    partner_kind: PartnerKind
    partner_id: int
    on_debit: bool
    amount_fc: Decimal
    currency_code: str
    exchange_rate: Decimal


@dataclass(frozen=True)
class DebtLine:
    """Một bên dòng đã được nhận ra là chạm công nợ, kèm chiều và bên."""

    line_no: int
    account_id: int
    partner_kind: PartnerKind
    partner_id: int
    currency_code: str
    exchange_rate: Decimal
    amount_fc: Decimal
    """Số tiền trên bên của chính dòng — luôn dương."""

    increases_debt: bool
    """Bên thuận tính chất công nợ: dòng làm khoản nợ lớn lên."""

    @property
    def new_target_kind(self) -> SettlementTargetKind:
        """Loại đích của dòng sổ phụ dòng này sinh ra khi KHÔNG trỏ đích nào."""
        if self.increases_debt:
            return open_debt_kind(self.partner_kind)
        return advance_kind(self.partner_kind)

    @property
    def settles_advance(self) -> bool:
        """Dòng này đối trừ khoản ỨNG TRƯỚC (bên thuận) hay khoản NỢ (bên ngược).

        Một dòng làm khoản nợ lớn lên không tất toán được khoản nợ nào — thứ
        duy nhất nó tất toán được là nghĩa vụ chiều ngược đang treo, tức khoản
        ứng trước. Bản 7C-3 từ chối thẳng ca này vì lúc ấy khoản ứng trước chưa
        có dòng sổ phụ nào để trỏ tới.
        """
        return self.increases_debt

    @property
    def money_in(self) -> bool:
        """Chiều TIỀN của lượt đối trừ — mốc hướng lãi/lỗ tỷ giá (FR-SYS-066).

        Suy từ BÊN của dòng chứ không từ `target_kind` của đích: `OPENING_BALANCE`
        không tự mang chiều, và suy theo nó thì lãi ghi thành lỗ đúng ở ca ấy.
        Thu nợ khách (bên ngược của phải thu) và nhận lại tiền đã trả trước cho
        người bán (bên thuận của phải trả) đều là tiền VÀO.
        """
        return (self.partner_kind is PartnerKind.CUSTOMER) is not self.increases_debt


class UnnumberedPairLine(Protocol):
    """Thân dòng cặp Nợ/Có **trước khi ghi** — `PairLine` trừ đi `line_no`."""

    @property
    def debit_account_id(self) -> int | None: ...
    @property
    def credit_account_id(self) -> int | None: ...
    @property
    def amount_fc(self) -> Decimal: ...
    @property
    def partner_kind(self) -> int | None: ...
    @property
    def partner_id(self) -> int | None: ...


@dataclass(frozen=True)
class _NumberedLine:
    """Gắn số dòng vào một dòng thân chứng từ để nó thành `PairLine`."""

    line: UnnumberedPairLine
    line_no: int

    @property
    def debit_account_id(self) -> int | None:
        return self.line.debit_account_id

    @property
    def credit_account_id(self) -> int | None:
        return self.line.credit_account_id

    @property
    def amount_fc(self) -> Decimal:
        return self.line.amount_fc

    @property
    def partner_kind(self) -> int | None:
        return self.line.partner_kind

    @property
    def partner_id(self) -> int | None:
        return self.line.partner_id


class PairLine(Protocol):
    """Hình dạng dòng **cặp Nợ/Có** mà `sides_of_pairs` đọc.

    Protocol chứ không lớp cơ sở: `cash_voucher_lines` và `bank_voucher_lines`
    là hai bảng của hai module, và tầng `posting` không được biết tên bảng nào
    trong `modules/` (luật phụ thuộc #1).
    """

    @property
    def line_no(self) -> int: ...
    @property
    def debit_account_id(self) -> int | None: ...
    @property
    def credit_account_id(self) -> int | None: ...
    @property
    def amount_fc(self) -> Decimal: ...
    @property
    def partner_kind(self) -> int | None: ...
    @property
    def partner_id(self) -> int | None: ...


def sides_of_payload_lines(
    lines: Sequence[UnnumberedPairLine],
    *,
    currency_code: str,
    exchange_rate: Decimal,
) -> list[DebtSide]:
    """`sides_of_pairs` cho thân chứng từ **chưa ghi** (schema Pydantic).

    Thân gửi lên chưa mang `line_no` — số dòng do đường ghi đánh, `enumerate`
    từ 1 — nên phép kiểm chạy TRƯỚC lúc ghi phải tự đánh số theo đúng luật ấy,
    nếu không thông điệp lỗi trỏ người dùng vào một dòng khác dòng họ gõ sai.
    """
    return sides_of_pairs(
        [_NumberedLine(line, line_no) for line_no, line in enumerate(lines, start=1)],
        currency_code=currency_code,
        exchange_rate=exchange_rate,
    )


def sides_of_pairs(
    lines: Sequence[PairLine],
    *,
    currency_code: str,
    exchange_rate: Decimal,
) -> list[DebtSide]:
    """Trải dòng hình dạng **cặp Nợ/Có** thành các bên (phiếu thu/chi, ngân hàng).

    Mỗi dòng cho tối đa hai bên; bên nào cũng có thể là bên công nợ, và cả hai
    cùng lúc là chuyện hợp lệ (bù trừ 131 ↔ 331 trên một dòng). Tiền tệ và tỷ
    giá lấy từ CHỨNG TỪ vì hai hình dạng ấy không mang tiền tệ theo dòng.
    """
    sides: list[DebtSide] = []
    for line in lines:
        if line.amount_fc <= _ZERO:
            continue
        partner_kind = line.partner_kind
        partner_id = line.partner_id
        if partner_kind is None or partner_id is None:
            continue
        for account_id, on_debit in (
            (line.debit_account_id, True),
            (line.credit_account_id, False),
        ):
            if account_id is None:
                continue
            sides.append(
                DebtSide(
                    line_no=line.line_no,
                    account_id=account_id,
                    partner_kind=PartnerKind(partner_kind),
                    partner_id=partner_id,
                    on_debit=on_debit,
                    amount_fc=line.amount_fc,
                    currency_code=currency_code,
                    exchange_rate=exchange_rate,
                )
            )
    return sides


def classify(session: Session, sides: Sequence[DebtSide]) -> list[DebtLine]:
    """Lọc ra các bên chạm công nợ, kèm chiều và bên.

    Một truy vấn cho cả chứng từ (`accounts_by_id`), cùng lối `posting_mapper`.
    """
    accounts = accounts_by_id(session, [side.account_id for side in sides])
    debt_lines: list[DebtLine] = []
    for side in sides:
        tracking_token = _TRACKING_BY_PARTNER_KIND.get(side.partner_kind)
        if tracking_token is None:
            continue
        account = accounts.get(side.account_id)
        if account is None or tracking_token not in (account.detail_tracking or ()):
            continue
        if side.amount_fc <= _ZERO:
            continue
        debt_lines.append(
            DebtLine(
                line_no=side.line_no,
                account_id=side.account_id,
                partner_kind=side.partner_kind,
                partner_id=side.partner_id,
                currency_code=side.currency_code,
                exchange_rate=side.exchange_rate,
                amount_fc=side.amount_fc,
                increases_debt=(side.on_debit == _NATURAL_SIDE_IS_DEBIT[side.partner_kind]),
            )
        )
    return debt_lines


SETTLEMENT_LINE_PARTNER_MISMATCH_CODE = "settlement.line_partner_mismatch"
SETTLEMENT_LINE_ACCOUNT_SPREAD_CODE = "settlement.line_account_spread"
SETTLEMENT_LINE_DIRECTION_MISMATCH_CODE = "settlement.line_direction_mismatch"


@dataclass(frozen=True)
class SettlementScope:
    """Phạm vi mà khối đối trừ của một chứng từ TIỀN được phép chạm tới.

    `total_fc` là tổng nguyên tệ của các bên CÔNG NỢ, không phải tổng tiền
    chứng từ; `account_id` là TK công nợ duy nhất mà chúng nằm trên.
    """

    total_fc: Decimal
    account_id: int | None


def settlement_scope_of(
    session: Session,
    *,
    has_settlements: bool,
    settles_advance: bool,
    sides: Sequence[DebtSide],
    partner_kind: PartnerKind | None,
    partner_id: int | None,
) -> SettlementScope:
    """Phạm vi đối trừ của phiếu thu/chi và chứng từ ngân hàng (lát 7C-5).

    Trước lát này hai phân hệ ấy đưa **tổng tiền chứng từ** vào BR-QUY-03 và
    KHÔNG truyền `account_id`, trong khi `posting_mapper` ghi sổ cái theo đối
    tác của TỪNG DÒNG. Không cổng nào buộc hai thứ bằng nhau, nên một phiếu thu
    150 gồm `Có 131 khách A 100 + Có 511 50` đối trừ 150 vào hóa đơn của A là
    chứng từ **lọt mọi cổng** hôm nay: sổ cái nhích 100, sổ phụ nhích 150. Đó
    là điều kiện #9 chặn `arap_matches_control`, và cùng nó là lý do
    `record_pair_voucher_debt` phải dùng một phép xấp xỉ.

    Đo theo dòng công nợ đóng cả hai chuyện một lượt, và **nới** chứ không chỉ
    siết: phiếu thu gộp thu nợ với doanh thu bán lẻ hôm nay không lập được (nó
    buộc phải đối trừ cả phần doanh thu), từ lát này lập được — phần đối trừ
    khớp đúng phần công nợ.

    Ba vi phạm mới đóng nốt phần còn lại của hình dạng ấy: dòng công nợ mang
    đối tác khác đối tác của chứng từ (khối đối trừ nhận đối tác ở HEADER, nên
    hai chỗ lệch nhau là lệch cả phạm vi lẫn số), dòng công nợ trải trên nhiều
    TK (đích đối trừ chỉ nhận được MỘT `account_id`, đúng như mua/bán/GLE đã
    truyền từ 7B), và dòng công nợ **ngược chiều** với chiều đối trừ của chứng
    từ.

    Vế thứ ba là thứ giữ cho tổng có ý nghĩa. `settles_advance` là một giá trị
    cho CẢ chứng từ (suy từ loại phiếu, `money_voucher_settles_advance`), còn
    dòng thì có chiều riêng — nên nếu chỉ cộng số tiền thì một phiếu thu gồm
    `Có 131 khách A 100` **và** `Nợ 131 khách A 20` có tổng dòng công nợ 120
    trong khi sổ cái chỉ nhích 80: đối trừ 120 lọt cổng và hai vế lệch 40. Dòng
    không chạm quỹ được phép ở phiếu thu/chi (`posting_mapper._split_pair` nói
    thẳng), nên hình dạng ấy dựng được. Từ chối nó giữ đúng bất biến "mỗi loại
    đích một chiều" mà 7A đặt ra: một chứng từ tất toán một chiều.

    Ném ngay thay vì gom cùng vi phạm của `price_settlements`: chưa biết khối
    đối trừ được phép chạm tới đâu thì "lệch tổng" chỉ là tiếng vọng — cùng lẽ
    với vế `if not violations` của chính phép so tổng ấy.

    **Chỉ áp khi chứng từ CÓ khối đối trừ** — cùng vế thoát sớm `if not
    settlements` của `price_settlements`. Một chứng từ tiền chi cho ba nhà cung
    cấp trong một lượt, không đối trừ hóa đơn nào, là chứng từ hợp lệ: mỗi dòng
    của nó sinh một khoản sổ phụ riêng qua `record_pair_voucher_debt`, và ở đó
    không có con số ở mức chứng từ nào để hai vế phải khớp. Áp phạm vi cho nó
    là cấm một hình dạng mà lát này không có lý do gì đụng tới.
    """
    if not has_settlements:
        return SettlementScope(total_fc=_ZERO, account_id=None)
    debt_lines = classify(session, sides)
    violations: list[PostingViolation] = []
    if partner_id is not None and partner_kind is not None:
        for line in debt_lines:
            if line.partner_id != partner_id or line.partner_kind is not partner_kind:
                violations.append(
                    PostingViolation(
                        SETTLEMENT_LINE_PARTNER_MISMATCH_CODE,
                        "Dòng chạm tài khoản công nợ phải cùng đối tượng với chứng từ "
                        "khi chứng từ có khối đối trừ",
                        line_no=line.line_no,
                        line_partner_id=str(line.partner_id),
                        voucher_partner_id=str(partner_id),
                    )
                )
    for line in debt_lines:
        if line.settles_advance != settles_advance:
            violations.append(
                PostingViolation(
                    SETTLEMENT_LINE_DIRECTION_MISMATCH_CODE,
                    "Dòng công nợ đi ngược chiều đối trừ của chứng từ — tách nó sang "
                    "chứng từ riêng",
                    line_no=line.line_no,
                    account_id=str(line.account_id),
                )
            )
    accounts = {line.account_id for line in debt_lines}
    if len(accounts) > 1:
        violations.append(
            PostingViolation(
                SETTLEMENT_LINE_ACCOUNT_SPREAD_CODE,
                "Các dòng công nợ của chứng từ nằm trên nhiều tài khoản — tách thành "
                "chứng từ riêng cho mỗi tài khoản công nợ",
                account_ids=",".join(str(account_id) for account_id in sorted(accounts)),
            )
        )
    if violations:
        raise PostingValidationError(
            "Dòng công nợ của chứng từ chưa khớp khối đối trừ", violations=violations
        )
    return SettlementScope(
        total_fc=sum((line.amount_fc for line in debt_lines), _ZERO),
        account_id=next(iter(accounts), None),
    )


def due_date_of(session: Session, *, partner_id: int, document_date: date) -> date | None:
    """Hạn thanh toán rơi về điều khoản của danh mục đối tác (7C-3, Q1-B).

    Chứng từ ghi tay không có ô điều khoản thanh toán, và một khoản nợ không có
    hạn thì **không bao giờ kêu quá hạn** — nó nằm mãi ở cột "chưa đến hạn" của
    báo cáo tuổi nợ và ngoài tầm guard ngưỡng nợ. Rơi về danh mục là cùng luật
    FR-SAL-009 mà hóa đơn bán đã dùng từ 7C-2.

    Đối tác chưa khai điều khoản → để trống, không đoán.
    """
    term_id = session.execute(
        select(Partner.payment_term_id).where(Partner.id == partner_id)
    ).scalar_one_or_none()
    if term_id is None:
        return None
    term = session.get(PaymentTerm, term_id)
    if term is None:
        return None
    return document_date + timedelta(days=term.due_days)


def subledger_entries(
    session: Session,
    *,
    voucher: Voucher,
    debt_lines: Sequence[DebtLine],
    scale: int,
) -> list[SubledgerEntry]:
    """Dòng sổ phụ mà các bên KHÔNG trỏ đích sinh ra — một dòng cho mỗi bên.

    Không gộp theo (đối tác, TK) như hóa đơn mua: mỗi dòng là một khoản người
    dùng cố ý tách ra, và gộp lại thì màn đối trừ mất đúng ranh giới ấy.

    Người gọi lọc trước những bên đã trỏ đích — chúng là lượt ĐỐI TRỪ, không
    sinh khoản mới. Khoản ứng trước để **hạn trống**: không có gì đến hạn ở một
    khoản ta đang giữ tiền của người khác, và cho nó một hạn thì báo cáo tuổi
    nợ sẽ kêu quá hạn một nghĩa vụ không ai đòi.
    """
    return [
        SubledgerEntry(
            target_kind=line.new_target_kind,
            partner_kind=line.partner_kind,
            partner_id=line.partner_id,
            ledger=FINANCIAL_LEDGER,
            account_id=line.account_id,
            document_no=voucher.voucher_no,
            document_date=voucher.document_date,
            due_date=(
                due_date_of(
                    session, partner_id=line.partner_id, document_date=voucher.document_date
                )
                if line.increases_debt
                else None
            ),
            currency_code=line.currency_code,
            exchange_rate=line.exchange_rate,
            amount_fc=line.amount_fc,
            amount=convert_currency(line.amount_fc, line.exchange_rate, scale),
            description=voucher.description,
        )
        for line in debt_lines
    ]


def money_voucher_settles_advance(*, money_in: bool, partner_kind: PartnerKind | None) -> bool:
    """Chứng từ TIỀN đang tất toán khoản ứng trước, hay khoản nợ?

    Phiếu thu/chi và chứng từ ngân hàng giữ khối đối trừ ở mức CHỨNG TỪ (một
    đối tác cho cả phiếu), nên chiều suy từ chiều tiền của loại chứng từ thay
    vì từ bên của từng dòng như chứng từ nghiệp vụ khác. Hai lối cho cùng một
    kết quả: tiền VÀO từ khách là thu nợ, tiền VÀO từ người bán là họ trả lại
    khoản ta đã ứng trước.

    Đối tác trống → `False`: chứng từ không có đối tác thì cũng không có dòng
    đối trừ nào để kiểm, và `price_settlements` đã báo vi phạm riêng cho ca ấy.
    """
    if partner_kind is None:
        return False
    return money_in is (partner_kind is PartnerKind.VENDOR)


def _require_subledger() -> ArApSubledger:
    """Cửa ghi sổ phụ công nợ — `receivables` cài, tầng này chỉ gọi (ADR-021)."""
    subledger = PROVIDERS.ar_ap_subledger()
    if subledger is None:  # pragma: no cover - `model_registry` luôn nạp bản cài
        raise RuntimeError("Chưa có bản cài sổ phụ công nợ (ArApSubledger)")
    return subledger


def record_pair_voucher_debt(
    session: Session,
    *,
    voucher: Voucher,
    lines: Sequence[PairLine],
    currency_code: str,
    exchange_rate: Decimal,
    scale: int,
    has_settlements: bool,
) -> None:
    """Ghi sổ phụ cho chứng từ TIỀN vừa ghi sổ (phiếu thu/chi, chứng từ ngân hàng).

    `has_settlements` là **toàn bộ** phép phân nhánh: chứng từ có khối đối trừ
    thì coi như mọi chuyển động công nợ của nó đã đi qua `settled` của các đích;
    chứng từ không có thì mọi dòng chạm công nợ là khoản mới — ứng trước ở bên
    ngược, khoản nợ ở bên thuận.

    Phép phân nhánh ấy là một **bất biến từ lát 7C-5**, trước đó là một xấp
    xỉ: `settlement_scope_of` buộc tổng khối đối trừ bằng đúng tổng DÒNG CÔNG
    NỢ (không phải tổng tiền chứng từ) và buộc mọi dòng công nợ cùng đối tác
    với chứng từ, nên "có khối đối trừ" nay kéo theo "mọi chuyển động công nợ
    đã đi qua `settled`". Bản trước so với tổng MỌI dòng, nên một phiếu thu 150
    gồm Có 131 khách A 100 + Có 511 50 đối trừ 150 vào hóa đơn của A lọt mọi
    cổng và để lại sổ cái nhích 100 trong khi sổ phụ nhích 150 — điều kiện #9
    chặn `arap_matches_control` cho tới lát ấy.

    Gọi **vô điều kiện**, kể cả khi không có dòng nào: `record` thay TRỌN theo
    `voucher_id`, nên nó cũng là lượt dọn dòng cũ của một chứng từ vừa được sửa
    từ "có công nợ" thành "không".
    """
    debt_lines = (
        []
        if has_settlements
        else classify(
            session,
            sides_of_pairs(lines, currency_code=currency_code, exchange_rate=exchange_rate),
        )
    )
    _require_subledger().record(
        session,
        voucher_id=voucher.id,
        entries=subledger_entries(session, voucher=voucher, debt_lines=debt_lines, scale=scale),
    )


def remove_voucher_debt(session: Session, *, voucher_id: UUID) -> None:
    """Gỡ dòng sổ phụ của một chứng từ vừa bỏ ghi sổ."""
    _require_subledger().remove(session, voucher_id=voucher_id)
