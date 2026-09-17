"""Khoản công nợ còn treo theo chứng từ — cửa đọc dùng chung của các BFF (lát 7G-4).

Thẻ công nợ trên màn hình đối tác và tab "việc còn thiếu" (nhóm quá hạn) của
mua/bán đều cần cùng một con số: **mỗi chứng từ còn nợ bao nhiêu tại một
ngày**. Con số ấy đã có một định nghĩa duy nhất trong hệ — dataset
`ar_ap_open_items` (7G-1, gộp aging ở 7G-2b): hai nguồn (sổ phụ + số dư ban
đầu), đối trừ tính TẠI ngày chốt, niên độ chọn theo từng chi nhánh. Tệp này
**chạy dataset đó** qua đúng executor của report engine chứ không chép lại câu
SQL: 7G-2b đã ghi rõ hai bản chép của khối UNION lệch nhau hai lần trong một
vòng review, và một BFF chép bản thứ ba sẽ là thẻ công nợ nói khác báo cáo
tuổi nợ — thứ người dùng mang đi đối chiếu với đối tác.

Vì sao KHÔNG dùng `partner_open_debt(...)` (migration 0026): hàm ấy là
`SECURITY DEFINER` chạy **ngoài RLS**, đúng cho guard ngưỡng nợ (ngưỡng của
đối tác trước toàn công ty) và sai cho một cửa ĐỌC — nó sẽ in công nợ của chi
nhánh mà người xem không được thấy. Cửa này chạy trong session của người gọi,
`branch_ids = NULL`, RLS lọc ở tầng bảng.

Chỉ **sổ tài chính** (`ledger = 0`), cùng phạm vi với guard và với
`partner_open_debt`: sổ quản trị có thể ghi khác, và một thẻ trộn hai sổ vào một
con số là sai ngay lần đầu ai đó ghi bút toán chỉ-quản-trị (ADR-006).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.config.reports.models import ReportDataset
from ket.kernel.contracts import Ledger, PartnerKind
from ket.kernel.protocols import SettlementTargetKind
from ket.reporting.engine.executor import execute_dataset_ordered

OPEN_ITEMS_DATASET_CODE: Final[str] = "ar_ap_open_items"

Direction = Literal["thu", "chi"]
"""Giá trị `:direction` của dataset — `thu` phải thu, `chi` phải trả."""

DueState = Literal["qua-han", "chua-den-han"]

_SORT: Final[tuple[str, ...]] = ("due_date", "document_date", "document_no")
"""Khoản đến hạn sớm nhất lên đầu — thứ tự của một danh sách "việc cần làm",
không phải của một tờ báo cáo. `NULLS` (không ghi hạn) rơi xuống cuối theo
mặc định `ASC` của PostgreSQL, đúng ý: khoản không có hạn không phải việc gấp."""


@dataclass(frozen=True, slots=True)
class OpenItem:
    """Một khoản còn treo, đúng những cột hai BFF cần."""

    item_id: UUID
    """Khóa của KHOẢN nợ (dòng `ar_ap_ledger` hoặc dòng `opening_balance_invoices`)
    — ổn định giữa hai mốc đọc trong cùng niên độ; dòng chuyển sang niên độ mới
    mang khóa MỚI, và đó chính là điều `period_summary` dựa vào."""
    document_id: UUID | None
    """`None` với nợ mang sang từ số dư ban đầu — không có chứng từ để mở."""
    target_kind: int
    """`SettlementTargetKind` của khoản — 5/6 là khoản ỨNG TRƯỚC, không phải nợ."""
    source_label: str
    """Nguồn khoản nợ bằng tiếng người ("Hóa đơn bán", "Khách ứng trước"…) — cột
    của dataset; client cần nó để biết `document_id` trỏ loại chứng từ nào (nợ
    ghi tay trỏ chứng từ GLE, không phải hóa đơn mua/bán)."""
    document_no: str | None
    document_date: date | None
    """Cả hai `None` với khoản ứng trước đầu kỳ: `advance_has_no_invoice_ref`
    buộc dòng ấy bỏ trống số và ngày hóa đơn — nó không phải hóa đơn nào cả."""
    due_date: date | None
    partner_kind: int
    partner_id: int
    partner_code: str | None
    partner_name: str | None
    currency_code: str
    amount_fc: Decimal
    settled_fc: Decimal
    remaining_fc: Decimal
    """Ba số theo NGUYÊN TỆ — trục đối trừ, và là con số in cạnh mã tiền."""
    amount: Decimal
    settled: Decimal
    """Giá trị gốc và phần đã đối trừ TẠI `as_of` (VND theo tỷ giá ghi nhận nợ) —
    biên bản đối chiếu theo kỳ (7G-5) cần cả hai để tính phát sinh giảm."""
    remaining: Decimal
    """Phần còn nợ quy đổi theo tỷ giá GHI NHẬN nợ — trục cộng chung nhiều tiền."""
    days_overdue: int | None


def open_items(
    session: Session,
    *,
    direction: Direction,
    as_of: date,
    partner_kind: PartnerKind | None = None,
    partner_id: int | None = None,
    due_state: DueState | None = None,
    open_only: bool = True,
) -> Iterator[OpenItem]:
    """Khoản còn treo của một chiều tại `as_of`, trong phạm vi RLS người gọi.

    `open_only=False` trả CẢ khoản đã tất toán tại `as_of` — biên bản đối chiếu
    theo kỳ cần chúng để cộng "phát sinh giảm trong kỳ" (một hóa đơn thu đủ
    trong kỳ không còn treo ở cuối kỳ nhưng lượt thu ấy là phát sinh của kỳ).

    `partner_kind` đi kèm `partner_id` chứ không suy từ `direction`: nhân viên
    (kind 2) đứng ở một không gian id khác đối tác, và dataset chỉ khớp đúng
    người khi được nói rõ loại (xem chú thích `WHERE` cuối tệp SQL).
    """
    dataset = session.get(ReportDataset, OPEN_ITEMS_DATASET_CODE)
    if dataset is None:  # pragma: no cover - seed builtin bảo đảm
        raise RuntimeError(f"Dataset {OPEN_ITEMS_DATASET_CODE!r} chưa được seed")
    binds: dict[str, object] = {
        # `:from_date` thuộc bộ bind chuẩn của engine; dataset không dùng nhưng
        # `text()` vẫn thấy tên ấy (nó đứng trong chú thích đầu tệp SQL) và
        # psycopg không gửi được một NULL không kiểu — nên bind một ngày thật.
        "from_date": as_of,
        "to_date": as_of,
        "ledger": int(Ledger.FINANCIAL),
        "branch_ids": None,
        "direction": direction,
        "partner_id": partner_id,
        "partner_kind": int(partner_kind) if partner_kind is not None else None,
        "open_only": open_only,
        "due_state": due_state,
    }
    for row in execute_dataset_ordered(session, dataset=dataset, sort=_SORT, binds=binds):
        yield OpenItem(
            item_id=_as_uuid(row["item_id"]),
            document_id=_uuid_or_none(row["document_id"]),
            target_kind=int(str(row["target_kind"])),
            source_label=str(row["source_label"]),
            document_no=_str_or_none(row["document_no"]),
            document_date=_date_or_none(row["document_date"]),
            due_date=_date_or_none(row["due_date"]),
            partner_kind=int(str(row["partner_kind"])),
            partner_id=int(str(row["partner_id"])),
            partner_code=_str_or_none(row["partner_code"]),
            partner_name=_str_or_none(row["partner_name"]),
            currency_code=str(row["currency_code"]),
            amount_fc=Decimal(str(row["amount_fc"])),
            settled_fc=Decimal(str(row["settled_fc"])),
            remaining_fc=Decimal(str(row["remaining_fc"])),
            amount=Decimal(str(row["amount"])),
            settled=Decimal(str(row["settled"])),
            remaining=Decimal(str(row["remaining"])),
            days_overdue=int(str(row["days_overdue"])) if row["days_overdue"] is not None else None,
        )


@dataclass(frozen=True, slots=True)
class OpenItemsSummary:
    """Tổng của một chiều — ruột của nửa thẻ công nợ."""

    open_amount: Decimal
    open_count: int
    overdue_amount: Decimal
    overdue_count: int
    oldest_due_date: date | None
    """Hạn sớm nhất trong các khoản QUÁ HẠN — chỗ bấu đầu tiên khi đi đòi/trả."""


ADVANCE_TARGET_KINDS: Final[frozenset[int]] = frozenset(
    {
        int(SettlementTargetKind.ADVANCE_FROM_CUSTOMER),
        int(SettlementTargetKind.ADVANCE_TO_VENDOR),
        int(SettlementTargetKind.OPENING_ADVANCE),
    }
)
"""Khoản ứng trước (5 khách ứng, 6 ta trả trước người bán, 7 ứng trước đầu kỳ):
tiền ứng trước đứng NGƯỢC chiều nợ, nên nó có mặt trên thẻ công nợ (người xem
phải thấy khách đã ứng bao nhiêu) nhưng KHÔNG được trừ vào ngưỡng nợ — cùng luật
với `partner_open_debt()` mà `PartnerDebtGuard` dùng (0030 loại 5/6, 0031 loại
`is_advance`), nếu không thẻ nói "còn được nợ" một số mà guard sẽ không chặn ở đó."""


def debt_excluding_advances(items: Sequence[OpenItem]) -> Decimal:
    """Phần còn nợ THẬT (không kể khoản ứng trước) — vế trừ của ngưỡng nợ."""
    return sum(
        (item.remaining for item in items if item.target_kind not in ADVANCE_TARGET_KINDS),
        Decimal(0),
    )


def summarize(items: Sequence[OpenItem]) -> OpenItemsSummary:
    """Cộng một lần trên danh sách đã đọc; quá hạn = có hạn và hạn đã qua
    (`days_overdue > 0`) — cùng phép chia `due_state` của dataset."""
    open_amount = Decimal(0)
    overdue_amount = Decimal(0)
    overdue_count = 0
    oldest: date | None = None
    for item in items:
        open_amount += item.remaining
        if item.days_overdue is not None and item.days_overdue > 0:
            overdue_amount += item.remaining
            overdue_count += 1
            if item.due_date is not None and (oldest is None or item.due_date < oldest):
                oldest = item.due_date
    return OpenItemsSummary(
        open_amount=open_amount,
        open_count=len(items),
        overdue_amount=overdue_amount,
        overdue_count=overdue_count,
        oldest_due_date=oldest,
    )


@dataclass(frozen=True, slots=True)
class PeriodSummary:
    """Bốn con số của biên bản đối chiếu theo kỳ (VND), giữ bất biến
    `opening + increase - decrease == closing`."""

    opening: Decimal
    """Còn nợ tại ngày liền trước kỳ."""
    increase: Decimal
    """Giá trị các khoản có `document_date` trong kỳ."""
    decrease: Decimal
    """Phần đối trừ ghi sổ trong kỳ — kể cả đối trừ của khoản phát sinh trước kỳ."""
    closing: Decimal
    """Còn nợ tại ngày cuối kỳ."""


def period_summary(
    *, before: Sequence[OpenItem], at: Sequence[OpenItem], from_date: date, to_date: date
) -> PeriodSummary:
    """Ghép hai lượt đọc dataset (`open_only=False`) thành bốn số của kỳ.

    `before` đọc tại `from_date − 1`, `at` đọc tại `to_date`, và phép ghép đi
    **theo từng khoản** (`item_id`), không theo hai tổng: với mỗi khoản của `at`,
    phần đã đối trừ tại mốc trước tra từ `before` (không có thì 0). Khoản không
    có ở `before` mà ngày chứng từ trong kỳ là phát sinh TĂNG; mọi khoản còn lại
    vào ĐẦU KỲ với giá trị trừ phần đã trả trước kỳ; GIẢM là phần đối trừ nhích
    thêm giữa hai mốc; CUỐI là còn nợ tại `to_date`. Đẳng thức đầu + tăng − giảm
    = cuối vì thế đúng **bằng cấu trúc** với từng khoản, không phải bằng một phép
    cộng riêng.

    Vì sao không trừ hai tổng (bản đầu, review 7G-5 H-1): dataset chọn niên độ
    số dư ban đầu theo `to_date`, nên biên bản cả năm đọc `before` ở niên độ cũ
    (giá trị gốc, đã trả năm trước) và `at` ở niên độ mới (dòng chuyển sang mang
    khóa MỚI, giá trị = phần còn lại lúc chuyển, đã trả = chỉ năm nay) — hiệu hai
    tổng đã trả khi ấy trừ luôn phần trả năm trước và in một "phát sinh giảm"
    ÂM lên tờ giấy hai bên ký. Ghép theo khoản thì dòng chuyển sang không có ở
    `before`, vào đầu kỳ đúng bằng phần còn lại lúc chuyển. Cùng lối, dòng số dư
    ban đầu có ngày hóa đơn rơi trong kỳ (review M-1) không bị đếm hai lần: nó
    có ở `before` nên thuộc đầu kỳ, không thuộc tăng.
    """
    settled_before = {item.item_id: item.settled for item in before}
    opening = increase = decrease = closing = Decimal(0)
    for item in at:
        previously = settled_before.get(item.item_id)
        is_new = previously is None
        in_period = item.document_date is not None and from_date <= item.document_date <= to_date
        if is_new and in_period:
            increase += item.amount
        else:
            opening += item.amount - (previously or Decimal(0))
        decrease += item.settled - (previously or Decimal(0))
        closing += item.remaining
    return PeriodSummary(opening=opening, increase=increase, decrease=decrease, closing=closing)


def _as_uuid(value: object) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _uuid_or_none(value: object) -> UUID | None:
    return None if value is None else _as_uuid(value)


def _date_or_none(value: object) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)
