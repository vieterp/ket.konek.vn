"""Cân Nợ/Có — hai phép, hai lưới (BR-GLE-01, validator #1 và #2).

Phép thứ nhất cân trên **số quy đổi, từng sổ** — bất biến của sổ cái. Phép thứ
hai cân trên **nguyên tệ, từng loại tiền trong từng sổ** — lưới bắt lỗi tỷ giá:
một chứng từ USD lệch tỷ giá giữa dòng Nợ và dòng Có vẫn có thể cân sau quy đổi
nếu số "vừa khéo" bù nhau, nhưng nguyên tệ thì không nói dối được.

Thông điệp mang **số lệch** (success criteria phase-04: "nêu số lệch") — người
dùng nhìn thấy `120.000` và biết ngay dòng nào gõ thiếu một số 0.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from ket.kernel.errors import PostingViolation
from ket.posting.engine.prepared import PreparedLine


def check_balanced(lines: list[PreparedLine]) -> list[PostingViolation]:
    violations: list[PostingViolation] = []

    # Chứng từ mà mọi dòng đều 0 đồng "cân" một cách vô nghĩa — nó không ghi
    # nhận nghiệp vụ nào và chỉ làm nhiễu sổ (review 4A). Kiểm theo từng sổ vì
    # sổ quản trị rỗng có chủ đích là hợp lệ (danh sách rỗng ≠ dòng 0 đồng).
    totals: dict[int, Decimal] = defaultdict(Decimal)
    for line in lines:
        totals[line.ledger] += line.debit + line.credit
    for ledger, total in sorted(totals.items()):
        if total == 0:
            violations.append(
                PostingViolation(
                    "posting.zero_amount",
                    "Chứng từ không có số tiền nào — không có gì để ghi sổ",
                    ledger=ledger,
                )
            )

    converted: dict[int, Decimal] = defaultdict(Decimal)
    foreign: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    for line in lines:
        converted[line.ledger] += line.debit - line.credit
        foreign[(line.ledger, line.source.currency)] += line.source.debit_fc - line.source.credit_fc

    for ledger, gap in sorted(converted.items()):
        if gap != 0:
            violations.append(
                PostingViolation(
                    "posting.unbalanced",
                    f"Tổng Nợ lệch tổng Có {abs(gap)} trên số quy đổi",
                    ledger=ledger,
                    gap=str(gap),
                )
            )

    for (ledger, currency), gap in sorted(foreign.items()):
        if gap != 0:
            violations.append(
                PostingViolation(
                    "posting.unbalanced_fc",
                    f"Tổng Nợ lệch tổng Có {abs(gap)} {currency} trên nguyên tệ",
                    ledger=ledger,
                    currency=currency,
                    gap=str(gap),
                )
            )

    return violations
