"""Mở `Session` đã định tuyến đúng dataset và đúng phạm vi chi nhánh.

Ba thứ phải xảy ra **trước câu truy vấn đầu tiên** của mỗi transaction, đúng
thứ tự này:

1. `SET LOCAL search_path` → chọn schema dataset (ADR-017). Sai bước này là
   ghi sổ nhầm doanh nghiệp.
2. `set_config('ket.branch_ids', …, local)` → phạm vi RLS (RT-04). Thiếu
   bước này thì không đọc được gì (fail-closed), chứ không phải đọc được tất.
3. Gắn `AuditContext` vào `session.info` → nhật ký biết ai đang thao tác.

Cả ba đều `LOCAL`/theo transaction, nên connection quay lại pool là sạch. Đây
là điều kiện để nhiều người dùng dùng chung một pool mà không kế thừa quyền
của nhau.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import (
    AUDIT_CONTEXT_KEY,
    AuditContext,
    register_audit_listener,
)
from ket.kernel.security.rls import set_search_path_statement
from ket.kernel.security.tenant import set_branch_scope


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Nhà máy `Session` cho một engine.

    `expire_on_commit=False` để đối tượng còn đọc được sau khi service commit —
    nếu không, tầng API phải mở lại transaction chỉ để đọc thứ vừa ghi.

    Đăng ký listener nhật ký ở đây, tức là **không có đường** tạo session bỏ
    qua nhật ký: mọi session của ứng dụng đều sinh từ hàm này.
    """
    register_audit_listener()
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


def bind_transaction_scope(
    session: Session,
    *,
    dataset_schema: str,
    branch_ids: Sequence[int],
    audit: AuditContext,
) -> None:
    """Đặt schema + phạm vi chi nhánh + ngữ cảnh nhật ký cho transaction đang mở.

    Bắt buộc gọi **trong** một transaction đang mở: `SET LOCAL` ngoài
    transaction không có tác dụng và PostgreSQL chỉ cảnh báo chứ không báo lỗi —
    đúng kiểu hỏng âm thầm mà cơ chế này sinh ra để chống.
    """
    if not session.in_transaction():
        raise RuntimeError("bind_transaction_scope phải chạy trong transaction đang mở")
    session.execute(text(set_search_path_statement(dataset_schema)))
    set_branch_scope(session, branch_ids)
    session.info[AUDIT_CONTEXT_KEY] = audit


@contextmanager
def dataset_session(
    factory: sessionmaker[Session],
    *,
    dataset_schema: str,
    branch_ids: Sequence[int],
    audit: AuditContext,
) -> Iterator[Session]:
    """Session đã định tuyến, **chưa** tự commit.

    Dùng cho đường đọc và cho test. Đường ghi nghiệp vụ dùng
    `unit_of_work.unit_of_work` để có commit/rollback đúng một lần.
    """
    session = factory()
    try:
        session.begin()
        bind_transaction_scope(
            session,
            dataset_schema=dataset_schema,
            branch_ids=branch_ids,
            audit=audit,
        )
        yield session
    finally:
        session.close()
