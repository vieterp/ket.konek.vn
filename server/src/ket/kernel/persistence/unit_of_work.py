"""Một request nghiệp vụ = một transaction (FR-NFR-003).

Không phải quy ước cho đẹp: chứng từ kế toán chạm nhiều bảng (header, chi
tiết, phát sinh sổ cái, số dư, nhật ký, khóa idempotency). Commit từng phần
để lại sổ lệch mà không có gì báo. Vì vậy **không** service nào được tự
`commit()`; chúng nhận `Session` đã mở sẵn và người mở transaction cũng là
người đóng.

Cùng lý do đó, khóa idempotency (RT-12) ghi trong **chính** transaction này,
không phải ở transaction riêng commit trước — bằng không sẽ có khóa tồn tại
cho một chứng từ chưa từng được ghi.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import AuditContext
from ket.kernel.persistence.session import bind_transaction_scope


@dataclass(frozen=True)
class RequestScope:
    """Danh tính của một request — mọi thứ transaction cần biết về người gọi.

    Phase 2 slice sau dựng đối tượng này trong middleware từ token + header
    dataset; ở đây nó đã là hợp đồng cố định để không phase nào tự nghĩ ra một
    cách truyền ngữ cảnh riêng.
    """

    dataset_schema: str
    user_id: int
    branch_ids: tuple[int, ...]
    correlation_id: UUID | None = None
    client_info: str | None = None
    acting_branch_id: int | None = None
    """Chi nhánh đang thao tác (khi người dùng có nhiều chi nhánh). Ghi vào
    nhật ký cho bản ghi không có cột `branch_id` riêng."""

    def __post_init__(self) -> None:
        """Chi nhánh đang thao tác phải nằm trong phạm vi được phép.

        Không kiểm ở đây thì lỗi vẫn nổ, nhưng nổ **muộn và khó hiểu**: dòng
        nhật ký mang chi nhánh ngoài phạm vi bị `WITH CHECK` của RLS chặn, và
        cả transaction nghiệp vụ đổ theo với một lỗi PostgreSQL thô, ở giữa
        flush, không nêu được nguyên nhân thật. Chặn tại chỗ dựng ngữ cảnh cho
        thông điệp chỉ thẳng vào chỗ sai.
        """
        if self.acting_branch_id is not None and self.acting_branch_id not in self.branch_ids:
            raise ValueError(
                f"acting_branch_id={self.acting_branch_id} không nằm trong phạm vi "
                f"branch_ids={self.branch_ids}"
            )

    def audit_context(self) -> AuditContext:
        return AuditContext(
            user_id=self.user_id,
            branch_id=self.acting_branch_id,
            correlation_id=self.correlation_id,
            client_info=self.client_info,
        )


@contextmanager
def unit_of_work(factory: sessionmaker[Session], scope: RequestScope) -> Iterator[Session]:
    """Mở transaction, commit khi thoát sạch, rollback khi có ngoại lệ.

    Rollback kéo theo cả nhật ký (listener ghi cùng transaction) — đó là cách
    bất biến "không có nhật ký mồ côi" được bảo đảm, chứ không phải bằng dọn dẹp
    về sau.
    """
    session = factory()
    try:
        session.begin()
        bind_transaction_scope(
            session,
            dataset_schema=scope.dataset_schema,
            branch_ids=scope.branch_ids,
            audit=scope.audit_context(),
        )
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def branch_scope_of(branch_ids: Sequence[int]) -> tuple[int, ...]:
    """Chuẩn hóa danh sách chi nhánh (bỏ trùng, sắp xếp) cho `RequestScope`."""
    return tuple(sorted(set(branch_ids)))
