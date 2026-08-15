"""Lỗi nghiệp vụ có mã ổn định.

Hợp đồng lỗi (plan.md §Quy ước REST API, FR-NFR-050): server trả **mã lỗi**
`error_code` + tham số `details`, client dựng câu tiếng Việt/Anh từ đó. Server
không bao giờ trả traceback thô ra ngoài.

`error_code` là **hợp đồng công khai** — đổi mã là breaking change với client
và với thông điệp đã dịch. Thêm mã mới thì thêm lớp con ở đây.

Bộ chuyển `DomainError` → RFC 7807 `application/problem+json` nằm ở
`api/middleware/problem_details.py` (bước 12 của phase 2, slice sau). Lớp gốc
đặt ở kernel ngay từ bây giờ để mọi tầng ném cùng một loại lỗi.
"""

from __future__ import annotations

from typing import ClassVar


class DomainError(Exception):
    """Lỗi nghiệp vụ người dùng sửa được (≠ lỗi lập trình).

    Lỗi lập trình (gọi sai hàm, sai kiểu, sai bất biến nội bộ) vẫn dùng
    `ValueError`/`TypeError`/`AssertionError` — chúng phải nổ to và lộ ra log,
    không được gói thành thông điệp dịu cho người dùng.
    """

    error_code: ClassVar[str] = "domain_error"

    http_status: ClassVar[int] = 422
    """Mã HTTP mà `api/middleware/problem_details.py` trả cho lớp lỗi này.

    Khai **ở lớp lỗi** chứ không phải bằng một bảng ánh xạ đặt cạnh handler: bảng
    ánh xạ là thứ người thêm lớp lỗi mới sẽ quên cập nhật, và cái giá của việc
    quên là một lỗi xác thực trả về 422 — client hiểu thành "dữ liệu sai" và
    không bật lại màn hình đăng nhập.

    422 là mặc định đúng cho phần lớn lỗi nghiệp vụ (yêu cầu hợp lệ về cú pháp
    nhưng vi phạm luật nghiệp vụ)."""

    def __init__(self, message: str, **details: str | int | None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, str | int | None] = details

    def __str__(self) -> str:
        return self.message


class InvalidSchemaNameError(DomainError):
    """Tên schema dataset không hợp lệ.

    Tên schema đi thẳng vào câu lệnh SQL (identifier không tham số hóa được),
    nên nó là **ranh giới tin cậy** — mọi đường vào phải qua
    `ket.kernel.datasets.naming.validate_schema_name`.
    """

    error_code: ClassVar[str] = "dataset.schema_name_invalid"


class DatasetNotFoundError(DomainError):
    """Không có dữ liệu kế toán với mã này trong control schema."""

    error_code: ClassVar[str] = "dataset.not_found"


class DatasetAlreadyExistsError(DomainError):
    """Mã dữ liệu kế toán đã tồn tại (FR-SYS-001)."""

    error_code: ClassVar[str] = "dataset.already_exists"


class DatasetRoleNotAdministrableError(DomainError):
    """Vai trò của dataset đã tồn tại nhưng `ket_owner` không quản trị được nó.

    PostgreSQL 16 đòi ADMIN OPTION mới `ALTER`/`GRANT` được một vai trò, và
    `ket_owner` chỉ có ADMIN với vai trò do **chính nó** tạo. Trạng thái này xuất
    hiện khi vai trò được dựng bởi superuser — khôi phục `pg_dumpall
    --globals-only`, hoặc cài lại `ket_owner`.

    Báo lỗi rõ thay vì để `permission denied to alter role` thô nổi lên giữa
    chừng: cách sửa (`GRANT <role> TO ket_owner WITH ADMIN OPTION` bằng
    superuser) không hề suy ra được từ thông điệp gốc.
    """

    error_code: ClassVar[str] = "dataset.role_not_administrable"


class AuditContextMissingError(DomainError):
    """Có thay đổi trên bảng cần ghi nhật ký nhưng transaction không khai người thực hiện.

    Fail-closed có chủ đích (FR-NFR-012/013): thà chặn thao tác còn hơn ghi
    một thay đổi không có vết. Sửa bằng cách mở transaction qua
    `ket.kernel.persistence.unit_of_work.unit_of_work`, không phải bằng cách
    nới điều kiện ở listener.
    """

    error_code: ClassVar[str] = "audit.context_missing"


class UnsupportedPostgresVersionError(DomainError):
    """Cụm PostgreSQL cũ hơn phiên bản đích của bản cài (D4).

    Từ chối khởi động thay vì chạy tiếp: SQL của các phase sau viết theo phiên
    bản đích, và một câu lệnh không được hỗ trợ sẽ nổ đúng lúc đang chạy — giữa
    kỳ khóa sổ chẳng hạn — thay vì lúc cài đặt.
    """

    error_code: ClassVar[str] = "system.postgres_version_unsupported"


