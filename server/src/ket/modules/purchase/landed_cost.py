"""Phân bổ chi phí mua hàng vào từng dòng hóa đơn (FR-PUR, SRS 05 §4.2).

Thuần `Decimal`, không chạm DB — service gọi với tổng chi phí và danh sách
dòng, nhận về phần chi phí của từng dòng. Ba cách phân bổ (`LandedCostAllocation`):

* **Theo giá trị** — tỷ lệ với `amount_fc` của dòng.
* **Theo số lượng** — tỷ lệ với `quantity`; dòng không có số lượng nhận 0, và
  nếu không dòng nào có số lượng thì không có gì để chia theo → từ chối.
* **Thủ công** — người dùng nhập `landed_cost_fc` từng dòng; tổng phải khớp
  đúng tổng chi phí, không tự bù.

**Phần lẻ làm tròn dồn về dòng có trọng số lớn nhất** (dòng đầu tiên nếu
bằng nhau): làm tròn từng phần rồi cộng lại thì tổng lệch vài đồng so với tổng
chi phí, và bút toán Nợ hàng / Có chi phí không cân. Dồn về dòng lớn nhất là
cách làm phổ biến của kế toán VN (sai số tương đối nhỏ nhất) và là cách duy
nhất cho kết quả **xác định** không phụ thuộc thứ tự nhập.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.money import round_money
from ket.modules.purchase.models import LandedCostAllocation
from ket.posting.contracts import AMOUNT_SCALE

ALLOCATION_MISMATCH_CODE = "purchase.landed_cost_allocation_mismatch"
"""Phân bổ thủ công: tổng các dòng khác tổng chi phí mua hàng."""

ALLOCATION_NO_BASIS_CODE = "purchase.landed_cost_no_basis"
"""Phân bổ theo số lượng nhưng không dòng nào có số lượng."""

ALLOCATION_METHOD_UNKNOWN_CODE = "purchase.landed_cost_allocation_unknown"

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class AllocationLine:
    """Dữ liệu một dòng hóa đơn mà phép phân bổ cần — không hơn."""

    amount_fc: Decimal
    quantity: Decimal | None
    manual_fc: Decimal
    """`landed_cost_fc` client gửi — chỉ có nghĩa khi phân bổ thủ công."""


def allocate(
    *,
    method: int,
    total_fc: Decimal,
    lines: Sequence[AllocationLine],
    scale: int = AMOUNT_SCALE,
) -> tuple[Decimal, ...]:
    """Phần chi phí của từng dòng, cùng thứ tự với `lines`, tổng đúng bằng `total_fc`."""
    if method == LandedCostAllocation.MANUAL:
        return _manual(total_fc, lines, scale)
    if method == LandedCostAllocation.BY_VALUE:
        weights = [line.amount_fc for line in lines]
    elif method == LandedCostAllocation.BY_QUANTITY:
        weights = [line.quantity if line.quantity is not None else _ZERO for line in lines]
    else:
        raise PostingValidationError(
            "Cách phân bổ chi phí mua hàng không hợp lệ",
            violations=[
                PostingViolation(
                    ALLOCATION_METHOD_UNKNOWN_CODE,
                    "Chọn một trong ba cách: theo giá trị, theo số lượng, thủ công",
                    method=str(method),
                )
            ],
        )
    return _proportional(total_fc, weights, scale)


def _manual(total_fc: Decimal, lines: Sequence[AllocationLine], scale: int) -> tuple[Decimal, ...]:
    shares = tuple(round_money(line.manual_fc, scale) for line in lines)
    entered = sum(shares, _ZERO)
    if entered != round_money(total_fc, scale):
        raise PostingValidationError(
            "Tổng chi phí phân bổ thủ công không khớp tổng chi phí mua hàng",
            violations=[
                PostingViolation(
                    ALLOCATION_MISMATCH_CODE,
                    "Sửa phần chi phí trên các dòng cho tổng đúng bằng tổng chi phí mua hàng",
                    entered_fc=str(entered),
                    total_fc=str(total_fc),
                )
            ],
        )
    return shares


def _proportional(total_fc: Decimal, weights: Sequence[Decimal], scale: int) -> tuple[Decimal, ...]:
    total_fc = round_money(total_fc, scale)
    if not weights or total_fc == _ZERO:
        return tuple(_ZERO for _ in weights)
    basis = sum(weights, _ZERO)
    if basis <= _ZERO:
        raise PostingValidationError(
            "Không có cơ sở để phân bổ chi phí mua hàng",
            violations=[
                PostingViolation(
                    ALLOCATION_NO_BASIS_CODE,
                    "Phân bổ theo số lượng cần ít nhất một dòng có số lượng — "
                    "hoặc chọn phân bổ theo giá trị",
                )
            ],
        )
    shares = [round_money(total_fc * weight / basis, scale) for weight in weights]
    remainder = total_fc - sum(shares, _ZERO)
    if remainder != _ZERO:
        largest = max(range(len(weights)), key=lambda index: (weights[index], -index))
        shares[largest] += remainder
    return tuple(shares)


@dataclass(frozen=True, slots=True)
class CostLinePiece:
    """Một mẩu "khoản chi phí `cost_index` → dòng hàng `line_index`, số `amount_fc`"."""

    cost_index: int
    line_index: int
    amount_fc: Decimal


def pair_costs_with_lines(
    cost_amounts: Sequence[Decimal], line_shares: Sequence[Decimal]
) -> tuple[CostLinePiece, ...]:
    """Chẻ hai cách chia của CÙNG một tổng — theo khoản chi phí và theo dòng
    hàng — thành các mẩu (khoản, dòng, số tiền) sao cho mỗi mẩu là một cặp
    Nợ/Có cùng số nguyên tệ.

    Vì sao cần: engine quy đổi **từng dòng** ghi sổ rồi mới kiểm cân, nên "một
    dòng Có tổng khoản + nhiều dòng Nợ phần phân bổ" chỉ cân khi tỷ giá là số
    nguyên — `Σ round(phần_i × tỷ giá)` nói chung khác `round(tổng × tỷ giá)`.
    Chẻ thành cặp cùng số thì hai vế của mỗi cặp làm tròn giống hệt nhau, và
    tổng theo TK ở hai phía vẫn đúng bằng tổng khoản / tổng phần phân bổ.

    Cách chẻ: đi song song hai dãy theo thứ tự nhập, mỗi bước lấy `min(phần
    còn lại của khoản, phần còn lại của dòng)` — thuần số học, không làm tròn,
    tối đa `K + N − 1` mẩu. Khi có nhiều khoản, mẩu nào thuộc khoản nào là
    theo thứ tự chứ không phải một sự thật kế toán (số phân bổ của từng dòng
    là `line_shares`, đã chốt); chỉ tổng theo TK và theo chiều là có nghĩa.

    Tiền điều kiện: hai dãy cùng tổng (`allocate` bảo đảm) — lệch là dữ liệu
    đã hỏng, nổ chứ không bù.
    """
    if sum(cost_amounts, _ZERO) != sum(line_shares, _ZERO):
        raise RuntimeError(
            f"Tổng chi phí mua hàng {sum(cost_amounts, _ZERO)} khác tổng phần phân bổ "
            f"{sum(line_shares, _ZERO)}"
        )
    pieces: list[CostLinePiece] = []
    costs = [(index, amount) for index, amount in enumerate(cost_amounts) if amount > _ZERO]
    lines = [(index, share) for index, share in enumerate(line_shares) if share > _ZERO]
    cost_at = line_at = 0
    cost_left = costs[0][1] if costs else _ZERO
    line_left = lines[0][1] if lines else _ZERO
    while cost_at < len(costs) and line_at < len(lines):
        amount = min(cost_left, line_left)
        pieces.append(
            CostLinePiece(
                cost_index=costs[cost_at][0], line_index=lines[line_at][0], amount_fc=amount
            )
        )
        cost_left -= amount
        line_left -= amount
        if cost_left == _ZERO:
            cost_at += 1
            cost_left = costs[cost_at][1] if cost_at < len(costs) else _ZERO
        if line_left == _ZERO:
            line_at += 1
            line_left = lines[line_at][1] if line_at < len(lines) else _ZERO
    return tuple(pieces)
