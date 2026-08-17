"""Cấu hình app server.

Nguồn giá trị theo thứ tự ưu tiên: biến môi trường `KET_*` → file `.env` →
mặc định trong lớp này. **Bí mật thật (mật khẩu DB, khóa mã hóa backup, token
eSign) KHÔNG lấy từ đây ở môi trường thật** — chúng nằm trong OS keystore và
được nạp ở phase 2 (ADR-019 key-management). Phase 1 chỉ dựng khung cấu hình.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ket import __version__
from ket.kernel.versions import parse_required_version

DeploymentMode = Literal["standalone", "lan"]


class Settings(BaseSettings):
    """Cấu hình runtime của một tiến trình app server."""

    model_config = SettingsConfigDict(
        env_prefix="KET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    # --- Triển khai (LD-01: một-máy và LAN là cấu hình, không phải nhánh code)
    deployment_mode: DeploymentMode = "standalone"
    """`standalone` = app server + PostgreSQL + client cùng PC, bind 127.0.0.1.
    `lan` = app server chạy như dịch vụ trên máy host, 5–50 client."""

    host: str = "127.0.0.1"
    port: int = 5443

    # --- Cơ sở dữ liệu
    database_url: str = "postgresql+psycopg://ket_app@localhost/ket"
    """DSN **runtime**, đăng nhập bằng vai trò `ket_app`: không sở hữu bảng,
    không `BYPASSRLS`, không sửa được `audit_log` (RT-02/RT-04). Không kèm mật
    khẩu ở môi trường thật — mật khẩu nạp từ OS keystore. Kênh app→DB phải TLS
    verify-full khi DB khác host (ADR-019, RT-06)."""

    owner_database_url: str = "postgresql+psycopg://ket_owner@localhost/ket"
    """DSN **đặc quyền** cho DDL/migration/tạo dataset. Tách hẳn khỏi
    `database_url` là điều kiện để nhật ký bất biến và RLS có hiệu lực — dùng
    chung một vai trò cho cả hai việc thì cả hai cơ chế đều vô hiệu."""

    db_pool_size: int = 10
    db_max_overflow: int = 5
    """Quy mô mục tiêu đã chốt (OQ#3): ~20 người dùng đồng thời. Job nặng chạy ở
    tiến trình worker riêng nên không ăn vào pool của API."""

    default_dataset_schema: str = "ket_default"
    """Một dữ liệu kế toán = một PG schema (ADR-017). Chỉ là mặc định cho lệnh
    migration chạy tay; runtime lấy schema từ bảng `datasets` theo dataset của
    request."""

    alembic_ini_path: Path | None = None
    """Ghi đè đường dẫn `alembic.ini` khi chạy từ thư mục khác hoặc từ bản đóng
    gói (S4/phase 11). `None` = suy từ vị trí gói."""

    minimum_postgres_version: int = 16
    """Phiên bản PostgreSQL tối thiểu của bản cài (quyết định D4 của phase 2).

    Ghim để dev, CI và máy khách chạy **cùng một** bộ hành vi: RLS, quy tắc
    `CREATEROLE`, `MERGE`, `NULLS NOT DISTINCT` đều khác nhau giữa các phiên
    bản, và đây là những thứ cơ chế cô lập dữ liệu dựa vào. Kiểm lúc khởi động
    thay vì để một câu lệnh của phase 3 nổ giữa kỳ khóa sổ ở nơi cài đặt."""

    verify_postgres_version_on_startup: bool = True
    """Bật cổng `minimum_postgres_version` lúc khởi động.

    Cờ **riêng**, không gộp vào `verify_schema_on_startup`: hai cổng canh hai thứ
    khác nhau — phiên bản của **cụm** so với phiên bản của **schema**. Gộp lại thì
    một bản cài tắt kiểm schema (đang khôi phục, đang gỡ rối) cũng tắt luôn cổng
    phiên bản cụm mà không ai chủ ý làm thế."""

    verify_schema_on_startup: bool = True
    """Chặn khởi động khi schema DB lệch phiên bản migration (LD-05,
    FR-NFR-054). Chỉ đặt `False` cho test/CI không có PostgreSQL."""

    minimum_client_version: str = __version__
    """Bản client cũ nhất còn được **ghi** vào bản cài này (LD-05, FR-NFR-054).

    Mặc định bằng chính phiên bản server: một bản cài chưa ai chỉnh cấu hình sẽ
    đòi client cùng lứa, và đó là hướng mặc định đúng — nới ra là quyết định có
    chủ đích của người triển khai, còn siết vào là thứ họ sẽ quên làm.

    Chỉ tăng khi một migration **phá tương thích** với client cũ. Tăng theo mỗi
    bản phát hành thường là cách nhanh nhất để cả văn phòng dừng việc vì một
    máy trạm chưa kịp tự cập nhật.

    Client cũ hơn giá trị này vẫn **đọc** được (chế độ chỉ-đọc); chỉ lệnh ghi
    trả `426` — xem `api/middleware/schema_version_gate.py`.
    """

    updates_dir: Path | None = None
    """Thư mục chứa gói cập nhật client mà app server tự phục vụ (LD-05).

    Bản cài LAN **không có internet**, nên máy trạm không đi hỏi được CDN nào —
    app server là nơi duy nhất mọi máy trạm đều với tới. Đẩy gói lên bằng
    `python -m ket.admin publish-update`; lệnh đó ghi `index.json` trong thư mục
    này, và endpoint chỉ phục vụ tệp có tên trong danh mục ấy.

    `None` (mặc định) = **chưa bật đường tự cập nhật**. Endpoint vẫn tồn tại và
    trả `204` — máy trạm hiểu là "chưa có bản mới" và chạy bình thường. Hướng
    hỏng ngược lại, trả lỗi, sẽ dội một lỗi lên mọi máy trạm mỗi lần chúng kiểm
    cập nhật, và tiếng ồn đó che mất lỗi thật.
    """

    attachments_dir: Path | None = None
    """Thư mục gốc chứa tệp đính kèm của chứng từ và danh mục (FR-NFR-053).

    Tệp nằm **ngoài** cơ sở dữ liệu (DB chỉ giữ metadata), chia thư mục con theo
    schema dataset để một dữ liệu kế toán sao lưu/khôi phục được độc lập (RT-03).

    `None` (mặc định) = **chưa bật đường đính kèm**, và mọi endpoint đính kèm trả
    `503` kèm tên biến cần đặt. Không đoán hộ một đường dẫn: thư mục này phải nằm
    trong phạm vi sao lưu của khách hàng, mà chỗ ấy ở đâu thì chỉ người triển
    khai biết — một mặc định kiểu `./attachments` sẽ chạy êm suốt nhiều tháng rồi
    lộ ra vào đúng ngày cần khôi phục.
    """

    attachment_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1)
    """Trần dung lượng một tệp đính kèm (25 MiB).

    Đủ cho hợp đồng scan vài chục trang và ảnh chụp chứng từ — hai thứ chiếm gần
    hết lượng đính kèm thật. Trần này chặn **trong lúc ghi** (xem
    `kernel/attachments/storage.py`), không tin `Content-Length` client khai."""

    cors_allowed_origins: tuple[str, ...] = (
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    )
    """Origin được phép gọi API từ trình duyệt/webview (CORS).

    Cần vì webview của Tauri **không** chạy ở origin của app server: macOS dùng
    `tauri://localhost`, Windows dùng `http(s)://tauri.localhost`. Không khai ba
    origin đó thì client desktop không gọi được endpoint nào — trình duyệt chặn
    trước khi request rời máy, và triệu chứng là "đăng nhập không phản hồi" mà
    log server hoàn toàn trống.

    Chế độ trình duyệt trong LAN (v1.x) **không** cần mục nào ở đây: khi ấy app
    server tự phục vụ chính bundle web đó, nên request là same-origin.

    Danh sách đóng, không có `*`: hệ này giữ PII lương và bí mật doanh nghiệp,
    và `*` biến mọi trang web mà nhân viên mở thành một chỗ có thể gọi API nội
    bộ.

    Khai bằng **JSON**, không phải danh sách ngăn cách bằng dấu phẩy — đây là
    kiểu phức nên `pydantic-settings` phân tích bằng JSON, và một chuỗi có dấu
    phẩy làm tiến trình **không khởi động**. Giá trị khai ra **thay thế** cả
    danh sách này chứ không cộng thêm, nên máy lập trình chạy `vite dev` phải
    liệt kê lại ba origin Tauri:

        KET_CORS_ALLOWED_ORIGINS='["tauri://localhost","http://tauri.localhost",
                                   "https://tauri.localhost","http://localhost:5173"]'
    """

    @field_validator("minimum_client_version")
    @classmethod
    def _minimum_client_version_parses(cls, value: str) -> str:
        """Giá trị gõ sai phải chặn khởi động, không được im lặng thành vô hiệu.

        Kiểm ở đây chứ không ở middleware: middleware chạy khi đã có request,
        tức là triệu chứng đầu tiên của một dòng cấu hình sai sẽ là `500` trên
        máy người dùng thay vì một tiến trình không chịu khởi động.
        """
        parse_required_version(value)
        return value

    # --- Danh tính & bí mật (ADR-019, RT-05)
    keyring_service: str = "ket"
    """Tên dịch vụ trong OS keystore. Đổi khi cần chạy hai bản cài trên cùng một
    máy (dev + demo) mà không để chúng dùng chung khóa mã hóa."""

    app_key: SecretStr | None = None
    """Khóa Fernet dạng rõ, **ghi đè** OS keystore. Chỉ dùng cho test/CI và bản
    cài container — nơi keystore không có, hoặc thuộc về người dùng khác với
    người chạy dịch vụ. Trên máy khách thật, để trống và dùng
    `python -m ket.admin generate-app-key`."""

    session_ttl_minutes: int = 720
    """Tuổi thọ tuyệt đối của một phiên đăng nhập (12 giờ).

    Phủ trọn một ngày làm việc kể cả ca dài, nên người dùng bình thường không
    bao giờ bị đá ra giữa chừng; máy trạm bỏ quên qua đêm thì sáng hôm sau phải
    đăng nhập lại. Không có gia hạn trượt: gia hạn theo hoạt động nghĩa là một
    phiên bị chiếm sẽ tự nuôi mình sống mãi."""

    # --- Hợp đồng ghi (FR-NFR-004/005)
    idempotency_ttl_hours: int = Field(default=24, ge=1)
    """Khóa idempotency còn chống trùng trong bao lâu (RT-12).

    24 giờ = cửa sổ của **một lần gửi lại**, không phải của lịch sử. Sau đó khóa
    hết hiệu lực và lần gửi lại bị **từ chối** chứ không chạy lại — xem
    `kernel/idempotency/service.py`."""

    # --- Hạn mức request
    rate_limit_per_minute: int = Field(default=600, ge=0)
    """Trần request mỗi phút cho một người gọi (0 = tắt).

    600 = 10 request/giây, cao hơn nhiều lần nhịp của người nhập liệu nhanh
    nhất kể cả khi màn hình gọi song song nhiều endpoint. Ngưỡng này để bắt
    client hỏng và kịch bản chạy loạn, không để bóp người dùng thật."""

    rate_limit_auth_per_minute: int = Field(default=30, ge=0)
    """Trần riêng cho `/api/v1/auth/*` (0 = tắt).

    Chặt hơn hai chục lần vì đây là nơi mật khẩu bị thử. 30 lần/phút vẫn thừa
    cho người gõ sai vài lần rồi đổi mật khẩu, nhưng cắt hẳn đường dò từ một máy
    trạm trong LAN — nơi khóa theo tài khoản không giúp gì nếu kẻ dò rải đều
    qua nhiều tài khoản."""

    # --- Tác vụ nền (ADR-014, RT-13)
    worker_database_url: str = "postgresql+psycopg://ket_worker@localhost/ket"
    """DSN của **tiến trình worker**, đăng nhập bằng vai trò `ket_worker`.

    Vai trò riêng chứ không dùng lại `ket_app` (quyết định F2): worker phải thấy
    job của **mọi** chi nhánh để giành việc, và mở quyền đó cho vai trò runtime
    của API nghĩa là nới RLS cho mọi request. Quyền của `ket_worker` trong mỗi
    schema dataset dừng ở đúng bảng `jobs`; thân job chạy sau một
    `SET LOCAL ROLE ds_<mã>_app`, tức dưới đúng RLS như một request."""

    worker_owner_database_url: str | None = None
    """DSN `ket_owner` **chỉ** cho loại job khai `JobPrivilege.CONTROL_OWNER`.

    Mặc định `None` = worker **không** cầm quyền owner, và job cần nó (dọn phiên
    đăng nhập) hỏng với thông điệp chỉ đúng hai đường xử lý. Đó là hướng hỏng đã
    chọn: `DELETE` trên `auth_sessions` bị thu hồi khỏi vai trò runtime có chủ
    đích, nên cấp quyền owner cho một tiến trình chạy nền **theo mặc định** sẽ
    xóa chính bất biến đó. Bản cài nào muốn đặt lịch dọn phiên qua hàng đợi thì
    khai tường minh biến này."""

    worker_poll_seconds: int = Field(default=2, ge=1)
    """Nghỉ bao lâu khi mọi dữ liệu kế toán đều hết việc.

    2 giây: độ trễ không ai nhận ra khi bấm một tác vụ, mà vẫn đủ thưa để một
    bản cài để không cả đêm không tạo ra 40.000 truy vấn rỗng mỗi giờ cho mỗi
    dataset."""

    job_lease_seconds: int = Field(default=60, ge=5)
    """Một lần giành việc giữ job bao lâu nếu worker im lặng (RT-13)."""

    job_heartbeat_seconds: int = Field(default=15, ge=1)
    """Nhịp gia hạn lease trong lúc job chạy. Phải nhỏ hơn hẳn `job_lease_seconds`
    — kiểm ở `Settings` chứ không để reaper cướp job của một worker đang khỏe."""

    job_max_attempts: int = Field(default=3, ge=1)
    """Số lần một job được giành trước khi reaper đánh hỏng nó."""

    worker_reap_seconds: int = Field(default=30, ge=1)
    """Khoảng cách giữa hai lượt quét job hết lease."""

    worker_error_backoff_seconds: int = Field(default=15, ge=1)
    """Nghỉ bao lâu sau một vòng quét hỏng (DB khởi động lại, mạng chớp).

    Dài hơn nhịp nghỉ thường: nguyên nhân hay gặp nhất là PostgreSQL đang khởi
    động lại, và thử lại mỗi 2 giây chỉ đổ đầy log rồi vẫn phải chờ đúng ngần ấy
    thời gian."""

    # --- Vận hành
    debug: bool = False
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")

    @model_validator(mode="after")
    def _heartbeat_fits_inside_lease(self) -> Settings:
        """Nhịp heartbeat phải thưa hơn hẳn lease, nếu không reaper cướp job đang chạy.

        Kiểm ở đây chứ không ở worker: một cấu hình sai kiểu `lease=10,
        heartbeat=30` cho ra triệu chứng "job chạy mãi không xong, thỉnh thoảng
        chạy hai lần" — mất hàng giờ để lần ra, trong khi nó là một phép so sánh
        hai số. Hệ số 3 để một nhịp lỡ (GC, truy vấn nặng) chưa đủ mất lease.
        """
        if self.job_heartbeat_seconds * 3 > self.job_lease_seconds:
            raise ValueError(
                f"job_heartbeat_seconds={self.job_heartbeat_seconds} quá thưa so với "
                f"job_lease_seconds={self.job_lease_seconds}: lease phải dài ít nhất "
                "gấp ba nhịp heartbeat, nếu không tác vụ đang chạy bình thường vẫn bị "
                "coi là mồ côi và bị xếp lại hàng đợi."
            )
        return self


def get_settings() -> Settings:
    """Đọc cấu hình từ môi trường.

    Phase 2 sẽ bọc bằng cache theo tiến trình; phase 1 giữ đơn giản.
    """
    return Settings()
