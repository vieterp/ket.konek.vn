"""Mở `Session` đã định tuyến đúng dataset và đúng phạm vi chi nhánh.

Bốn thứ phải xảy ra **trước câu truy vấn đầu tiên** của mỗi transaction. Ba lệnh
SQL đầu độc lập nhau về mặt kỹ thuật — thứ tự dưới đây đọc theo tầng ("quyền,
rồi tầm nhìn, rồi phạm vi dòng") chứ không phải một ràng buộc của PostgreSQL:

1. `SET LOCAL ROLE ds_<mã>_app` → **quyền** chỉ trên dataset này (D3). `search_path`
   một mình chỉ chọn tầm nhìn, không cấm gì; giới hạn của lớp này (tiêm SQL nhiều
   câu lệnh vẫn `SET ROLE` sang dataset khác được) ghi trong
   `kernel/security/dataset_roles.py`.
2. `SET LOCAL search_path` → chọn schema dataset (ADR-017). Sai bước này là
   ghi sổ nhầm doanh nghiệp.
3. `set_config('ket.branch_ids', …, local)` → phạm vi RLS (RT-04). Thiếu
   bước này thì không đọc được gì (fail-closed), chứ không phải đọc được tất.
4. Gắn `AuditContext` vào `session.info` → nhật ký biết ai đang thao tác.

Ba lệnh đầu đều `LOCAL`/theo transaction, nên connection quay lại pool là sạch.
Đây là điều kiện để nhiều người dùng dùng chung một pool mà không kế thừa quyền
của nhau.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Final

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.auditing.listener import (
    AUDIT_CONTEXT_KEY,
    AuditContext,
    register_audit_listener,
)
from ket.kernel.security.dataset_roles import set_local_role_statement
from ket.kernel.security.rls import set_search_path_statement
from ket.kernel.security.tenant import set_branch_scope

LOCK_TIMEOUT_MS: Final[int] = 3_000
"""Chờ tối đa bao lâu ở một khóa hàng trước khi bỏ cuộc.

Không có nó thì `lock_timeout` mặc định của PostgreSQL là **vô hạn**, và đó là
một vấn đề có thật với chính thiết kế của lát này: idempotency cố ý dựa vào việc
request trùng khóa **chờ** ở ràng buộc duy nhất. Cái chờ đó dài đúng bằng
transaction nghiệp vụ của bên thắng — ở phase 6 (ghi sổ nhiều bảng) có thể vài
giây. Người dùng bấm lại năm lần là năm connection nằm chờ, và pool chỉ có
`db_pool_size + db_max_overflow` = 15 chỗ. Vài người cùng làm vậy thì cả văn
phòng đứng vì một khóa.

3 giây: dài hơn hẳn mọi thao tác ghi bình thường (đo ở lát này: dưới 50ms), đủ
ngắn để một connection kẹt không kéo theo cái thứ hai. Hết giờ → PostgreSQL ném
lỗi, `unit_of_work` rollback, và tầng API trả lỗi thay vì treo."""

_SET_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :value, true)")
"""Giá trị ràng buộc như **tham số**, không ghép chuỗi — cùng lý do với GUC chi
nhánh (`security/tenant.py`): câu lệnh có tham số buộc psycopg dùng extended
protocol, và protocol đó không cho nhiều câu lệnh trong một lần gửi."""


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
    session.execute(text(set_local_role_statement(dataset_schema)))
    session.execute(text(set_search_path_statement(dataset_schema)))
    session.execute(_SET_LOCK_TIMEOUT, {"value": f"{LOCK_TIMEOUT_MS}ms"})
    set_branch_scope(session, branch_ids)
    session.info[AUDIT_CONTEXT_KEY] = audit


@contextmanager
def control_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transaction trên **schema điều khiển**: đăng nhập, phiên, danh tính.

    Không `SET ROLE`, không `search_path`, không GUC chi nhánh — và đó không
    phải thiếu sót. Ba lệnh đó chọn *một dữ liệu kế toán*, còn đường này chạy
    **trước khi** người dùng chọn dataset: lúc kiểm mật khẩu, hệ thống chưa biết
    (và không được phép đoán) họ sắp mở doanh nghiệp nào.

    An toàn không đến từ `SET ROLE` ở đây mà từ chỗ khác: `ket_app` được cấp
    quyền trên **đúng năm bảng điều khiển** (`bootstrap.app_login_table_grants`
    — `users`, `datasets`, `system_metadata`, `auth_sessions`,
    `control_audit_log`), trong đó `control_audit_log` là chỉ-thêm. Bảng nghiệp
    vụ của mọi dataset nằm ngoài tầm với vì quyền của chúng thuộc `ds_<mã>_app`,
    và phiên này chưa `SET ROLE` sang vai trò nào cả.

    Chiều ngược lại cũng đã đóng: vai trò dataset **không** với được `users` hay
    `auth_sessions` (`bootstrap.control_group_table_grants`), nên một lỗ tiêm SQL
    trong truy vấn nghiệp vụ không leo thang thành tự cấp phiên.

    Commit khi thoát sạch, rollback khi có ngoại lệ — cùng hợp đồng với
    `unit_of_work`. Nhật ký điều khiển vì thế đi cùng chuyến với thay đổi nó mô
    tả.
    """
    session = factory()
    try:
        session.begin()
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def worker_session(factory: sessionmaker[Session], *, dataset_schema: str) -> Iterator[Session]:
    """Transaction của tiến trình worker trên bảng `jobs` của một dataset.

    Ba thứ **cố ý không có**, và mỗi thứ vắng mặt vì một lý do khác nhau:

    * Không `SET LOCAL ROLE`: đây là lượt **giành việc**, chạy dưới chính danh
      nghĩa `ket_worker` để policy RLS `p_worker_queue` (`TO ket_worker`) có
      hiệu lực. Chuyển vai trò ở đây sẽ làm policy đó không bao giờ áp và hàng
      đợi trông như rỗng (quyết định F2). Thân job thì ngược lại — nó chạy trong
      `unit_of_work` bình thường, có đủ ba lệnh.
    * Không GUC chi nhánh: worker không thuộc chi nhánh nào; phạm vi chi nhánh
      được đặt cho thân job theo `jobs.branch_id` của chính dòng vừa giành.
    * Không `AuditContext`: `Job` không phải `Audited` (trạng thái job đổi liên
      tục theo cơ chế nội bộ và ghi vết chúng chỉ làm loãng nhật ký kiểm toán).
      Việc *nghiệp vụ* mà job làm thì vẫn ghi vết đầy đủ, trong transaction của
      thân job.

    Commit khi thoát sạch — mỗi lượt báo tiến độ phải nhìn thấy được ngay, đó là
    lý do nó không dùng chung transaction với thân job.
    """
    session = factory()
    try:
        session.begin()
        session.execute(text(set_search_path_statement(dataset_schema)))
        session.execute(_SET_LOCK_TIMEOUT, {"value": f"{LOCK_TIMEOUT_MS}ms"})
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


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
