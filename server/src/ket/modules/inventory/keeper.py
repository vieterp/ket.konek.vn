"""Hàng đợi thủ kho + sổ kho / thẻ kho (SRS 17 §3.2, FR-WHK-010..014/020/021,
lát 8D) — khuôn thủ quỹ 6C, một chỗ khác biệt cấu trúc.

Thủ quỹ phải bắc Protocol qua kernel vì sổ quỹ (`warehousing`) và phiếu thu/chi
(`cash_book`) ở HAI module. Thủ kho thì không: `inventory_vouchers` và
`warehouse_book` cùng thuộc module `inventory`, nên một Protocol ở đây chỉ là
nghi thức — và đặt tệp này sang `warehousing` sẽ làm nó phải import `inventory`,
đúng thứ luật phụ thuộc #1 cấm (`import-linter` C3).

Phân vai:

* `sync_after_post` — sau khi phiếu kho ghi sổ kế toán: xếp hàng đợi
  (`keeper_status = PENDING`) khi phân hệ **bật**, hoặc vào thẳng sổ kho theo
  ngày hạch toán khi **tắt** (FR-WHK-021), cùng transaction;
* `book_vouchers` — thủ kho Ghi sổ kho hàng loạt, **một transaction** cho cả lô
  (FR-WHK-012: "chọn nhiều phiếu → Ghi sổ" là MỘT thao tác; nửa danh sách vào
  sổ nửa báo lỗi là trạng thái không ai đối chiếu nổi);
* `clear_after_unpost` — bỏ ghi sổ kế toán thì gỡ dòng sổ kho và trả phiếu về
  hàng đợi.

Quyền của vai Thủ kho chỉ có `view`/`post` (xem `__init__`): thủ kho không sửa
được chứng từ kế toán bằng THIẾT KẾ bộ quyền, không bằng kỷ luật gán
(FR-WHK-020, khuôn 6C).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, literal, select, tuple_
from sqlalchemy.orm import Session

from ket.kernel.config.catalog import WAREHOUSE_KEEPER_ENABLED_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import KeeperBookDateInvalidError, KeeperVoucherStateError
from ket.modules.inventory.models import (
    InventoryMovement,
    InventoryVoucher,
    KeeperStatus,
    MovementDirection,
    WarehouseBookEntry,
)
from ket.posting.contracts import Voucher
from ket.posting.documents.models import VoucherStatus

_ZERO = Decimal(0)


def keeper_enabled(session: Session, *, user_id: int) -> bool:
    return value_of(session, key=WAREHOUSE_KEEPER_ENABLED_KEY, user_id=user_id) is True


# ------------------------------------------------------------------ vòng đời


def sync_after_post(session: Session, voucher_id: UUID, user_id: int) -> None:
    """Sau khi phiếu kho ghi sổ kế toán — gọi từ hook `after_post` của module."""
    body = session.get(InventoryVoucher, voucher_id)
    if body is None:  # pragma: no cover - hook chỉ chạy cho phiếu kho
        raise RuntimeError(f"Phiếu kho {voucher_id} thiếu thân")
    if keeper_enabled(session, user_id=user_id):
        body.keeper_status = KeeperStatus.PENDING
        return
    voucher = session.get(Voucher, voucher_id)
    if voucher is None:  # pragma: no cover - hook chạy ngay sau post cùng scope
        raise RuntimeError(f"Phiếu kho {voucher_id} thiếu header")
    _write_book(session, voucher, book_date=voucher.posting_date, user_id=user_id)
    # Không đóng dấu `keeper_*` ở đường phân-hệ-tắt (khuôn thủ quỹ 6C): bộ dấu
    # trên thân phiếu nói "THỦ KHO đã ghi", và CHECK `keeper_booked_has_date`
    # buộc nó chỉ đi với `BOOKED`. Ai ghi, lúc nào thì nằm trên chính dòng sổ
    # kho (`warehouse_book.posted_by/posted_at`).
    body.keeper_status = KeeperStatus.NOT_APPLICABLE
    session.flush()


def clear_after_unpost(session: Session, voucher_id: UUID) -> None:
    """Bỏ ghi sổ kế toán: gỡ dòng sổ kho, phiếu về hàng đợi.

    Về `PENDING` chứ không về `NOT_APPLICABLE` kể cả khi phân hệ đang tắt: lượt
    ghi sổ kế toán tiếp theo sẽ đặt lại trạng thái đúng theo cờ lúc ấy, còn
    `PENDING` là trạng thái an toàn hơn cho một phiếu đang nháp (nó xuất hiện
    trong hàng đợi thay vì biến mất khỏi mọi màn hình).
    """
    session.execute(delete(WarehouseBookEntry).where(WarehouseBookEntry.voucher_id == voucher_id))
    body = session.get(InventoryVoucher, voucher_id)
    if body is not None:
        body.keeper_status = KeeperStatus.PENDING
        body.keeper_book_date = None
        body.keeper_posted_at = None
        body.keeper_posted_by = None
    session.flush()


# ------------------------------------------------------------------ hàng đợi


def pending_queue(session: Session) -> list[tuple[Voucher, InventoryVoucher]]:
    """Phiếu đã ghi sổ kế toán, chờ thủ kho (FR-WHK-010) — RLS lọc chi nhánh.

    Không đòi phân hệ bật: tắt giữa chừng vẫn còn phiếu treo trạng thái chờ, và
    màn hình phải nhìn thấy chúng để xử lý nốt thay vì mất dấu (review 6C M-1).
    """
    rows = session.execute(
        select(Voucher, InventoryVoucher)
        .join(InventoryVoucher, InventoryVoucher.id == Voucher.id)
        .where(
            Voucher.status == int(VoucherStatus.DA_GHI_SO),
            InventoryVoucher.keeper_status == KeeperStatus.PENDING,
        )
        .order_by(Voucher.posting_date, Voucher.voucher_no)
    ).all()
    return [(row[0], row[1]) for row in rows]


def book_vouchers(
    session: Session,
    *,
    voucher_ids: Sequence[UUID],
    book_date: date | None,
    user_id: int,
    today: date | None = None,
) -> int:
    """Ghi sổ kho hàng loạt (FR-WHK-012) — một transaction, trả số dòng sổ kho.

    `book_date=None` = theo ngày hạch toán từng phiếu; một ngày cụ thể áp cho cả
    lô, phải ≥ ngày hạch toán của TỪNG phiếu và ≤ hôm nay — sổ kho ghi việc ĐÃ
    làm, không ghi trước việc chưa làm (khuôn BR-WHK-05 của thủ quỹ).

    Khóa theo thứ tự **ổn định** (sort id) chứ không theo thứ tự người gọi đưa:
    hai lô đồng thời chọn chồng nhau theo hai thứ tự ngược nhau là deadlock
    (review 6C LOW-4).
    """
    if not voucher_ids:
        raise KeeperVoucherStateError("Chưa chọn phiếu nào để ghi sổ kho")
    if len(set(voucher_ids)) != len(voucher_ids):
        raise KeeperVoucherStateError(
            "Danh sách phiếu ghi sổ kho có phiếu trùng lặp", count=len(voucher_ids)
        )
    # Ngày ĐỊA PHƯƠNG có chủ đích (DTZ011) — hệ chạy LAN cùng múi giờ người dùng
    # (LD-01); lấy UTC là từ chối oan mọi lượt ghi sổ trước 7 giờ sáng.
    clock_today = today if today is not None else date.today()  # noqa: DTZ011
    if book_date is not None and book_date > clock_today:
        raise KeeperBookDateInvalidError(
            "Ngày ghi sổ kho không được muộn hơn hôm nay",
            book_date=book_date.isoformat(),
            today=clock_today.isoformat(),
        )
    written = 0
    for voucher_id in sorted(voucher_ids):
        written += _book_one(session, voucher_id=voucher_id, book_date=book_date, user_id=user_id)
    return written


def _book_one(session: Session, *, voucher_id: UUID, book_date: date | None, user_id: int) -> int:
    voucher = session.execute(
        select(Voucher).where(Voucher.id == voucher_id).with_for_update()
    ).scalar_one_or_none()
    body = session.get(InventoryVoucher, voucher_id)
    if voucher is None or body is None:
        raise KeeperVoucherStateError(
            "Không tìm thấy phiếu kho để ghi sổ kho", voucher_id=str(voucher_id)
        )
    if voucher.status != int(VoucherStatus.DA_GHI_SO):
        raise KeeperVoucherStateError(
            "Sổ kho chỉ nhận phiếu đã ghi sổ kế toán",
            voucher_id=str(voucher_id),
            voucher_no=voucher.voucher_no,
        )
    if body.keeper_status != KeeperStatus.PENDING:
        raise KeeperVoucherStateError(
            "Phiếu này không nằm trong hàng đợi thủ kho",
            voucher_id=str(voucher_id),
            voucher_no=voucher.voucher_no,
            keeper_status=body.keeper_status,
        )
    effective = book_date if book_date is not None else voucher.posting_date
    if effective < voucher.posting_date:
        raise KeeperBookDateInvalidError(
            "Ngày ghi sổ kho không được sớm hơn ngày hạch toán của phiếu",
            voucher_id=str(voucher_id),
            voucher_no=voucher.voucher_no,
            book_date=effective.isoformat(),
            posting_date=voucher.posting_date.isoformat(),
        )
    written = _write_book(session, voucher, book_date=effective, user_id=user_id)
    body.keeper_status = KeeperStatus.BOOKED
    body.keeper_book_date = effective
    body.keeper_posted_at = datetime.now().astimezone()
    body.keeper_posted_by = user_id
    session.flush()
    return written


def _write_book(session: Session, voucher: Voucher, *, book_date: date, user_id: int) -> int:
    """Một dòng sổ kho cho mỗi movement của phiếu.

    Sổ kho là sổ **số lượng** của thủ kho: hàng giữ hộ vào sổ như hàng của mình
    (thủ kho giữ cả hai trên cùng một kệ, BR-STK-07 nói về giá trị), còn giá vốn
    thì không có ở đây — đó là sổ kế toán kho.
    """
    movements = (
        session.execute(
            select(InventoryMovement)
            .where(InventoryMovement.voucher_id == voucher.id)
            .order_by(InventoryMovement.id)
        )
        .scalars()
        .all()
    )
    for movement in movements:
        session.add(
            WarehouseBookEntry(
                branch_id=movement.branch_id,
                warehouse_id=movement.warehouse_id,
                item_id=movement.item_id,
                lot_id=movement.lot_id,
                book_date=book_date,
                voucher_id=voucher.id,
                in_qty=(movement.quantity if movement.direction == MovementDirection.IN else _ZERO),
                out_qty=(
                    movement.quantity if movement.direction == MovementDirection.OUT else _ZERO
                ),
                posted_by=user_id,
            )
        )
    session.flush()
    return len(movements)


# ------------------------------------------------------------------ sổ / thẻ


def book_rows(
    session: Session,
    *,
    warehouse_id: int | None,
    item_id: int | None,
    from_date: date | None,
    to_date: date | None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[Sequence[WarehouseBookEntry], int]:
    """Sổ kho của thủ kho (FR-WHK-014) — dòng thô theo ngày ghi sổ, kèm TỔNG.

    Tồn lũy kế là việc của tầng đọc / bản in (thẻ kho `card_rows` bên dưới):
    service trả dữ liệu, không trả cách trình bày (khuôn `cash_book_rows` 6C).
    """
    conditions = []
    if warehouse_id is not None:
        conditions.append(WarehouseBookEntry.warehouse_id == warehouse_id)
    if item_id is not None:
        conditions.append(WarehouseBookEntry.item_id == item_id)
    if from_date is not None:
        conditions.append(WarehouseBookEntry.book_date >= from_date)
    if to_date is not None:
        conditions.append(WarehouseBookEntry.book_date <= to_date)
    total = session.execute(
        select(func.count()).select_from(WarehouseBookEntry).where(*conditions)
    ).scalar_one()
    query = (
        select(WarehouseBookEntry)
        .where(*conditions)
        .order_by(WarehouseBookEntry.book_date, WarehouseBookEntry.id)
        .offset(offset)
    )
    if limit is not None:
        query = query.limit(limit)
    return session.execute(query).scalars().all(), total


def card_opening_qty(
    session: Session,
    *,
    warehouse_id: int,
    item_id: int,
    before_date: date | None,
    before_id: int | None,
) -> Decimal:
    """Tồn trên sổ kho đứng TRƯỚC dòng đầu của trang đang xem — con số mở đầu
    của thẻ kho.

    Mốc là `(book_date, id)` của dòng đầu trang chứ không phải `from_date`: thẻ
    kho có phân trang, và trang 2 mà mở đầu bằng tồn trước `from_date` sẽ vẽ
    một đường tồn bắt đầu lại giữa chừng. Trang rỗng (`before_id=None`) thì mở
    đầu là tồn trước `before_date`, hoặc 0 khi không lọc ngày.
    """
    conditions = [
        WarehouseBookEntry.warehouse_id == warehouse_id,
        WarehouseBookEntry.item_id == item_id,
    ]
    if before_id is not None and before_date is not None:
        conditions.append(
            tuple_(WarehouseBookEntry.book_date, WarehouseBookEntry.id)
            < tuple_(literal(before_date), literal(before_id))
        )
    elif before_date is not None:
        conditions.append(WarehouseBookEntry.book_date < before_date)
    else:
        return _ZERO
    total = session.scalar(
        select(
            func.coalesce(func.sum(WarehouseBookEntry.in_qty - WarehouseBookEntry.out_qty), _ZERO)
        ).where(*conditions)
    )
    return Decimal(total or 0)
