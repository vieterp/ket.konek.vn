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
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.money import convert_currency
from ket.kernel.protocols import PROVIDERS, ArApSubledger, SettlementTargetKind, SubledgerEntry
from ket.posting.documents.models import Voucher

__all__ = [
    "FINANCIAL_LEDGER",
    "DebtLine",
    "DebtSide",
    "PairLine",
    "advance_kind",
    "classify",
    "due_date_of",
    "is_advance",
    "money_voucher_settles_advance",
    "open_debt_kind",
    "record_pair_voucher_debt",
    "remove_voucher_debt",
    "sides_of_pairs",
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

_ADVANCE_KINDS: Final[frozenset[SettlementTargetKind]] = frozenset(_ADVANCE_KIND.values())


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

    **Đây là một phép XẤP XỈ, không phải một bất biến.** BR-QUY-03 chỉ so tổng
    đối trừ với tổng tiền của chứng từ — tổng MỌI dòng, không riêng dòng công
    nợ — trong khi khối đối trừ của phiếu thu/chi và chứng từ ngân hàng nhận
    `partner_id` của HEADER còn `posting_mapper` ghi sổ cái theo đối tác của
    TỪNG DÒNG. Một phiếu thu 150 gồm Có 131 khách A 100 + Có 511 50, đối trừ
    150 vào hóa đơn của A, vì thế lọt qua mọi cổng hôm nay và để lại vế sổ cái
    nhích 100 trong khi vế sổ phụ nhích 150.

    Xấp xỉ này đúng với mọi chứng từ mà form dựng ra, và nó là lý do
    `arap_matches_control` **vẫn chưa** được đăng ký (điều kiện #9 ở đầu tệp
    ấy). Đóng nó là siết cash/bank cho khớp mua/bán/GLE — đối tác và TK của
    dòng công nợ phải khớp khối đối trừ — và đó là một thay đổi PHÁ VỠ với
    chứng từ hợp lệ hôm nay, nên là quyết định sản phẩm chứ không phải bản vá.

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
