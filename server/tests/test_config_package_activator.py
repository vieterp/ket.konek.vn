"""`kernel/config/packages/activator.activate` — cổng FR-SYS-004 (RT-09).

Dataset riêng cho tệp này (không dùng `dataset_alpha` dùng chung): kích hoạt
gói ghi `activated_at`/`activated_by` lên đúng dòng `config_packages` builtin,
và test không được để lại dấu vết đó trên dataset mà các tệp test khác cũng
đọc.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import ConfigPackage
from ket.kernel.config.packages.activator import activate
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.errors import AccountingSchemeLockedError, ConfigPackageIdUnknownError
from ket.kernel.periods.models import AccountingPeriod
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.service import JournalVoucherService
from ket.posting.documents.models import Voucher
from posting_support import PostingContext, posting_scope, seed_posting_context

pytestmark = pytest.mark.db

ACTOR_ID = 1
DATASET_CODE = "cfgpkg_activator"


@pytest.fixture(scope="module")
def dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    ref = provision_dataset(owner_engine, code=DATASET_CODE, name="Kích hoạt gói", scheme="TT99")
    yield ref
    drop_dataset_schema(owner_engine, DATASET_CODE)


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset)


def _scope(dataset: DatasetRef, *, branch_ids: tuple[int, ...] = ()) -> RequestScope:
    """`branch_ids` rỗng cho thao tác trên `config_packages` (không RLS chi
    nhánh). Đọc `vouchers` (có RLS) thì **phải** truyền chi nhánh thật —
    thiếu nó, RLS coi phạm vi rỗng là "không thấy dòng nào" chứ không phải
    "thấy tất cả", nên chứng từ vừa tạo sẽ vô hình với chính truy vấn kiểm.
    """
    return RequestScope(dataset_schema=dataset.schema_name, user_id=ACTOR_ID, branch_ids=branch_ids)


def _package_id(session: Session, code: str) -> int:
    package_id = session.scalar(select(ConfigPackage.id).where(ConfigPackage.code == code))
    assert package_id is not None, f"gói builtin {code} chưa được gieo"
    return package_id


def _create_voucher(
    session_factory: sessionmaker[Session], dataset: DatasetRef, context: PostingContext
) -> None:
    """Một chứng từ tối thiểu — đủ để `vouchers.period_id` trỏ vào năm tài chính
    của `context` (chưa cần ghi sổ: `create()` đã đủ cho FR-SYS-004 "đã phát sinh")."""
    scope = posting_scope(dataset, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        JournalVoucherService(session).create(
            JournalVoucherIn(
                branch_id=context.branch_id,
                document_date=date(2026, 1, 15),
                posting_date=date(2026, 1, 15),
                currency_code="VND",
                exchange_rate=Decimal(1),
                description="chứng từ test kích hoạt gói",
                lines=(
                    JournalLineIn(account_id=context.accounts["642"], debit_fc=Decimal(50_000)),
                    JournalLineIn(account_id=context.accounts["111"], credit_fc=Decimal(50_000)),
                ),
            ),
            user_id=ACTOR_ID,
        )


def _fiscal_year_ids_with_vouchers(session: Session) -> tuple[int, ...]:
    rows = session.execute(
        select(AccountingPeriod.fiscal_year_id)
        .join(Voucher, Voucher.period_id == AccountingPeriod.id)
        .distinct()
    ).scalars()
    return tuple(rows)


def test_activate_unknown_package_id_raises(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    with unit_of_work(session_factory, _scope(dataset)) as session:
        with pytest.raises(ConfigPackageIdUnknownError):
            activate(session, package_id=-1, actor=ACTOR_ID)


def test_activate_same_scheme_is_allowed_even_with_vouchers(
    session_factory: sessionmaker[Session], dataset: DatasetRef, context: PostingContext
) -> None:
    _create_voucher(session_factory, dataset, context)

    with unit_of_work(session_factory, _scope(dataset, branch_ids=(context.branch_id,))) as session:
        fiscal_year_ids = _fiscal_year_ids_with_vouchers(session)
        assert fiscal_year_ids, "chứng từ vừa tạo phải làm năm tài chính có mặt trong tập này"

        package_id = _package_id(session, "TT99-2025")
        package = activate(
            session,
            package_id=package_id,
            actor=ACTOR_ID,
            fiscal_year_ids_with_vouchers=fiscal_year_ids,
        )
        assert package.activated_at is not None
        assert package.activated_by == ACTOR_ID


def test_activate_different_scheme_with_vouchers_is_refused(
    session_factory: sessionmaker[Session], dataset: DatasetRef, context: PostingContext
) -> None:
    """`context` dựng năm tài chính TT99 (`posting_support`); đã có chứng từ từ
    test trước — kích hoạt gói TT133 (khác `scheme`) phải bị chặn.
    """
    with unit_of_work(session_factory, _scope(dataset, branch_ids=(context.branch_id,))) as session:
        fiscal_year_ids = _fiscal_year_ids_with_vouchers(session)
        assert fiscal_year_ids

        package_id = _package_id(session, "TT133-2016")
        with pytest.raises(AccountingSchemeLockedError):
            activate(
                session,
                package_id=package_id,
                actor=ACTOR_ID,
                fiscal_year_ids_with_vouchers=fiscal_year_ids,
            )


def test_activate_different_scheme_without_matching_vouchers_is_allowed(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    """Không truyền năm tài chính nào (rỗng) — không có gì để xung đột."""
    with unit_of_work(session_factory, _scope(dataset)) as session:
        package_id = _package_id(session, "TT133-2016")
        package = activate(
            session, package_id=package_id, actor=ACTOR_ID, fiscal_year_ids_with_vouchers=()
        )
        assert package.activated_at is not None


def test_activate_holds_a_share_lock_on_the_conflicting_fiscal_year_row(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FOR SHARE` của `_reject_if_scheme_conflicts` phải THẬT SỰ giữ khóa
    trên dòng `fiscal_years` trong suốt transaction kích hoạt — đo bằng hai kết
    nối thật (sửa sau review, M-lock/M4: xóa `.with_for_update(read=True)` mà
    bộ test cũ vẫn xanh, nên phép đo tĩnh không đủ; cùng khuôn
    `test_write_holds_period_share_lock_and_serialises_writers` của lát 4C).

    **Vì sao kịch bản KHÁC scheme, không phải cùng scheme:** câu `SELECT ...
    FOR SHARE` của `_reject_if_scheme_conflicts` lọc
    `accounting_scheme != package.scheme` — ở kịch bản "cùng scheme, được phép"
    (test khác ở trên), điều kiện đó loại hết mọi dòng, nên câu lệnh không khóa
    gì cả (không có gì để chứng minh). Khóa chỉ thật sự được giữ ở đúng kịch
    bản có xung đột (khác scheme) — nơi hàm SẼ ném lỗi ngay sau khi đọc. Gate
    đặt bằng cách bắt rồi giữ lỗi đó lại trước khi ném ra ngoài: transaction
    (và khóa của nó) chỉ kết thúc khi `unit_of_work` thấy ngoại lệ thoát ra
    khỏi khối `with` — nghĩa là khóa vẫn còn nguyên suốt lúc gate đang chờ.
    """
    import threading

    from sqlalchemy import text as sql_text
    from sqlalchemy.exc import OperationalError

    from ket.kernel.config.packages import activator

    _create_voucher(session_factory, dataset, context)
    with unit_of_work(session_factory, _scope(dataset, branch_ids=(context.branch_id,))) as session:
        fiscal_year_ids = _fiscal_year_ids_with_vouchers(session)
    assert context.fiscal_year_id in fiscal_year_ids

    in_activate = threading.Event()
    release = threading.Event()
    real_reject = activator._reject_if_scheme_conflicts

    def gated_reject(*args: object, **kwargs: object) -> None:
        try:
            real_reject(*args, **kwargs)  # type: ignore[arg-type]
        except Exception as error:
            in_activate.set()
            assert release.wait(timeout=30), "test không nhả gate"
            raise error
        # Nhánh không xung đột không khóa gì (xem docstring) — không có ở đây
        # vì kịch bản test luôn xung đột, nhưng vẫn nhả gate để không treo mãi
        # nếu lời gọi khác dùng lại hàm này.
        in_activate.set()
        assert release.wait(timeout=30), "test không nhả gate"

    monkeypatch.setattr(activator, "_reject_if_scheme_conflicts", gated_reject)

    outcome: dict[str, object] = {}

    def activator_thread() -> None:
        try:
            with unit_of_work(session_factory, _scope(dataset)) as session:
                package_id = _package_id(session, "TT133-2016")
                outcome["package"] = activate(
                    session,
                    package_id=package_id,
                    actor=ACTOR_ID,
                    fiscal_year_ids_with_vouchers=fiscal_year_ids,
                )
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=activator_thread)
    thread.start()
    try:
        assert in_activate.wait(timeout=30), (
            f"activate() không tới được gate: {outcome.get('error')}"
        )

        with pytest.raises(OperationalError):
            with unit_of_work(session_factory, _scope(dataset)) as session:
                session.execute(
                    sql_text("SELECT id FROM fiscal_years WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": context.fiscal_year_id},
                )
    finally:
        release.set()
        thread.join(timeout=30)

    # Kịch bản khác scheme LUÔN bị từ chối — đúng bất biến FR-SYS-004, không
    # phải một tác dụng phụ của cách dựng test này.
    assert isinstance(outcome.get("error"), AccountingSchemeLockedError), outcome.get("error")
