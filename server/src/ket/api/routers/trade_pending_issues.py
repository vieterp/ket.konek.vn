"""BFF tab "việc còn thiếu" của màn hình mua hàng và bán hàng (U1, lát 7G-4).

`GET /api/v1/purchase/pending-issues` và `GET /api/v1/sales/pending-issues`.

**Chỉ ĐỌC, và chỉ ở tầng api** — cùng lập luận với `cashflow.py`: mỗi tab đọc
ba nơi (`vouchers` + thân hóa đơn của module, `einvoices`, dataset công nợ
`ar_ap_open_items` của `receivables`), tức một màn hình đọc ≥2 module, đúng
điều kiện RT-21. Router module (`purchase.py`/`sales.py`) không ôm việc này vì
C3 cấm `sales` hỏi `einvoice` hay `receivables`, và tầng api là chỗ duy nhất
đứng trên cả ba.

Ba nhóm mỗi chiều, đúng ba "việc còn thiếu" design reference nhóm 01 nêu —
trừ "chưa nhập/xuất kho", chờ phase 8 (xem `PendingIssueCode`):

* `chua-ghi-so` — chứng từ Đã cất (`VoucherStatus.DA_CAT`). Trùng với nhóm
  `PUR`/`SAL` của `/vouchers/pending-issues` một cách CÓ CHỦ ĐÍCH: tab U1 của
  một màn hình phải là một lượt gọi, và cột "Việc tiếp theo" ở đây cần thêm
  đối tác, số tiền, hạn — thứ bảng tóm tắt chung của phase 4 không mang.
* `chua-co-hoa-don` — chứng từ ĐÃ GHI SỔ mà hóa đơn còn thiếu. Chiều mua: cột
  `vendor_invoice_status = NOT_YET` (hàng về trước, hóa đơn về sau, FR-PUR-006;
  `NONE` — mua của cá nhân — là "không có" chứ không "còn thiếu"). Chiều bán:
  không có tờ HĐĐT nào **còn sống** treo lên chứng từ — quyết định user
  2026-09-17: tờ `CHUA_PHAT_HANH`, `PHAT_HANH_LOI`, `DA_HUY` KHÔNG tính là có;
  tờ đang/đã phát hành, đã gửi, đã bị điều chỉnh thì có; tờ **đã bị thay thế** KHÔNG tính (review H-1: tờ thay thế treo lên chính chứng từ gốc, nên chứng từ có tờ cũ `DA_THAY_THE` + tờ mới còn nháp là chứng từ chưa có hóa đơn hợp lệ).
* `qua-han` — khoản còn nợ có hạn và hạn đã qua tại `as_of`, đọc qua
  `api/open_items.py` (dataset của báo cáo tuổi nợ; RLS lọc chi nhánh). Gồm cả
  nợ mang sang từ số dư ban đầu — không có chứng từ để mở nhưng vẫn là việc.

Quyền: `purchase.invoice.view` cho chiều mua, `sales.invoice.view` cho chiều
bán — xét trên đúng thứ dữ liệu trả về (chứng từ của module ấy). Nhóm
`chua-co-hoa-don` chiều bán chỉ lộ "có/không có tờ", không lộ nội dung tờ, nên
không đòi thêm quyền `einvoice`; nút "phát hành" thì client ẩn theo quyền của
chính nó.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import ColumnElement, Select, exists, func, select
from sqlalchemy.orm import Session, aliased

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.open_items import Direction, OpenItem, open_items
from ket.api.routers.trade_pending_issues_schemas import (
    NextAction,
    PendingIssueCode,
    TradePendingIssueGroup,
    TradePendingIssuesResponse,
    TradePendingVoucher,
)
from ket.kernel.contracts import PartnerKind
from ket.kernel.master_data.models.item import INVENTORY_NATURES, Item
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.einvoice.models import LIVE_STATUSES, EInvoice
from ket.modules.inventory.models import InventoryVoucher
from ket.modules.purchase import INVOICE_PERMISSION_CODE as PURCHASE_INVOICE_CODE
from ket.modules.purchase import PURCHASE_PERMISSION_MODULE
from ket.modules.purchase.models import (
    PurchaseInvoice,
    PurchaseInvoiceKind,
    PurchaseInvoiceLine,
    VendorInvoiceStatus,
)
from ket.modules.sales import INVOICE_PERMISSION_CODE as SALES_INVOICE_CODE
from ket.modules.sales import SALES_PERMISSION_MODULE
from ket.modules.sales.models import (
    KINDS_NEEDING_EINVOICE,
    SalesInvoice,
    SalesInvoiceKind,
    SalesInvoiceLine,
)
from ket.posting.documents.models import Voucher, VoucherStatus

router = APIRouter(tags=["pending-issues"])

PENDING_SAMPLE_LIMIT: Final[int] = 10
"""Cùng ngưỡng với `/vouchers/pending-issues`: tab là bảng dẫn đường, danh sách
đầy đủ nằm ở lưới chứng từ của màn hình."""

PurchaseReader = Annotated[
    AuthorizedRequest,
    Depends(
        require_permission(
            permission_code(PURCHASE_PERMISSION_MODULE, PURCHASE_INVOICE_CODE, Action.VIEW)
        )
    ),
]
SalesReader = Annotated[
    AuthorizedRequest,
    Depends(
        require_permission(
            permission_code(SALES_PERMISSION_MODULE, SALES_INVOICE_CODE, Action.VIEW)
        )
    ),
]

AsOf = Annotated[date | None, Query()]


@router.get("/api/v1/purchase/pending-issues", response_model=TradePendingIssuesResponse)
def purchase_pending_issues(
    authorized: PurchaseReader, factory: SessionFactory, as_of: AsOf = None
) -> TradePendingIssuesResponse:
    """Tab "việc còn thiếu" của màn hình mua hàng."""
    return _pending_issues(authorized, factory, side="purchase", as_of=as_of)


@router.get("/api/v1/sales/pending-issues", response_model=TradePendingIssuesResponse)
def sales_pending_issues(
    authorized: SalesReader, factory: SessionFactory, as_of: AsOf = None
) -> TradePendingIssuesResponse:
    """Tab "việc còn thiếu" của màn hình bán hàng."""
    return _pending_issues(authorized, factory, side="sales", as_of=as_of)


Side = Literal["purchase", "sales"]


def _pending_issues(
    authorized: AuthorizedRequest, factory: SessionFactory, *, side: Side, as_of: date | None
) -> TradePendingIssuesResponse:
    with unit_of_work(factory, authorized.scope) as session:
        # Ngày địa phương của máy chủ, cùng khuôn với `cashflow.py`: "hôm nay"
        # trên một tab việc-cần-làm là ngày làm việc, không phải ngày UTC.
        effective_as_of = as_of or datetime.now(UTC).astimezone().date()
        groups: list[TradePendingIssueGroup] = []

        unposted = _voucher_group(
            session,
            side=side,
            as_of=effective_as_of,
            code="chua-ghi-so",
            next_action="post",
            condition=Voucher.status == int(VoucherStatus.DA_CAT),
        )
        if unposted is not None:
            groups.append(unposted)

        if side == "purchase":
            missing = _voucher_group(
                session,
                side=side,
                as_of=effective_as_of,
                code="chua-co-hoa-don",
                next_action="attach-vendor-invoice",
                condition=(Voucher.status == int(VoucherStatus.DA_GHI_SO))
                & (PurchaseInvoice.vendor_invoice_status == VendorInvoiceStatus.NOT_YET),
            )
        else:
            live_einvoice = exists().where(
                EInvoice.source_voucher_id == Voucher.id,
                EInvoice.status.in_(LIVE_STATUSES),
            )
            missing = _voucher_group(
                session,
                side=side,
                as_of=effective_as_of,
                code="chua-co-hoa-don",
                next_action="issue-einvoice",
                condition=(Voucher.status == int(VoucherStatus.DA_GHI_SO))
                & SalesInvoice.kind.in_(KINDS_NEEDING_EINVOICE)
                & ~live_einvoice,
            )
        if missing is not None:
            groups.append(missing)

        stock = _voucher_group(
            session,
            side=side,
            as_of=effective_as_of,
            code="chua-nhap-kho" if side == "purchase" else "chua-xuat-kho",
            next_action="stock-in" if side == "purchase" else "stock-out",
            condition=_missing_stock_voucher_condition(side),
        )
        if stock is not None:
            groups.append(stock)

        overdue = _overdue_group(session, side=side, as_of=effective_as_of)
        if overdue is not None:
            groups.append(overdue)

        return TradePendingIssuesResponse(side=side, as_of=effective_as_of, groups=groups)


def _voucher_group(
    session: Session,
    *,
    side: Side,
    as_of: date,
    code: PendingIssueCode,
    next_action: NextAction,
    condition: ColumnElement[bool],
) -> TradePendingIssueGroup | None:
    """Một nhóm dựng từ thân chứng từ của module (hai nhóm đầu).

    Đếm và mẫu là HAI truy vấn trên cùng `condition`: mẫu cắt ở
    `PENDING_SAMPLE_LIMIT`, đếm thì không — tab hiện "23 việc" chứ không "10".
    """
    query = _voucher_query(side).where(condition)
    total = session.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    if total == 0:
        return None
    rows = session.execute(
        query.order_by(Voucher.posting_date, Voucher.voucher_no).limit(PENDING_SAMPLE_LIMIT)
    ).all()
    return TradePendingIssueGroup(
        code=code,
        count=int(total),
        next_action=next_action,
        sample=[
            TradePendingVoucher(
                voucher_id=row.voucher_id,
                source_label=_MODULE_LABEL[side],
                voucher_no=row.voucher_no,
                document_date=row.document_date,
                partner_id=row.partner_id,
                partner_code=row.partner_code,
                partner_name=row.partner_name,
                currency_code=row.currency_code,
                amount_fc=row.total_fc,
                due_date=row.due_date,
                days_overdue=_days_overdue(row.due_date, as_of),
            )
            for row in rows
        ],
    )


def _missing_stock_voucher_condition(side: Side) -> ColumnElement[bool]:
    """Đã ghi sổ, có dòng hàng qua kho (mã hàng `goods`/`finished_goods` kèm số
    lượng), mà không phiếu kho nào mang `source_document_id` trỏ về — lát 8A.

    Lọc phiếu sinh theo **thân `inventory_vouchers`**, không chỉ theo cột
    `source_document_id` của header: phase sau có thể ghi cùng cột ấy cho
    chứng từ loại khác (phiếu chi trả hóa đơn), và lúc đó "đã có gì đó trỏ về"
    không còn nghĩa là "đã nhập kho".
    """
    generated_header = aliased(Voucher)
    generated = exists().where(
        generated_header.source_document_id == Voucher.id,
        InventoryVoucher.id == generated_header.id,
    )
    if side == "purchase":
        stocked_line = exists().where(
            PurchaseInvoiceLine.voucher_id == Voucher.id,
            PurchaseInvoiceLine.quantity.is_not(None),
            PurchaseInvoiceLine.item_id == Item.id,
            Item.nature.in_(sorted(nature.value for nature in INVENTORY_NATURES)),
        )
        return (
            (Voucher.status == int(VoucherStatus.DA_GHI_SO))
            & (PurchaseInvoice.kind == PurchaseInvoiceKind.GOODS)
            & stocked_line
            & ~generated
        )
    stocked_line = exists().where(
        SalesInvoiceLine.voucher_id == Voucher.id,
        SalesInvoiceLine.quantity.is_not(None),
        SalesInvoiceLine.item_id == Item.id,
        Item.nature.in_(sorted(nature.value for nature in INVENTORY_NATURES)),
    )
    return (
        (Voucher.status == int(VoucherStatus.DA_GHI_SO))
        & SalesInvoice.kind.in_((SalesInvoiceKind.GOODS, SalesInvoiceKind.AGENCY))
        & stocked_line
        & ~generated
    )


def _voucher_query(side: Side) -> Select[Any]:
    """Header chứng từ + thân hóa đơn của module + đối tác, cho một chiều.

    Hai nhánh chứ không một hàm chung nhận model: `vendor_id`/`customer_id` là
    hai cột có tên khác nhau vì chúng LÀ hai vai khác nhau của cùng danh mục
    đối tác, và ép chúng vào một "partner column" trừu tượng là giấu đi điều đó.
    """
    if side == "purchase":
        return (
            select(
                Voucher.id.label("voucher_id"),
                Voucher.voucher_no,
                Voucher.document_date,
                Voucher.posting_date,
                Voucher.currency_code,
                PurchaseInvoice.vendor_id.label("partner_id"),
                Partner.code.label("partner_code"),
                Partner.name.label("partner_name"),
                PurchaseInvoice.total_fc,
                PurchaseInvoice.due_date,
            )
            .join(PurchaseInvoice, PurchaseInvoice.id == Voucher.id)
            .outerjoin(Partner, Partner.id == PurchaseInvoice.vendor_id)
        )
    return (
        select(
            Voucher.id.label("voucher_id"),
            Voucher.voucher_no,
            Voucher.document_date,
            Voucher.posting_date,
            Voucher.currency_code,
            SalesInvoice.customer_id.label("partner_id"),
            Partner.code.label("partner_code"),
            Partner.name.label("partner_name"),
            SalesInvoice.total_fc,
            SalesInvoice.due_date,
        )
        .join(SalesInvoice, SalesInvoice.id == Voucher.id)
        .outerjoin(Partner, Partner.id == SalesInvoice.customer_id)
    )


_MODULE_LABEL: Final[dict[Side, str]] = {"purchase": "Hóa đơn mua", "sales": "Hóa đơn bán"}
"""Cùng từ vựng với cột `source_label` của dataset — hai nhóm đầu và nhóm quá
hạn phải gọi một thứ bằng một tên."""

_DIRECTION_OF_SIDE: Final[dict[Side, Direction]] = {"purchase": "chi", "sales": "thu"}
_PARTNER_KIND_OF_SIDE: Final[dict[Side, PartnerKind]] = {
    "purchase": PartnerKind.VENDOR,
    "sales": PartnerKind.CUSTOMER,
}
_COLLECT_OR_PAY: Final[dict[Side, NextAction]] = {"purchase": "pay", "sales": "collect"}


def _overdue_group(session: Session, *, side: Side, as_of: date) -> TradePendingIssueGroup | None:
    """Nhóm quá hạn — đọc dataset công nợ, không đọc thân hóa đơn.

    Lọc theo LOẠI đối tác của chiều (khách hàng ↔ phải thu, nhà cung cấp ↔
    phải trả): dataset xếp "trả trước người bán" vào phía phải thu và "khách
    ứng trước" vào phía phải trả — đúng về kế toán, nhưng một khoản ta ứng cho
    NCC quá hạn không phải việc của màn hình BÁN HÀNG. Khoản treo lên nhân viên
    (kind 2) cũng không thuộc hai màn hình này — phase 9.
    """
    items: list[OpenItem] = [
        item
        for item in open_items(
            session,
            direction=_DIRECTION_OF_SIDE[side],
            as_of=as_of,
            due_state="qua-han",
        )
        if item.partner_kind == int(_PARTNER_KIND_OF_SIDE[side])
    ]
    if not items:
        return None
    # `open_items` đã sắp theo hạn sớm nhất — cắt là đủ.
    return TradePendingIssueGroup(
        code="qua-han",
        count=len(items),
        next_action=_COLLECT_OR_PAY[side],
        sample=[
            TradePendingVoucher(
                voucher_id=item.document_id,
                source_label=item.source_label,
                voucher_no=item.document_no,
                document_date=item.document_date,
                partner_id=item.partner_id,
                partner_code=item.partner_code,
                partner_name=item.partner_name,
                currency_code=item.currency_code,
                amount_fc=item.remaining_fc,
                due_date=item.due_date,
                days_overdue=item.days_overdue,
            )
            for item in items[:PENDING_SAMPLE_LIMIT]
        ],
    )


def _days_overdue(due_date: date | None, as_of: date) -> int | None:
    """Cùng định nghĩa với cột `days_overdue` của dataset: không hạn → `None`,
    chưa đến hạn → 0, quá hạn → số ngày."""
    if due_date is None:
        return None
    return max((as_of - due_date).days, 0)
