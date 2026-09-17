"""BFF màn hình đối tác: thông tin + thẻ công nợ (`/api/v1/partners/{id}/overview`).

Nợ H56 của phase 3, trả ở lát 7G-4. Phase 3 hoãn BFF này vì thẻ công nợ đọc
`ar_ap_ledger` của module `receivables` (phase 7); dựng lúc ấy là một BFF đọc
MỘT module — trái RT-21 — với phần công nợ là một trường rỗng mà UI phải đoán
cách vẽ. Nay đủ hai module: danh mục (`kernel.master_data`) + công nợ (dataset
`ar_ap_open_items` qua `api/open_items.py`).

**Chỉ ĐỌC**, cùng lập luận với `cashflow.py`. Sửa hồ sơ đi
`/api/v1/master/partners/{id}`, tài khoản ngân hàng đi `…/bank-accounts` —
hai đường ấy đã có từ 3D và màn hình vẫn dùng chúng; BFF này chỉ là lượt đọc
đầu tiên khi mở hồ sơ.

Quyền theo TỪNG nửa (quyết định user 2026-09-17): `master.partners.view` bắt
buộc cho cả endpoint (không có nó thì không có hồ sơ để xem); nửa phải thu
cần `sales.invoice.view`, nửa phải trả cần `purchase.invoice.view` — cùng trục
với báo cáo tuổi nợ (7B/7C đổi `required_permission_module` sang
`sales`/`purchase`) và cùng khuôn "quyền xét trên đúng thứ dữ liệu trả về" của
BFF quỹ/ngân hàng (họ lỗi 6B H-1).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.open_items import (
    Direction,
    OpenItem,
    debt_excluding_advances,
    open_items,
    summarize,
)
from ket.api.routers.master_data_guards import ensure_visible
from ket.api.routers.partner_overview_schemas import (
    DebtSide,
    PartnerDebtCard,
    PartnerInfo,
    PartnerOverviewResponse,
)
from ket.api.routers.partners import SPEC as PARTNER_SPEC
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.purchase import INVOICE_PERMISSION_CODE as PURCHASE_INVOICE_CODE
from ket.modules.purchase import PURCHASE_PERMISSION_MODULE
from ket.modules.sales import INVOICE_PERMISSION_CODE as SALES_INVOICE_CODE
from ket.modules.sales import SALES_PERMISSION_MODULE

router = APIRouter(prefix="/api/v1/partners", tags=["partners"])

PartnerReader = Annotated[
    AuthorizedRequest, Depends(require_permission(PARTNER_SPEC.permission_code(Action.VIEW)))
]

RECEIVABLE_VIEW: Final[str] = permission_code(
    SALES_PERMISSION_MODULE, SALES_INVOICE_CODE, Action.VIEW
)
PAYABLE_VIEW: Final[str] = permission_code(
    PURCHASE_PERMISSION_MODULE, PURCHASE_INVOICE_CODE, Action.VIEW
)


@router.get("/{partner_id}/overview", response_model=PartnerOverviewResponse)
def partner_overview(
    partner_id: int,
    authorized: PartnerReader,
    factory: SessionFactory,
    as_of: Annotated[date | None, Query()] = None,
) -> PartnerOverviewResponse:
    """Hồ sơ đối tác kèm thẻ công nợ tại `as_of` (mặc định hôm nay).

    Nhóm đối tác (`is_group`) cũng trả được: thẻ của nhóm là tổng của các đối
    tác trong nhóm? **Không** — dataset lọc theo `partner_id` đúng một bản ghi,
    nên thẻ của một nhóm là hai nửa rỗng (0 khoản). Cộng theo cây là báo cáo
    "tổng hợp theo nhóm khách hàng" (7G-2b), không phải việc của thẻ.
    """
    with unit_of_work(factory, authorized.scope) as session:
        partner = _load_partner(session, partner_id, authorized)
        # Ngày địa phương của máy chủ, cùng khuôn với `cashflow.py`.
        effective_as_of = as_of or datetime.now(UTC).astimezone().date()

        receivable_items = (
            _items(session, direction="thu", partner=partner, as_of=effective_as_of)
            if authorized.access.has(RECEIVABLE_VIEW)
            else None
        )
        payable_items = (
            _items(session, direction="chi", partner=partner, as_of=effective_as_of)
            if authorized.access.has(PAYABLE_VIEW)
            else None
        )
        # Vế trừ của ngưỡng nợ KHÔNG kể khoản ứng trước (target_kind 5/6) — cùng
        # luật với `partner_open_debt()` của guard: khoản ta trả trước người bán
        # đứng ở nửa phải thu (đúng, người xem phải thấy nó) nhưng không phải nợ
        # của khách, trừ nó vào ngưỡng là thẻ nói "còn được nợ" ít hơn mức guard
        # sẽ chặn. Phạm vi chi nhánh vẫn khác guard (RLS ↔ toàn công ty) — quyết
        # định của lát, ghi ở docstring đầu tệp.
        credit_available = (
            partner.credit_limit - debt_excluding_advances(receivable_items)
            if partner.credit_limit is not None and receivable_items is not None
            else None
        )
        return PartnerOverviewResponse(
            partner=_partner_info(partner),
            debt=PartnerDebtCard(
                as_of=effective_as_of,
                receivable=_side(receivable_items),
                payable=_side(payable_items),
                credit_limit=partner.credit_limit,
                credit_available=credit_available,
            ),
        )


def _load_partner(session: Session, partner_id: int, authorized: AuthorizedRequest) -> Partner:
    """Cùng cửa với router danh mục: không tồn tại hay thuộc chi nhánh khác đều
    là `404` (xem `ensure_visible`)."""
    service: MasterDataService[MasterDataRow] = MasterDataService(session, PARTNER_SPEC.model)
    record = service.get(partner_id)
    ensure_visible(record, authorized.scope.acting_branch_id, PARTNER_SPEC)
    if not isinstance(record, Partner):  # pragma: no cover - SPEC.model là Partner
        raise TypeError("Danh mục đối tác trỏ một model khác Partner")
    return record


def _partner_info(partner: Partner) -> PartnerInfo:
    """ORM → `PartnerInfo`, cùng phép chuyển `uid` → chuỗi với `to_response`
    của router danh mục."""
    values: dict[str, object] = {
        field: getattr(partner, field) for field in PartnerInfo.model_fields
    }
    values["uid"] = str(partner.uid)
    return PartnerInfo.model_validate(values)


def _items(
    session: Session, *, direction: Direction, partner: Partner, as_of: date
) -> list[OpenItem]:
    """Khoản còn treo của một chiều.

    Lọc theo `partner_id` mà KHÔNG ghim `partner_kind`: đối tác là MỘT bản ghi
    dùng chung mua + bán, và một khoản ta trả trước người bán (target_kind 6)
    nằm ở phía phải thu với `partner_kind = vendor` — ghim `customer` cho nửa
    phải thu là làm rơi đúng khoản ấy. Bỏ trống loại thì dataset tự thu về danh
    mục `partners` (kind 0/1), loại nhân viên (kind 2, không gian id khác) ra
    ngoài — xem chú thích `WHERE` cuối `ar_ap_open_items.sql`.
    """
    return list(open_items(session, direction=direction, as_of=as_of, partner_id=partner.id))


def _side(items: list[OpenItem] | None) -> DebtSide | None:
    """Một nửa thẻ; `None` khi người xem không có quyền với chiều đó."""
    if items is None:
        return None
    summary = summarize(items)
    return DebtSide(
        open_amount=summary.open_amount,
        open_count=summary.open_count,
        overdue_amount=summary.overdue_amount,
        overdue_count=summary.overdue_count,
        oldest_due_date=summary.oldest_due_date,
    )
