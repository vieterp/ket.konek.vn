"""`resolve_package` chỉ đọc gói **đã kích hoạt** (sửa sau review, C1 CRITICAL).

Review thù địch lát 5A tìm ra: trước sửa này, `resolve_package` chọn theo
`effective_from` mà không lọc `activated_at IS NOT NULL` — một gói **nhập**
qua `.zip` (`is_builtin=False`, nằm im cho tới khi kích hoạt) sẽ **chiếm quyền
đọc TK ngay khi nhập**, đi vòng qua cổng khóa đổi chế độ kế toán của
`activator.activate` (FR-SYS-004). Ba test dưới đây tái hiện đúng kịch bản
probe của reviewer, xác nhận sau khi kích hoạt mới có hiệu lực, và khóa tiêu
chí phân định khi hai gói cùng `effective_from` (đột biến M9).

Dataset riêng (không `dataset_alpha`): cần kiểm soát tuyệt đối tập
`config_packages` khớp `scheme=TT99` cho một `on_date` cụ thể — `dataset_alpha`
dùng chung hàng trăm test khác, không đảm bảo được điều đó.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import ConfigPackage
from ket.kernel.config.accounts_provider import resolve_package
from ket.kernel.datasets.provisioning import DatasetRef, drop_dataset_schema, provision_dataset
from ket.kernel.errors import ConfigPackageNotFoundError
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work

pytestmark = pytest.mark.db

ACTOR_ID = 1
DATASET_CODE = "cfgpkg_resolve_activation"
ON_DATE = date(2030, 6, 1)
"""Ngày xa trong tương lai — chỉ gói do chính test này tạo mới phủ được nó,
không lẫn với `TT99-2025`/`TT133-2016` (hiệu lực 2026/2017, cũng khớp `on_date`
này về mặt `effective_from <= ngày`, nên test luôn tạo gói của **chính nó**
với `effective_from` sát `ON_DATE` để phép so `code` không mơ hồ)."""


@pytest.fixture(scope="module")
def dataset(owner_engine: Engine) -> Iterator[DatasetRef]:
    ref = provision_dataset(
        owner_engine, code=DATASET_CODE, name="Resolve theo kích hoạt", scheme="TT99"
    )
    yield ref
    drop_dataset_schema(owner_engine, DATASET_CODE)


def _scope(dataset: DatasetRef) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=ACTOR_ID, branch_ids=())


def _builtin_code(session: Session) -> str:
    code = session.scalar(
        select(ConfigPackage.code)
        .where(ConfigPackage.scheme == "TT99")
        .where(ConfigPackage.is_builtin.is_(True))
    )
    assert code is not None, "gói TT99 dựng sẵn chưa được gieo"
    return code


def test_imported_but_not_activated_package_does_not_affect_resolve(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    """Đúng kịch bản probe của reviewer: nhập một gói TT99 "hijack" hiệu lực
    ngay ngày kế tiếp gói thật, KHÔNG gọi activate — `resolve_package` vẫn
    phải trả về gói đã kích hoạt (builtin), không phải gói vừa nhập.
    """
    with unit_of_work(session_factory, _scope(dataset)) as session:
        builtin_code = _builtin_code(session)

        hijack = ConfigPackage(
            code="ZZ-PROBE-HIJACK",
            name="Gói giả lập nhập nhưng chưa kích hoạt",
            scheme="TT99",
            effective_from=ON_DATE - timedelta(days=180),
            is_builtin=False,
            activated_at=None,
            activated_by=None,
        )
        session.add(hijack)
        session.flush()

        resolved = resolve_package(session, scheme="TT99", on_date=ON_DATE)
        assert resolved.code == builtin_code, (
            "gói chưa kích hoạt không được ảnh hưởng resolve — C1 tái diễn"
        )
        assert resolved.code != "ZZ-PROBE-HIJACK"

        session.delete(hijack)


def test_activating_the_imported_package_makes_it_win(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    """Sau khi kích hoạt (thời điểm kích hoạt muộn hơn gói builtin), gói nhập
    thắng — đúng tinh thần "gói mới hơn thay gói cũ kể từ ngày nó hiệu lực".
    """
    with unit_of_work(session_factory, _scope(dataset)) as session:
        activated = ConfigPackage(
            code="ZZ-PROBE-ACTIVATED",
            name="Gói giả lập đã kích hoạt",
            scheme="TT99",
            effective_from=ON_DATE - timedelta(days=180),
            is_builtin=False,
            activated_at=datetime.now(UTC),
            activated_by=ACTOR_ID,
        )
        session.add(activated)
        session.flush()

        resolved = resolve_package(session, scheme="TT99", on_date=ON_DATE)
        assert resolved.code == "ZZ-PROBE-ACTIVATED"

        session.delete(activated)


def test_same_effective_from_breaks_tie_by_activation_then_id(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    """Hai gói cùng `scheme` cùng `effective_from` — kích hoạt SAU thắng
    (không phải `id` lớn hơn thắng bừa). Khóa đột biến M9 (bỏ tiêu chí phụ).

    **Cố ý tạo dòng `id` NHỎ HƠN nhưng `activated_at` MUỘN HƠN** (ngược chiều
    nhau): nếu tạo hai dòng theo đúng thứ tự thời gian tự nhiên (dòng sau vừa
    có `id` lớn hơn vừa có `activated_at` muộn hơn), hai tiêu chí luôn đồng
    thuận và test không phân biệt được `ORDER BY activated_at DESC` với
    `ORDER BY id DESC` — cả hai đều trỏ ra cùng một gói thắng, nên bỏ hẳn tiêu
    chí `activated_at` vẫn để test xanh. Đảo ngược ở đây buộc đúng tiêu chí
    `activated_at` phải là tiêu chí quyết định.
    """
    shared_effective_from = ON_DATE - timedelta(days=1)
    with unit_of_work(session_factory, _scope(dataset)) as session:
        smaller_id_later_activation = ConfigPackage(
            code="ZZ-PROBE-TIE-SMALLER-ID-LATER-ACTIVATION",
            name="Gói id nhỏ hơn nhưng kích hoạt sau — phải thắng",
            scheme="TT99",
            effective_from=shared_effective_from,
            is_builtin=False,
            activated_at=datetime.now(UTC),
            activated_by=ACTOR_ID,
        )
        session.add(smaller_id_later_activation)
        session.flush()

        larger_id_earlier_activation = ConfigPackage(
            code="ZZ-PROBE-TIE-LARGER-ID-EARLIER-ACTIVATION",
            name="Gói id lớn hơn nhưng kích hoạt trước — phải thua",
            scheme="TT99",
            effective_from=shared_effective_from,
            is_builtin=False,
            activated_at=datetime.now(UTC) - timedelta(minutes=10),
            activated_by=ACTOR_ID,
        )
        session.add(larger_id_earlier_activation)
        session.flush()
        assert larger_id_earlier_activation.id > smaller_id_later_activation.id
        assert larger_id_earlier_activation.activated_at < smaller_id_later_activation.activated_at

        resolved = resolve_package(session, scheme="TT99", on_date=ON_DATE)
        assert resolved.code == smaller_id_later_activation.code, (
            "activated_at phải thắng trước id — id lớn hơn không được ghi đè "
            "một gói kích hoạt sớm hơn"
        )

        session.delete(smaller_id_later_activation)
        session.delete(larger_id_earlier_activation)


def test_same_effective_from_and_activation_breaks_tie_by_id(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    """Ba tiêu chí cùng hòa (`effective_from`, `activated_at` — cùng một giá
    trị Python truyền cho cả hai dòng) — `id` lớn hơn phải thắng, tất định.
    Khóa đúng đột biến M9 gốc (bỏ `ConfigPackage.id.desc()`): hai test tie ở
    trên đều phân định được bằng `activated_at` trước khi chạm tới `id`, nên
    chỉ mình chúng không đủ bắt việc bỏ tiêu chí `id` — cần một cặp hòa cả hai
    tiêu chí đầu.
    """
    shared_effective_from = ON_DATE - timedelta(days=2)
    shared_activated_at = datetime.now(UTC)
    with unit_of_work(session_factory, _scope(dataset)) as session:
        first = ConfigPackage(
            code="ZZ-PROBE-FULL-TIE-1",
            name="Gói hòa cả effective_from lẫn activated_at — id nhỏ hơn",
            scheme="TT99",
            effective_from=shared_effective_from,
            is_builtin=False,
            activated_at=shared_activated_at,
            activated_by=ACTOR_ID,
        )
        session.add(first)
        session.flush()

        second = ConfigPackage(
            code="ZZ-PROBE-FULL-TIE-2",
            name="Gói hòa cả effective_from lẫn activated_at — id lớn hơn",
            scheme="TT99",
            effective_from=shared_effective_from,
            is_builtin=False,
            activated_at=shared_activated_at,
            activated_by=ACTOR_ID,
        )
        session.add(second)
        session.flush()
        assert second.id > first.id

        resolved = resolve_package(session, scheme="TT99", on_date=ON_DATE)
        assert resolved.code == "ZZ-PROBE-FULL-TIE-2"

        session.delete(first)
        session.delete(second)


def test_no_activated_package_covering_the_date_raises(
    session_factory: sessionmaker[Session], dataset: DatasetRef
) -> None:
    far_future = date(2099, 1, 1)
    with unit_of_work(session_factory, _scope(dataset)) as session:
        with pytest.raises(ConfigPackageNotFoundError):
            resolve_package(session, scheme="TT133", on_date=date(1999, 1, 1))
        # Đối chứng dương: cùng scheme, ngày trong phạm vi builtin thì resolve được.
        resolved = resolve_package(session, scheme="TT99", on_date=far_future)
        assert resolved.is_builtin is True
