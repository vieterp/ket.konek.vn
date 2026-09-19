"""Hình dạng response của tab "việc còn thiếu" mua/bán (U1, lát 7G-4).

Một bộ schema cho CẢ HAI chiều: màn hình mua dùng lại bộ xương của màn hình
bán (design reference nhóm 01), nên client dựng một component cho hai
endpoint — hai shape sẽ là hai component, và chúng lệch nhau ở lần sửa đầu.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

PendingIssueCode = Literal[
    "chua-ghi-so", "chua-co-hoa-don", "chua-nhap-kho", "chua-xuat-kho", "qua-han"
]
"""Mã nhóm, giống nhau hai chiều để client ánh xạ nhãn theo `(chiều, mã)`:
`qua-han` là "quá hạn thanh toán" ở màn mua và "quá hạn thu tiền" ở màn bán.

Nhóm "chưa nhập kho" / "chưa xuất kho" (lát 8A): chứng từ đã ghi sổ có dòng
hàng qua kho mà không phiếu kho nào sinh kèm — phiếu sinh tự động cho dòng có
kho (`InventoryPosting`), nên nhóm này thực chất là "dòng hàng thiếu kho" (mua)
và "chưa bật kiêm phiếu xuất kho" (bán). Trước 8A hai nhóm **cố ý vắng mặt**:
một nhóm luôn đếm 0 là một lời hứa mà hệ thống chưa giữ được."""

NextAction = Literal[
    "post", "attach-vendor-invoice", "issue-einvoice", "stock-in", "stock-out", "pay", "collect"
]
"""Mã máy cho ô "Việc tiếp theo" — nhãn tiếng Việt thuộc tầng i18n của client,
cùng luật với `PendingIssueGroupResponse.next_action` của phase 4."""


class TradePendingVoucher(BaseModel):
    """Một chứng từ trong nhóm — đủ để UI mở đúng chứng từ và nói "của ai,
    bao nhiêu, hạn nào".

    `amount_fc` theo NGUYÊN TỆ của chứng từ, kèm `currency_code`: ở nhóm
    `qua-han` là phần **còn nợ** tại `as_of` (cùng con số với báo cáo tuổi nợ),
    ở hai nhóm kia là tổng thanh toán của chứng từ. Thân hóa đơn mua/bán chỉ giữ
    tổng nguyên tệ (VND là chuyện của từng dòng định khoản), nên đây là trục
    duy nhất mà cả ba nhóm nói cùng một thứ. Với nợ mang sang từ số dư ban đầu
    `voucher_id` là `None` — không có chứng từ để mở, nhưng khoản nợ vẫn là việc
    phải làm.
    """

    voucher_id: UUID | None
    source_label: str
    """Nguồn khoản nợ bằng tiếng người, cột `source_label` của dataset — client
    dựa vào đây để mở đúng màn hình: "Phải thu ghi tay" trỏ chứng từ GLE, không
    phải hóa đơn bán. Với hai nhóm dựng từ thân hóa đơn, nó là nhãn của module."""
    voucher_no: str | None
    document_date: date | None
    """Cả hai `None` với khoản ứng trước đầu kỳ (không phải hóa đơn nào cả)."""
    partner_id: int
    partner_code: str | None
    partner_name: str | None
    currency_code: str
    amount_fc: Decimal
    due_date: date | None
    days_overdue: int | None


class TradePendingIssueGroup(BaseModel):
    """Một nhóm việc: đếm đủ, nêu đích danh tới `PENDING_SAMPLE_LIMIT` chứng từ."""

    code: PendingIssueCode
    count: int
    next_action: NextAction
    sample: list[TradePendingVoucher]


class TradePendingIssuesResponse(BaseModel):
    """Tab U1 của một chiều. Nhóm không có việc thì KHÔNG xuất hiện, cùng luật
    với `/vouchers/pending-issues`: tab là danh sách việc, không phải bảng
    trạng thái."""

    side: Literal["purchase", "sales"]
    as_of: date
    groups: list[TradePendingIssueGroup]
