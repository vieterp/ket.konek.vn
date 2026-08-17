"""Idempotency: khóa và lệnh ghi nghiệp vụ sống chết cùng nhau (FR-NFR-004, RT-12).

Bất biến đắt nhất của lát này nằm ở tên tệp: **cùng transaction**. Nếu khóa được
commit ở một transaction riêng thì mọi test dưới đây vẫn xanh trừ đúng một cái —
`test_a_failed_write_leaves_no_key_behind` — và cái giá của việc mất nó là một
khóa trỏ tới chứng từ chưa từng tồn tại, chặn vĩnh viễn lần thử lại.

Chạy trên PostgreSQL thật vì phần khó là hành vi **đồng thời**: request thứ hai
`INSERT` cùng khóa phải **chờ** rồi mới báo trùng. Không mô phỏng được.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    IdempotencyKeyExpiredError,
    IdempotencyKeyReusedError,
    IdempotencyRaceLostError,
    RowVersionConflictError,
)
from ket.kernel.idempotency.models import IdempotencyKey
from ket.kernel.idempotency.service import (
    IdempotentRef,
    execute_once,
    fingerprint_of,
    prune_expired_keys,
)
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch

pytestmark = pytest.mark.db

ROUTE = "POST /test/branches"

TEST_TTL = timedelta(hours=24)
"""`execute_once` cố ý không có TTL mặc định — nguồn sự thật là cấu hình bản
cài (`Settings.idempotency_ttl_hours`), nên test cũng phải khai tường minh."""


def _scope(dataset: DatasetRef, user_id: int = 1) -> RequestScope:
    return RequestScope(dataset_schema=dataset.schema_name, user_id=user_id, branch_ids=())


def _create_branch(code: str) -> Callable[[Session], tuple[int, IdempotentRef]]:
    """`work` mẫu: tạo một chi nhánh và trả tham chiếu tới nó."""

    def work(session: Session) -> tuple[int, IdempotentRef]:
        branch = BranchService(session).create(code=code, name=f"Chi nhánh {code}")
        session.flush()
        return branch.id, IdempotentRef(result_type="branches", result_id=str(branch.id))

    return work


def _load_branch(session: Session, ref: IdempotentRef) -> int:
    return int(ref.result_id)


def _count_branches(factory: sessionmaker[Session], dataset: DatasetRef, code: str) -> int:
    with unit_of_work(factory, _scope(dataset)) as session:
        return len(session.scalars(select(Branch).where(Branch.code == code)).all())


def test_sending_the_same_request_twice_creates_one_record(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Chính là tình huống RT-12 sinh ra để chặn: bấm Lưu, mất mạng, bấm lại."""
    code = "IDEM_MOT_LAN"
    fingerprint = fingerprint_of('{"code": "IDEM_MOT_LAN"}')

    first_id, first_created = execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-lap-01",
        fingerprint=fingerprint,
        work=_create_branch(code),
        replay=_load_branch,
        ttl=TEST_TTL,
    )
    second_id, second_created = execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-lap-01",
        fingerprint=fingerprint,
        work=_create_branch(code),
        replay=_load_branch,
        ttl=TEST_TTL,
    )

    assert first_created is True
    assert second_created is False, "lần gửi lại không được ghi thêm gì"
    assert first_id == second_id
    assert _count_branches(session_factory, dataset_alpha, code) == 1


def test_a_failed_write_leaves_no_key_behind(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Bất biến "cùng transaction", nhìn từ phía hỏng.

    Lệnh ghi nghiệp vụ đổ giữa chừng → khóa cũng phải biến mất, để lần thử lại
    còn làm lại được. Khóa commit riêng sẽ sống sót và khóa vĩnh viễn thao tác
    này.
    """

    def failing_work(session: Session) -> tuple[int, IdempotentRef]:
        BranchService(session).create(code="IDEM_HONG", name="Sẽ hỏng")
        session.flush()
        raise RuntimeError("lệnh ghi nghiệp vụ đổ")

    with pytest.raises(RuntimeError):
        execute_once(
            session_factory,
            _scope(dataset_alpha),
            route_key=ROUTE,
            key="khoa-hong-01",
            fingerprint=fingerprint_of("{}"),
            work=failing_work,
            replay=_load_branch,
            ttl=TEST_TTL,
        )

    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        key = session.scalar(
            select(IdempotencyKey).where(IdempotencyKey.idempotency_key == "khoa-hong-01")
        )
        branch = session.scalar(select(Branch).where(Branch.code == "IDEM_HONG"))

    assert key is None, "khóa sống sót sau khi lệnh ghi bị rollback"
    assert branch is None, "chi nhánh sống sót sau khi transaction bị rollback"


def test_reusing_a_key_for_different_content_is_refused(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Trả kết quả cũ cho nội dung mới = im lặng bỏ rơi lệnh ghi thứ hai."""
    execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-dung-lai",
        fingerprint=fingerprint_of('{"code": "IDEM_A"}'),
        work=_create_branch("IDEM_A"),
        replay=_load_branch,
        ttl=TEST_TTL,
    )

    with pytest.raises(IdempotencyKeyReusedError):
        execute_once(
            session_factory,
            _scope(dataset_alpha),
            route_key=ROUTE,
            key="khoa-dung-lai",
            fingerprint=fingerprint_of('{"code": "IDEM_B"}'),
            work=_create_branch("IDEM_B"),
            replay=_load_branch,
            ttl=TEST_TTL,
        )

    assert _count_branches(session_factory, dataset_alpha, "IDEM_B") == 0