class SchemaVersionMismatchError(DomainError):
    """Schema DB lệch phiên bản migration mà mã nguồn đang mong đợi.

    App server **từ chối khởi động** trong trường hợp này (LD-05, FR-NFR-054):
    một binary cũ ghi sổ vào schema mới (hoặc ngược lại) là con đường ngắn nhất
    tới dữ liệu hỏng âm thầm.
    """

    error_code: ClassVar[str] = "system.schema_version_mismatch"


class AppKeyUnavailableError(DomainError):
    """Không lấy được khóa mã hóa của ứng dụng từ OS keystore (ADR-019, RT-05).

    Fail-closed: thà từ chối thao tác chạm bí mật còn hơn ghi `totp_secret`
    dạng rõ vào DB — dạng rõ thì bản sao lưu cũng mang theo, và không có đường
    thu hồi về sau.

    503 chứ không 500: đây là **thiếu cấu hình vận hành** (chưa chạy
    `ket.admin generate-app-key`, hoặc keystore của máy chưa mở khóa), sửa được
    mà không cần sửa mã.
    """

    error_code: ClassVar[str] = "system.app_key_unavailable"
    http_status: ClassVar[int] = 503


class NotAuthenticatedError(DomainError):
    """Thiếu token phiên, token sai, đã thu hồi hoặc đã hết hạn.

    Một mã lỗi duy nhất cho cả bốn nguyên nhân: phân biệt "token không tồn tại"
    với "token đã thu hồi" chỉ giúp người dò token, không giúp người dùng thật —
    họ đăng nhập lại trong cả bốn trường hợp.
    """

    error_code: ClassVar[str] = "auth.not_authenticated"
    http_status: ClassVar[int] = 401


class InvalidCredentialsError(DomainError):
    """Sai tên đăng nhập hoặc mật khẩu, hoặc tài khoản đã bị vô hiệu hóa.

    Gộp ba nguyên nhân có chủ đích, và đường mã cũng phải **tốn công như nhau**
    ở cả ba (xem `passwords.verify_dummy`), nếu không thời gian phản hồi sẽ trả
    lời thay cho thông điệp.
    """

    error_code: ClassVar[str] = "auth.invalid_credentials"
    http_status: ClassVar[int] = 401


class AccountLockedError(DomainError):
    """Tài khoản tạm khóa vì quá nhiều lần đăng nhập sai.

    Nói thẳng thay vì gộp vào `InvalidCredentialsError`: hệ này chạy trong LAN
    một doanh nghiệp với 5–50 người dùng có tên biết trước, nên "giấu sự tồn tại
    của tài khoản" không bảo vệ được gì thật, trong khi một kế toán viên gõ sai
    mật khẩu ba lần rồi bị từ chối im lặng sẽ gọi hỗ trợ.
    """

    error_code: ClassVar[str] = "auth.account_locked"
    http_status: ClassVar[int] = 401


class TotpRequiredError(DomainError):
    """Mật khẩu đúng nhưng tài khoản bắt buộc 2FA và request chưa kèm mã.

    Client dùng mã lỗi này để hiện ô nhập mã — nên nó **phải** khác
    `InvalidCredentialsError`.
    """

    error_code: ClassVar[str] = "auth.totp_required"
    http_status: ClassVar[int] = 401


class InvalidTotpCodeError(DomainError):
    """Mã 2FA sai hoặc đã hết cửa sổ hiệu lực."""

    error_code: ClassVar[str] = "auth.totp_code_invalid"
    http_status: ClassVar[int] = 401


class TotpCodeReusedError(DomainError):
    """Mã 2FA đúng nhưng đã dùng rồi (FR-NFR-016).

    Một mã sống 30 giây và cửa sổ chấp nhận rộng hơn thế. Không chặn dùng lại
    thì ai đọc trộm được mã — qua vai, qua log, qua một phiên chia sẻ màn hình —
    vẫn dùng lại được trong cùng cửa sổ đó, và lớp thứ hai chỉ còn là hình thức.
    """

    error_code: ClassVar[str] = "auth.totp_code_reused"
    http_status: ClassVar[int] = 401


class WeakPasswordError(DomainError):
    """Mật khẩu không đạt chính sách tối thiểu (FR-NFR-010).

    `details` nêu **luật nào** bị vi phạm để client dựng được câu tiếng Việt cụ
    thể, thay vì một câu chung chung khiến người dùng thử mò.
    """

    error_code: ClassVar[str] = "auth.password_too_weak"


