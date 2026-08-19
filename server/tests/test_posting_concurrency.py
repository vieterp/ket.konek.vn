"""Đồng thời trên chứng từ (phase-04 bước 18, phần còn lại sau 4D).

Write-skew ghi-sổ ↔ khóa-sổ đã có bộ test riêng (`test_lock_vs_post_writeskew`).
Ở đây là hai bất biến còn lại của bước 18:

* Hai client ghi sổ HAI chứng từ khác nhau cùng kỳ → **cả hai thành công**:
  `FOR SHARE` trên hàng kỳ là khóa chia sẻ — các lượt ghi không chặn nhau,
  chỉ lượt khóa sổ (`FOR UPDATE`) mới xếp hàng sau chúng.
* Hai client sửa CÙNG một chứng từ từ cùng một phiên bản → một bên nhận xung
  đột phiên bản (tầng HTTP dịch thành `409` — đã có test middleware riêng).
"""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import RowVersionConflictError
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.engine.models import GlPosting
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
MAR_5 = date(2026, 3, 5)


@pytest.fixture
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


def _payload(context: PostingContext, description: str) -> JournalVoucherIn:
    return JournalVoucherIn(
        branch_id=context.branch_id,
        document_date=MAR_5,
        posting_date=MAR_5,
        currency_code="VND",
        exchange_rate=Decimal(1),
        description=description,
        lines=(
            JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(10_000)),
            JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(10_000)),
        ),
    )


def test_two_clients_posting_different_vouchers_in_the_same_period_both_succeed(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    voucher_ids: list[UUID] = []
    with unit_of_work(session_factory, scope) as session:
        service = JournalVoucherService(session)
        for name in ("client A", "client B"):
            voucher_ids.append(service.create(_payload(context, name), user_id=ACTOR_ID).id)

    # Rào chắn hai luồng để cả hai lượt ghi mở transaction trước khi lượt nào
    # commit. Test này chỉ canh yêu cầu bước 18 ("cả hai thành công") — khóa
    # độc quyền nhầm chỗ chỉ làm một bên xếp hàng chứ không đổ; tính CHIA SẺ
    # của khóa kỳ có test riêng bên dưới (probe giữ FOR SHARE đồng thời).
    barrier = threading.Barrier(2, timeout=30)
    errors: list[BaseException] = []

    def post_one(voucher_id: UUID) -> None:
        try:
            with unit_of_work(session_factory, scope) as session:
                barrier.wait()
                JournalVoucherService(session).post(voucher_id, user_id=ACTOR_ID)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=post_one, args=(vid,)) for vid in voucher_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    with unit_of_work(session_factory, scope) as session:
        statuses = (
            session.execute(select(Voucher.status).where(Voucher.id.in_(voucher_ids)))
            .scalars()
            .all()
        )
        assert statuses == [VoucherStatus.DA_GHI_SO.value, VoucherStatus.DA_GHI_SO.value]
        posted_lines = session.execute(
            select(func.count()).select_from(GlPosting).where(GlPosting.voucher_id.in_(voucher_ids))
        ).scalar_one()
        # 2 chứng từ × 2 dòng × 2 sổ.
        assert posted_lines == 8


def test_posting_takes_a_shared_lock_on_the_period_row_not_an_exclusive_one(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    """Khóa kỳ của lượt ghi sổ phải là FOR SHARE — probe phân biệt thật (RT-09).

    Phiên A giữ FOR SHARE trên hàng kỳ (đúng khóa mọi lượt ghi giữ); phiên B
    ghi sổ với `lock_timeout` ngắn. FOR SHARE tương thích FOR SHARE nên B phải
    xong ngay khi A còn giữ; đổi khóa của `PostingService.post` thành
    FOR UPDATE thì B kẹt sau khóa của A và vấp timeout — mutation review 4E
    (M7) mà test rào-chắn ở trên không phân biệt được.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        voucher = JournalVoucherService(session).create(
            _payload(context, "probe khóa chia sẻ"), user_id=ACTOR_ID
        )
        voucher_id, period_id = voucher.id, voucher.period_id

    with unit_of_work(session_factory, scope) as holder:
        held = holder.execute(
            select(AccountingPeriod.id)
            .where(AccountingPeriod.id == period_id)
            .with_for_update(read=True)
        ).scalar_one()
        assert held == period_id
        with unit_of_work(session_factory, scope) as writer:
            writer.execute(text("SET LOCAL lock_timeout = '2s'"))
            JournalVoucherService(writer).post(voucher_id, user_id=ACTOR_ID)

    with unit_of_work(session_factory, scope) as session:
        status = session.execute(
            select(Voucher.status).where(Voucher.id == voucher_id)
        ).scalar_one()
        assert status == VoucherStatus.DA_GHI_SO.value


def test_two_clients_editing_the_same_voucher_from_one_version_one_conflicts(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> None:
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        voucher = JournalVoucherService(session).create(
            _payload(context, "hai người cùng mở form"), user_id=ACTOR_ID
        )
        voucher_id, stale_version = voucher.id, voucher.row_version

    # Người thứ nhất lưu — phiên bản nhảy lên.
    with unit_of_work(session_factory, scope) as session:
        JournalVoucherService(session).update(
            voucher_id,
            _payload(context, "người thứ nhất lưu trước"),
            expected_row_version=stale_version,
            user_id=ACTOR_ID,
        )

    # Người thứ hai vẫn cầm phiên bản cũ → xung đột, không ghi đè âm thầm
    # (tầng HTTP dịch lỗi này thành 409 — test middleware riêng đã canh).
    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(RowVersionConflictError):
            JournalVoucherService(session).update(
                voucher_id,
                _payload(context, "người thứ hai lưu sau"),
                expected_row_version=stale_version,
                user_id=ACTOR_ID,
            )

    with unit_of_work(session_factory, scope) as session:
        description = session.execute(
            select(Voucher.description).where(Voucher.id == voucher_id)
        ).scalar_one()
        assert description == "người thứ nhất lưu trước"