def test_another_user_cannot_replay_a_key_that_is_not_theirs(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Khóa client sinh ngẫu nhiên vẫn có thể trùng nhau giữa hai người dùng.

    Trả kết quả của người kia sẽ vừa rò dữ liệu vừa nuốt mất lệnh ghi này.
    """
    fingerprint = fingerprint_of('{"code": "IDEM_NGUOI_KHAC"}')
    execute_once(
        session_factory,
        _scope(dataset_alpha, user_id=1),
        route_key=ROUTE,
        key="khoa-chung",
        fingerprint=fingerprint,
        work=_create_branch("IDEM_NGUOI_KHAC"),
        replay=_load_branch,
        ttl=TEST_TTL,
    )

    with pytest.raises(IdempotencyKeyReusedError):
        execute_once(
            session_factory,
            _scope(dataset_alpha, user_id=2),
            route_key=ROUTE,
            key="khoa-chung",
            fingerprint=fingerprint,
            work=_create_branch("IDEM_NGUOI_KHAC_2"),
            replay=_load_branch,
            ttl=TEST_TTL,
        )


def test_an_expired_key_is_refused_instead_of_replayed(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Fail-closed (RT-12): chạy lại một lệnh ghi sổ sau 25 giờ là ghi trùng."""
    fingerprint = fingerprint_of('{"code": "IDEM_HET_HAN"}')
    execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-het-han",
        fingerprint=fingerprint,
        work=_create_branch("IDEM_HET_HAN"),
        replay=_load_branch,
        ttl=timedelta(hours=1),
    )

    later = datetime.now(UTC) + timedelta(hours=2)
    with pytest.raises(IdempotencyKeyExpiredError):
        execute_once(
            session_factory,
            _scope(dataset_alpha),
            route_key=ROUTE,
            key="khoa-het-han",
            fingerprint=fingerprint,
            work=_create_branch("IDEM_HET_HAN_2"),
            replay=_load_branch,
            ttl=TEST_TTL,
            now=later,
        )

    assert _count_branches(session_factory, dataset_alpha, "IDEM_HET_HAN_2") == 0