class UserNotFoundError(DomainError):
    """Không có người dùng với tên đăng nhập này (đường quản trị/CLI).

    Khác `InvalidCredentialsError`: đường quản trị **được** biết tài khoản có
    tồn tại hay không, còn đường đăng nhập thì không.
    """

    error_code: ClassVar[str] = "auth.user_not_found"
    http_status: ClassVar[int] = 404


class UserAlreadyExistsError(DomainError):
    """Tên đăng nhập đã có người dùng."""

    error_code: ClassVar[str] = "auth.user_already_exists"
    http_status: ClassVar[int] = 409


class AuthThrottledError(DomainError):
    """Quá nhiều lần băm mật khẩu cùng lúc — máy chủ từ chối để tự bảo vệ.

    Băm Argon2id tốn 64 MiB mỗi lần. FastAPI chạy handler đồng bộ trong
    threadpool (mặc định 40 luồng), nên không có hàng rào nào thì 40 lần đăng
    nhập song song đòi ~2,5 GB trên đúng cái máy đang chạy PostgreSQL ở chế độ
    một-máy — và không cần tài khoản hợp lệ, vì nhánh "không có tài khoản" cũng
    băm (`passwords.verify_dummy`).

    503 chứ không 401: đây không phải câu trả lời về danh tính. Người dùng thật
    thử lại sau vài giây là được.
    """

    error_code: ClassVar[str] = "auth.throttled"
    http_status: ClassVar[int] = 503


class SessionScopeLimitedError(DomainError):
    """Phiên hiện tại chỉ dùng được cho một việc, và đây không phải việc đó.

    Phiên `totp_enrollment` được cấp cho người bắt buộc 2FA mà chưa đăng ký
    thiết bị: không có nó thì họ không đăng nhập được, mà không đăng nhập được
    thì cũng không đăng ký được — tự khóa mình ra ngoài. Phiên đó **chỉ** mở
    đúng các endpoint đăng ký thiết bị; mọi đường khác dừng ở đây.
    """

    error_code: ClassVar[str] = "auth.session_scope_limited"
    http_status: ClassVar[int] = 403


class PasswordChangeRequiredError(DomainError):
    """Tài khoản đang mang mật khẩu tạm, phải đổi trước khi làm việc khác.

    Ép ở **server** chứ không chỉ báo cho client (FR-SYS-075): mật khẩu tạm do
    người khác đặt và đã đi qua một kênh nào đó, nên nó không còn là bí mật của
    riêng chủ tài khoản. Một client tự viết bỏ qua cờ này là chuyện của mười
    dòng mã.
    """

    error_code: ClassVar[str] = "auth.password_change_required"
    http_status: ClassVar[int] = 403


class DatasetHeaderMissingError(DomainError):
    """Endpoint nghiệp vụ nhưng request không nói đang mở dữ liệu kế toán nào.

    Không có mặc định "dataset đầu tiên": đoán sai nghĩa là ghi sổ nhầm doanh
    nghiệp, và đó là loại sai không ai phát hiện cho tới kỳ quyết toán.
    """

    error_code: ClassVar[str] = "dataset.header_missing"
    http_status: ClassVar[int] = 400


class DatasetAccessDeniedError(DomainError):
    """Người dùng không có vai trò nào trong dữ liệu kế toán này.

    Khác `PermissionDeniedError` ở chỗ nó trả lời câu hỏi lớn hơn: không phải
    "thiếu một quyền" mà là "không thuộc về doanh nghiệp này". Client dùng nó để
    ẩn hẳn dataset khỏi danh sách chọn thay vì hiện rồi báo lỗi ở từng màn hình.
    """

    error_code: ClassVar[str] = "dataset.access_denied"
    http_status: ClassVar[int] = 403


class PermissionDeniedError(DomainError):
    """Thiếu quyền cho hành vi này (FR-NFR-011, FR-SYS-071).

    `details.permission` nêu **mã quyền** còn thiếu — quản trị viên đọc thông
    điệp là biết phải cấp gì, thay vì phải dò trong bảng phân quyền.
    """

    error_code: ClassVar[str] = "auth.permission_denied"
    http_status: ClassVar[int] = 403


class BranchNotInScopeError(DomainError):
    """Chi nhánh đang thao tác nằm ngoài phạm vi được gán (FR-SYS-072)."""

    error_code: ClassVar[str] = "auth.branch_not_in_scope"
    http_status: ClassVar[int] = 403


class RoleNotFoundError(DomainError):
    """Không có vai trò với mã này trong dữ liệu kế toán đang mở."""

    error_code: ClassVar[str] = "auth.role_not_found"
    http_status: ClassVar[int] = 404


class BranchNotFoundError(DomainError):
    """Không có chi nhánh với mã này trong dữ liệu kế toán đang mở."""

    error_code: ClassVar[str] = "org.branch_not_found"
    http_status: ClassVar[int] = 404
