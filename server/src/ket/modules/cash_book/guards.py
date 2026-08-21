"""`CashBalanceGuard` — "Chi quá số tồn tiền mặt/tiền gửi" (FR-QUY-020, FR-SYS-062).

Bản cài `PostingGuard` đầu tiên. Điều kiện kích hoạt theo `docs/srs/01` §8.2:
**số dư TK 111x/112x < 0 sau khi ghi sổ** — nên guard soi MỌI chứng từ chạm
các TK đó (phiếu chi, ủy nhiệm chi 6C, cả chứng từ GLE tự định khoản vào 111x),
không riêng gì phiếu của module này. Đăng ký vào `GUARD_REGISTRY` lúc import
gói `cash_book` — posting không import module (C4).

Ba mức đọc từ khóa `warning.cash_balance` (catalog FR-SYS-060): guard tự đọc
cấu hình của nó, đúng phân vai của pipeline (`posting/engine/guards.py`).

Số dư soi bằng `cash_balance_floor_from` — số dư THẤP NHẤT từ ngày hạch toán
của chứng từ tới hết năm, cộng nhẩm phần chứng từ sắp ghi (không có chuyện
ghi thử rồi xóa). Lấy floor chứ không lấy số dư tại một ngày (review 6B, M-2):
một phiếu chi lùi ngày trừ vào số dư của MỌI ngày về sau — nó có thể không âm
tại chính ngày đó nhưng làm âm một ngày sau, nơi quỹ đã bị các phiếu khác rút
xuống.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.orm import Session

from ket.kernel.config.accounts_provider import accounts_by_id
from ket.kernel.config.catalog import (
    CASH_BALANCE_WARNING_KEY,
    WARNING_LEVEL_BLOCK,
    WARNING_LEVEL_NONE,
)
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import PostingViolation
from ket.modules.cash_book.balance_service import cash_balance_floor_from
from ket.posting.contracts import GuardFinding, Voucher
from ket.posting.engine.prepared import PreparedLine

CASH_BALANCE_NEGATIVE_CODE = "cash.balance_negative"

CASH_ACCOUNT_CODE_PREFIXES = ("111", "112")
"""Nhóm TK tiền mặt/tiền gửi — soi theo TIỀN TỐ số hiệu, không theo danh sách id.

Đây là literal số hiệu TK **có chủ đích**, đúng loại mà doctrine "không
hard-code hệ TK" (SRS 19 §9 #1) cho phép đặt tên ở một hằng: điều kiện kích
hoạt của cảnh báo này do CHÍNH SRS định nghĩa bằng số hiệu ("Số dư TK
111x/112x < 0" — `docs/srs/01` §8.2), và nhóm 111/112 = tiền là bất biến chung
của cả TT99 lẫn TT133 — không phải một đích hạch toán cấu hình được như 641/642.
Nếu một chế độ kế toán tương lai đổi nhóm TK tiền, chỗ sửa là MỘT hằng này."""

CASH_ON_HAND_PREFIX = CASH_ACCOUNT_CODE_PREFIXES[0]
"""Riêng tiền mặt tại quỹ (111x) — kiểm kê quỹ chỉ áp cho két tiền, không áp
cho tài khoản ngân hàng."""


class CashBalanceGuard:
    """Soi số dư quỹ/tiền gửi âm sau ghi sổ — cắm vào `PostingService`."""

    def check(
        self,
        session: Session,
        *,
        voucher: Voucher,
        lines: Sequence[PreparedLine],
    ) -> Sequence[GuardFinding]:
        level = value_of(session, key=CASH_BALANCE_WARNING_KEY, user_id=voucher.created_by)
        if level == WARNING_LEVEL_NONE:
            return ()

        accounts = accounts_by_id(session, [line.source.account_id for line in lines])
        # (sổ, số hiệu TK) → tổng Nợ − Có (VND) mà chứng từ sắp ghi thêm.
        deltas: dict[tuple[int, str], Decimal] = {}
        for line in lines:
            account = accounts.get(line.source.account_id)
            if account is None or not account.code.startswith(CASH_ACCOUNT_CODE_PREFIXES):
                continue
            key = (line.ledger, account.code)
            deltas[key] = deltas.get(key, Decimal(0)) + line.debit - line.credit

        findings: list[GuardFinding] = []
        blocking = level == WARNING_LEVEL_BLOCK
        for (ledger, account_code), delta in sorted(deltas.items()):
            floor = cash_balance_floor_from(
                session,
                ledger=ledger,
                branch_id=voucher.branch_id,
                account_code=account_code,
                from_date=voucher.posting_date,
            )
            projected = floor + delta
            if projected >= 0:
                continue
            findings.append(
                GuardFinding(
                    violation=PostingViolation(
                        CASH_BALANCE_NEGATIVE_CODE,
                        f"Ghi sổ xong thì TK {account_code} âm "
                        f"{-projected:,.0f} — chi quá số tồn (FR-SYS-062)",
                        account_code=account_code,
                        ledger=ledger,
                        projected=str(projected),
                    ),
                    blocking=blocking,
                )
            )
        return findings
