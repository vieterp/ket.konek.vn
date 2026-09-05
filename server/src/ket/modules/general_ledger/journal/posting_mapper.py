"""Dịch dòng chi tiết → `PostingRequest` (phase-04 §Chứng từ).

Đây là hợp đồng một chiều giữa module và engine: engine không đọc
`gl_journal_lines`, module không chạm `gl_postings`. Chứng từ nghiệp vụ khác
ghi hai sổ **giống nhau** nên `management_lines` để `None` — engine tự nhân
bản (LD-07; chứng từ ghi khác nhau giữa hai sổ xuất hiện từ phase 9).

**Cặp chênh lệch tỷ giá là dòng hệ thống thêm, không phải dòng người dùng gõ**
(lát 7C-3). Mọi dòng khác của chứng từ này do kế toán tự gõ, nên thoạt nhìn để
họ tự gõ cả cặp bù 515/635 có vẻ đúng tinh thần. Nó không chạy được: một dòng
`Nợ 131 <đối tác> / Có 515` gõ tay bị `settlement_service.classify` đọc thành
dòng ở bên THUẬN tính chất ⇒ sinh một **khoản phải thu ma**; còn dòng 131
không mang đối tác thì validator `dimension_required` chặn (TK ấy khai
`detail_tracking`). Nghĩa là không có đường tay nào giữ 131/331 khớp sổ phụ
trên chứng từ ngoại tệ — phải sinh tự động, đúng như bốn bản cài kia
(`cash_book`, `bank`, `purchase`, `sales`).

`classify` đọc `gl_journal_lines` chứ không đọc `PostingRequest`, nên cặp dòng
thêm ở đây **không** quay lại sinh khoản nợ nào.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import DEPOSIT_ACCOUNT_CODE_PREFIX
from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.protocols import SettlementTargetKind
from ket.modules.general_ledger.journal.models import JournalLine, JournalSettlement
from ket.posting.contracts import (
    ExtendedDimensionValue,
    PartnerKind,
    PostingDimensions,
    PostingLine,
    PostingRequest,
    Voucher,
)
from ket.posting.settlements import fx_adjustment_lines


def to_posting_request(
    session: Session, voucher_id: UUID, lines: Sequence[JournalLine]
) -> PostingRequest:
    # Chiều `bank_account` chỉ có nghĩa trên dòng 112x: một dòng GLE khai TK
    # ngân hàng cho TK khác là dữ liệu người dùng gõ nhầm, và để nó đi vào sổ
    # thì lượt chuyển số dư đầu năm xếp dòng ấy sang nhóm tiền gửi (review
    # pre-landing H-A). Lọc theo SỐ HIỆU TK, cùng doctrine với hai mapper kia.
    accounts = accounts_by_id(session, [line.account_id for line in lines])
    deposit_account_ids = {
        account_id
        for account_id, account in accounts.items()
        if account.code.startswith(DEPOSIT_ACCOUNT_CODE_PREFIX)
    }
    posting_lines = [_to_posting_line(line, deposit_account_ids) for line in lines]
    posting_lines.extend(_fx_lines(session, voucher_id, lines))
    return PostingRequest(
        voucher_id=voucher_id,
        financial_lines=tuple(posting_lines),
        management_lines=None,
    )


def _fx_lines(
    session: Session, voucher_id: UUID, lines: Sequence[JournalLine]
) -> list[PostingLine]:
    """Cặp bù chênh lệch tỷ giá cho mọi dòng đối trừ của chứng từ.

    Chẻ theo CHIỀU rồi gọi hai lượt, vì `fx_adjustment_lines` nhận một
    `money_in` cho cả lượt còn một chứng từ GLE đối trừ được **cả hai chiều
    cùng lúc** — chính hình dạng của bút toán bù trừ 131 ↔ 331. Hướng lãi/lỗ
    phụ thuộc chiều tiền (`_is_gain`), nên trộn hai chiều vào một lượt sẽ ghi
    lãi thành lỗ đúng ở nửa còn lại.
    """
    from ket.modules.general_ledger.journal.settlement_service import classify

    settlements = (
        session.execute(select(JournalSettlement).where(JournalSettlement.voucher_id == voucher_id))
        .scalars()
        .all()
    )
    if not settlements:
        return []
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - engine vừa nạp chính chứng từ này
        raise RuntimeError(f"Không tìm thấy chứng từ {voucher_id} để dựng dòng chênh lệch")

    # Chiều lấy từ BÊN của dòng định khoản, KHÔNG từ `target_kind` của đích.
    # Một dòng GLE ghi giảm khoản phải thu có thể trỏ vào hóa đơn số dư đầu kỳ,
    # và `OPENING_BALANCE` là loại đích duy nhất không tự mang chiều — suy theo
    # nó thì đúng ca ấy bị xếp sang chiều phải trả và lãi ghi thành lỗ.
    # `classify` đã chốt chiều đúng một lần, ở đúng chỗ nó quan sát được.
    money_in_by_line = {
        line.line_id: line.target_kind is SettlementTargetKind.JOURNAL_RECEIVABLE
        for line in classify(session, lines)
    }

    adjustments: list[PostingLine] = []
    for money_in in (True, False):
        group = [
            row
            for row in settlements
            if money_in_by_line.get(row.journal_line_id, False) is money_in
        ]
        if group:
            adjustments.extend(
                fx_adjustment_lines(session, voucher, money_in=money_in, settlements=group)
            )
    return adjustments


def _to_posting_line(line: JournalLine, deposit_account_ids: set[int]) -> PostingLine:
    extended = tuple(
        ExtendedDimensionValue(dimension_id=int(dimension_id), value_id=value_id)
        for dimension_id, value_id in sorted((line.extended_dimensions or {}).items())
    )
    return PostingLine(
        account_id=line.account_id,
        corresponding_account_id=line.corresponding_account_id,
        debit_fc=line.debit_fc,
        credit_fc=line.credit_fc,
        currency=line.currency_code,
        rate=line.exchange_rate,
        dimensions=PostingDimensions(
            partner_id=line.partner_id,
            partner_kind=(
                PartnerKind(line.partner_kind) if line.partner_kind is not None else None
            ),
            cost_object_id=line.cost_object_id,
            project_id=line.project_id,
            order_id=line.order_id,
            contract_id=line.contract_id,
            expense_item_id=line.expense_item_id,
            item_id=line.item_id,
            warehouse_id=line.warehouse_id,
            bank_account_id=(
                line.bank_account_id if line.account_id in deposit_account_ids else None
            ),
            extended=extended,
        ),
        source_line_id=line.id,
        description=line.description,
    )
