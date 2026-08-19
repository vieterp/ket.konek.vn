"""Bảng của schema điều khiển (`public`) — ADR-017.

Ba bảng nền của schema điều khiển nằm ở tệp này:

* `datasets` — sổ đăng ký dữ liệu kế toán: mã, schema, chế độ TT99/TT133.
* `users` — danh tính đăng nhập **toàn cục**: một người dùng một mật khẩu, mở
  được nhiều dữ liệu kế toán. Quyền thì per-dataset (`user_roles`,
  `user_branches` nằm trong schema dataset) vì vai trò của một người ở mỗi
  doanh nghiệp là khác nhau.
* `system_metadata` — phiên bản schema điều khiển + thông tin cho handshake.

Hai bảng điều khiển còn lại sống cạnh cơ chế dùng chúng, không gom về đây:
`auth_sessions` (`security/auth_models.py`) và `control_audit_log`
(`auditing/control_log.py`). Tiêu chí phân biệt là **schema**, không phải tệp:
tất cả đều kế thừa `ControlBase`, và đó là thứ quyết định chúng nằm ở `public`.

Mọi thứ còn lại thuộc về dataset. Ranh giới này là thứ khiến "sao lưu/khôi
phục một doanh nghiệp" (RT-03) trở thành thao tác trên **một** schema.

Thêm bảng điều khiển mới thì phải chạm ba chỗ, nếu không nó sẽ vắng mặt ở đúng
một trong ba: `model_registry` (để `create_all` thấy), bước nâng cấp trong
`bootstrap._UPGRADE_STEPS` (để cụm đã cài có nó) và
`bootstrap.app_login_table_grants` (để vai trò runtime dùng được nó).

Bảng mới **mặc định không** vào `bootstrap.control_group_table_grants`: nhóm
`ket_control` là thứ mọi vai trò dataset kế thừa, nên mỗi quyền ở đó là quyền có
hiệu lực trong mọi truy vấn nghiệp vụ.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import ControlBase


class Dataset(ControlBase):
    """Một dữ liệu kế toán = một schema PostgreSQL (FR-SYS-001)."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    schema_name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    scheme: Mapped[str] = mapped_column(String(10), nullable=False)
    """`TT99` hoặc `TT133` — chế độ kế toán áp dụng. Là **dữ liệu**, không phải
    nhánh code (LD-06): đổi chế độ = kích hoạt gói cấu hình khác ở phase 5."""

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(ControlBase):
    """Danh tính đăng nhập toàn cục (FR-SYS-070, FR-NFR-010).

    Danh tính toàn cục nhưng **quyền** per-dataset (`user_roles`,
    `user_branches` trong schema dataset): một người mở được nhiều dữ liệu kế
    toán bằng một mật khẩu, với vai trò khác nhau ở mỗi nơi.

    Hệ quả của ranh giới đó nằm ở `totp_required` — xem chú thích cột.
    """

    __tablename__ = "users"
    __audit_exclude__ = frozenset({"password_hash", "totp_secret_enc"})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    """Bí mật TOTP **đã mã hóa** bằng khóa app lấy từ OS keystore (RT-05,
    ADR-019). Cột `bytea` chứ không phải `text` để không ai lỡ tay ghi giá trị
    dạng rõ vào đây; bản dump và bản sao lưu vì thế cũng không chứa bí mật."""

    totp_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Bắt buộc 2FA khi đăng nhập (FR-NFR-016, FR-SYS-074).

    Cờ **toàn cục** dù luật sinh ra nó là per-dataset ("vai trò quản trị hoặc
    quyền `bank.*`/`einvoice.*`"), vì đăng nhập xảy ra **trước** khi chọn dữ
    liệu kế toán — lúc kiểm mật khẩu, hệ thống chưa biết người này sắp mở doanh
    nghiệp nào. Lát 2B-1b bật cờ khi gán vai trò nhạy cảm ở **bất kỳ** dataset
    nào: thà đòi 2FA thừa một lần còn hơn để một quản trị viên đăng nhập không
    có lớp thứ hai rồi mới chọn dataset."""

    totp_enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Thời điểm người dùng **chứng minh** thiết bị sinh mã hoạt động.

    Cần một cột riêng chứ không suy từ `totp_secret_enc IS NOT NULL`: giữa lúc
    ghi bí mật và lúc người dùng quét được mã QR, việc có thể hỏng (quét nhầm,
    đóng ứng dụng, lệch đồng hồ). Coi "có bí mật" là "đã đăng ký" sẽ khóa họ ra
    khỏi chính tài khoản của mình, và người duy nhất mở được lại là quản trị
    viên — trên một hệ chạy offline thì đó là một cuộc gọi hỗ trợ."""

    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Bước thời gian 30 giây của mã TOTP dùng gần nhất — chống dùng lại mã
    trong cửa sổ chấp nhận (xem `security/totp.verify_code`)."""

    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Khóa tạm sau quá nhiều lần sai (xem `auth_service.LOCKOUT_*`).

    Đếm trong DB chứ không trong bộ nhớ tiến trình: bản cài LAN chạy nhiều
    worker/tiến trình, và một bộ đếm trong bộ nhớ sẽ được đặt lại bằng cách khởi
    động lại dịch vụ — tức là ai dò mật khẩu cũng tự xóa được nó."""

    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locale: Mapped[str] = mapped_column(
        String(5), nullable=False, default="vi", server_default="vi"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemMetadata(ControlBase):
    """Cặp khóa–giá trị mức cụm: phiên bản schema điều khiển, phiên bản server."""

    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


CONTROL_SCHEMA_VERSION_KEY = "control_schema_version"