def test_the_same_key_on_a_different_route_is_a_different_scope(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Phạm vi theo route: hai màn hình sinh trùng khóa là chuyện có thật."""
    key = "khoa-hai-man-hinh"
    _, first_created = execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key="POST /test/route-a",
        key=key,
        fingerprint=fingerprint_of("{}"),
        work=_create_branch("IDEM_ROUTE_A"),
        replay=_load_branch,
        ttl=TEST_TTL,
    )
    _, second_created = execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key="POST /test/route-b",
        key=key,
        fingerprint=fingerprint_of("{}"),
        work=_create_branch("IDEM_ROUTE_B"),
        replay=_load_branch,
        ttl=TEST_TTL,
    )

    assert first_created and second_created
    assert _count_branches(session_factory, dataset_alpha, "IDEM_ROUTE_B") == 1


def test_a_business_constraint_error_is_not_disguised_as_a_key_conflict(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """`IntegrityError` của lệnh ghi ≠ tranh chấp khóa.

    Nuốt nó thành `409 idempotency.race_lost` sẽ bảo người dùng "thử lại sau" khi
    thứ họ cần là "mã chi nhánh này đã có người dùng".
    """
    execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-trung-ma-1",
        fingerprint=fingerprint_of("{}"),
        work=_create_branch("IDEM_TRUNG_MA"),
        replay=_load_branch,
        ttl=TEST_TTL,
    )

    # Khóa **khác**, nhưng mã chi nhánh thì trùng → ràng buộc duy nhất của
    # `branches` phải là thứ nổi lên.
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        execute_once(
            session_factory,
            _scope(dataset_alpha),
            route_key=ROUTE,
            key="khoa-trung-ma-2",
            fingerprint=fingerprint_of("{}"),
            work=_create_branch("IDEM_TRUNG_MA"),
            replay=_load_branch,
            ttl=TEST_TTL,
        )


def test_two_parallel_requests_with_one_key_write_exactly_one_record(
    test_settings: object, app_engine: Engine, dataset_alpha: DatasetRef
) -> None:
    """Hai máy trạm gửi cùng một khóa cùng lúc — chỉ một chứng từ được ghi.

    Mỗi luồng có `Session` riêng trên connection riêng, đúng như hai request
    thật. PostgreSQL cho request thứ hai **chờ** ở ràng buộc duy nhất rồi báo
    trùng; `execute_once` bắt lấy và đọc kết quả của bên thắng.
    """
    assert test_settings is not None
    factory = create_session_factory(app_engine)
    code = "IDEM_SONG_SONG"
    fingerprint = fingerprint_of('{"code": "IDEM_SONG_SONG"}')

    def attempt(_index: int) -> tuple[int, bool]:
        return execute_once(
            factory,
            _scope(dataset_alpha),
            route_key=ROUTE,
            key="khoa-song-song",
            fingerprint=fingerprint,
            work=_create_branch(code),
            replay=_load_branch,
            ttl=TEST_TTL,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))

    created = [result for result in results if result[1]]
    assert len(created) == 1, f"kỳ vọng đúng một lần thực hiện, nhận {results}"
    assert len({result[0] for result in results}) == 1, "mọi luồng phải thấy cùng một bản ghi"
    assert _count_branches(factory, dataset_alpha, code) == 1


def test_row_version_conflict_carries_the_latest_record() -> None:
    """`RowVersionConflictError` mang bản mới nhất ra tới thân RFC 7807.

    Không cần DB: đây là hợp đồng của lớp lỗi (`problem_extra`), và nó là thứ
    handler dựa vào để trả `latest` cho client.
    """
    error = RowVersionConflictError("lệch", latest={"id": 7, "row_version": 3}, entity="branches")

    assert error.http_status == 409
    assert error.problem_extra() == {"latest": {"id": 7, "row_version": 3}}
    assert error.details["entity"] == "branches"


def test_a_key_row_without_a_result_is_treated_as_unfinished(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Dòng khóa nửa vời (kết quả `NULL`) là **hỏng**, không phải kết quả rỗng.

    Không quan sát được qua đường bình thường — hai cột kết quả chỉ trống giữa
    hai câu lệnh của một transaction chưa commit. Tới được trạng thái này nghĩa
    là có người ghi thẳng vào bảng, hoặc một bản khôi phục dở dang. Guard trả
    `409` để client thử lại; gỡ nó đi thì `replay` nhận `int(None)` và người
    dùng nhận `500`.
    """
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        session.add(
            IdempotencyKey(
                route_key=ROUTE,
                idempotency_key="khoa-nua-voi",
                user_id=1,
                request_fingerprint=fingerprint_of("{}"),
                expires_at=datetime.now(UTC) + TEST_TTL,
            )
        )

    with pytest.raises(IdempotencyRaceLostError):
        execute_once(
            session_factory,
            _scope(dataset_alpha),
            route_key=ROUTE,
            key="khoa-nua-voi",
            fingerprint=fingerprint_of("{}"),
            work=_create_branch("IDEM_NUA_VOI"),
            replay=_load_branch,
            ttl=TEST_TTL,
        )

    assert _count_branches(session_factory, dataset_alpha, "IDEM_NUA_VOI") == 0


def test_expired_keys_are_prunable_and_live_ones_are_kept(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef
) -> None:
    """Bảng chỉ tăng nên phải có đường dọn — nhưng chỉ dọn thứ đã hết hiệu lực."""
    execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-con-han",
        fingerprint=fingerprint_of("{}"),
        work=_create_branch(f"IDEM_CON_HAN_{uuid4().hex[:6].upper()}"),
        replay=_load_branch,
        ttl=TEST_TTL,
    )
    execute_once(
        session_factory,
        _scope(dataset_alpha),
        route_key=ROUTE,
        key="khoa-se-het-han",
        fingerprint=fingerprint_of("{}"),
        work=_create_branch(f"IDEM_HET_{uuid4().hex[:6].upper()}"),
        replay=_load_branch,
        ttl=timedelta(minutes=1),
    )

    later = datetime.now(UTC) + timedelta(hours=2)
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        removed = prune_expired_keys(session, now=later)

    assert removed >= 1
    with unit_of_work(session_factory, _scope(dataset_alpha)) as session:
        remaining = set(
            session.scalars(
                select(IdempotencyKey.idempotency_key).where(
                    IdempotencyKey.idempotency_key.in_(["khoa-con-han", "khoa-se-het-han"])
                )
            ).all()
        )

    assert "khoa-con-han" in remaining, "khóa còn hạn bị dọn nhầm"
    assert "khoa-se-het-han" not in remaining
