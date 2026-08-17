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

from typing import Any, ClassVar


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

    def problem_extra(self) -> dict[str, Any]:
        """Trường phụ đưa thêm vào thân RFC 7807, ngoài `details`.

        Tồn tại cho đúng một loại lỗi: xung đột phiên bản phải trả kèm **bản
        ghi mới nhất** (FR-NFR-005), mà `details` chỉ nhận giá trị vô hướng —
        nó là tham số để client dựng câu thông báo, không phải chỗ chứa một bản
        ghi. Khai ở lớp lỗi, cùng lý do với `http_status`: một bảng ánh xạ đặt
        cạnh handler là thứ người thêm lớp lỗi mới sẽ quên.
        """
        return {}


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


class ClientVersionUnsupportedError(DomainError):
    """Client quá cũ (hoặc không khai phiên bản) mà đang gọi một lệnh ghi.

    426 Upgrade Required chứ không 403: đây không phải câu chuyện quyền — cùng
    tài khoản đó, cùng dữ liệu đó, chỉ cần bản client mới hơn là làm được. Mã
    riêng để client bắt đúng nhánh và hiện màn hình cập nhật thay vì màn hình
    "bạn không có quyền" (FR-NFR-054, LD-05).

    Lệnh **đọc** không bị chặn: một văn phòng đang chờ nâng cấp vẫn phải tra cứu
    được sổ sách. Chế độ chỉ-đọc là điều kiện để cổng này an toàn khi bật — chặn
    hoàn toàn thì một lần tăng `min_client_version` nhầm sẽ làm cả văn phòng
    dừng việc.
    """

    error_code: ClassVar[str] = "system.client_version_unsupported"
    http_status: ClassVar[int] = 426


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


class IdempotencyKeyMissingError(DomainError):
    """Endpoint đổi trạng thái nhưng request không mang `X-Idempotency-Key`.

    `400` chứ không `422`: thiếu ở đây là thiếu **header giao thức**, và phân
    biệt được hai loại giúp lập trình viên client biết phải sửa ở tầng nào —
    thân request hay lớp gửi HTTP.

    Bắt buộc chứ không "có thì tốt" (FR-NFR-004): mạng LAN của một doanh nghiệp
    có switch rẻ và cáp đi trần qua xưởng, nên mất phản hồi giữa chừng là
    chuyện xảy ra thật, và cách xử lý duy nhất mà người dùng biết là bấm lại.
    """

    error_code: ClassVar[str] = "idempotency.key_missing"
    http_status: ClassVar[int] = 400


class IdempotencyKeyInvalidError(DomainError):
    """Khóa có mặt nhưng không dùng được: rỗng, quá dài, hoặc chứa ký tự lạ.

    Kiểm ở cổng vào chứ không để `INSERT` tự đổ: một khóa dài hơn cột sẽ làm
    hỏng transaction **sau khi** lệnh ghi nghiệp vụ đã chạy, và biến một lỗi
    client đơn giản thành `500`.
    """

    error_code: ClassVar[str] = "idempotency.key_invalid"
    http_status: ClassVar[int] = 400


class IdempotencyKeyReusedError(DomainError):
    """Khóa đã dùng cho một yêu cầu khác (khác nội dung, hoặc khác người gửi)."""

    error_code: ClassVar[str] = "idempotency.key_reused"
    http_status: ClassVar[int] = 409


class IdempotencyKeyExpiredError(DomainError):
    """Khóa còn trong bảng nhưng đã quá hạn — từ chối, không chạy lại (RT-12)."""

    error_code: ClassVar[str] = "idempotency.key_expired"
    http_status: ClassVar[int] = 409


class IdempotencyRaceLostError(DomainError):
    """Hai yêu cầu cùng khóa chạy song song và không đọc lại được kết quả bên kia.

    Trạng thái hiếm: bên thắng phải vừa commit khóa vừa rollback ngay sau đó.
    Trả `409` để client thử lại bằng chính khóa cũ — an toàn, vì lần thử lại sẽ
    hoặc thấy kết quả cũ, hoặc thực hiện lần đầu tiên thật sự.
    """

    error_code: ClassVar[str] = "idempotency.race_lost"
    http_status: ClassVar[int] = 409


class DuplicateValueError(DomainError):
    """Giá trị đã có bản ghi khác dùng (mã danh mục, số chứng từ…).

    Tồn tại vì đây là **lỗi gõ tay thường gặp nhất** của người nhập liệu, và
    trước khi có nó, một mã chi nhánh trùng đi thẳng thành `500 lỗi không mong
    muốn + mã tham chiếu, gọi bộ phận hỗ trợ`. Từ phase 6 mỗi phân hệ có hàng
    chục ràng buộc duy nhất, nên hướng ánh xạ phải đúng ngay từ tầng chung.

    `details.constraint` nêu **tên ràng buộc** chứ không phải câu SQL: đủ để
    client dựng thông điệp đúng trường, không lộ cấu trúc bảng.
    """

    error_code: ClassVar[str] = "data.duplicate"
    http_status: ClassVar[int] = 409


class ReferenceNotFoundError(DomainError):
    """Tham chiếu tới một bản ghi không tồn tại (vi phạm khóa ngoại)."""

    error_code: ClassVar[str] = "data.reference_not_found"


class RowVersionConflictError(DomainError):
    """Bản ghi đã bị người khác sửa từ lúc client đọc nó (FR-NFR-005).

    Trả kèm **bản mới nhất** trong trường `latest` để màn hình hiện được "người
    kia vừa đổi gì" thay vì chỉ báo lỗi rồi bắt người dùng tự mở lại form —
    thao tác mà họ sẽ làm bằng cách bấm Lưu lần nữa.
    """

    error_code: ClassVar[str] = "concurrency.row_version_conflict"
    http_status: ClassVar[int] = 409

    def __init__(
        self, message: str, *, latest: dict[str, Any] | None = None, **details: str | int | None
    ) -> None:
        super().__init__(message, **details)
        self.latest = latest

    def problem_extra(self) -> dict[str, Any]:
        return {"latest": self.latest}


class RateLimitedError(DomainError):
    """Quá nhiều request trong một cửa sổ thời gian.

    Khác `AuthThrottledError` (trần tài nguyên băm mật khẩu, `503`): đây là hạn
    mức theo **người gọi**, nên `429` và kèm `Retry-After` để client tự giãn
    nhịp thay vì thử lại ngay lập tức.
    """

    error_code: ClassVar[str] = "system.rate_limited"
    http_status: ClassVar[int] = 429


class SettingUnknownError(DomainError):
    """Khóa tùy chọn không có trong catalog (FR-SYS-060).

    Catalog đóng chứ không cho ghi khóa tùy ý: một bảng key-value mở là nơi mọi
    thứ chưa kịp thiết kế sẽ rơi vào, và ba phase sau sẽ không ai biết khóa nào
    còn được đọc.
    """

    error_code: ClassVar[str] = "settings.key_unknown"
    http_status: ClassVar[int] = 404


class SettingScopeNotAllowedError(DomainError):
    """Tùy chọn này không khai báo cấp đang được ghi (user hoặc system)."""

    error_code: ClassVar[str] = "settings.scope_not_allowed"
    http_status: ClassVar[int] = 422


class SettingValueInvalidError(DomainError):
    """Giá trị không đúng kiểu đã khai của tùy chọn."""

    error_code: ClassVar[str] = "settings.value_invalid"


class JobTypeUnknownError(DomainError):
    """Loại job không có trong registry của tiến trình đang chạy.

    Xảy ra ở hai đầu, và ở đầu thứ hai nó là lỗi vận hành thật: client xếp hàng
    một loại đã bị gỡ, hoặc **worker chạy binary cũ hơn API** nên không biết loại
    vừa được thêm. Vì thế nó là lỗi có mã chứ không phải `KeyError` — worker
    đánh dấu job hỏng với thông điệp chỉ đúng nguyên nhân thay vì lặp lại mãi.
    """

    error_code: ClassVar[str] = "job.type_unknown"
    http_status: ClassVar[int] = 422


class JobParamsInvalidError(DomainError):
    """Tham số của job không khớp mô hình mà loại job đó khai.

    Kiểm ở lúc **xếp hàng** chứ không để worker phát hiện sau: một job hỏng vì
    sai tham số mà chỉ lộ ra sau vài phút trong hàng đợi là phản hồi tệ nhất có
    thể cho một thao tác người dùng vừa bấm.
    """

    error_code: ClassVar[str] = "job.params_invalid"


class JobNotFoundError(DomainError):
    """Không có job này trong dữ liệu kế toán đang mở (hoặc ngoài phạm vi chi nhánh)."""

    error_code: ClassVar[str] = "job.not_found"
    http_status: ClassVar[int] = 404


class JobNotCancellableError(DomainError):
    """Job đã kết thúc — không còn gì để hủy.

    Hủy là **yêu cầu** gửi cho worker, không phải lệnh giết: job đã `done`/
    `failed`/`cancelled` thì yêu cầu đó không có nơi nhận, và trả `409` trung
    thực hơn là im lặng gật đầu.
    """

    error_code: ClassVar[str] = "job.not_cancellable"
    http_status: ClassVar[int] = 409


class JobPrivilegeUnavailableError(DomainError):
    """Job đòi kết nối đặc quyền mà tiến trình worker không được cấu hình để có.

    Chỉ áp cho loại job khai `JobPrivilege.CONTROL_OWNER` (dọn phiên đăng nhập).
    Mặc định của bản cài là **không** cấu hình `worker_owner_database_url`, tức
    là worker không cầm quyền `ket_owner` — hỏng theo hướng đóng, và thông điệp
    nêu đúng hai đường đi tiếp: cấu hình DSN owner cho worker, hoặc chạy lệnh
    tương ứng của `python -m ket.admin`.
    """

    error_code: ClassVar[str] = "job.privilege_unavailable"
    http_status: ClassVar[int] = 503


class UpdatePackageNotFoundError(DomainError):
    """Máy trạm xin một gói cập nhật không có trong danh mục.

    Đường bình thường không bao giờ tới đây: updater chỉ tải đúng cái `url` mà
    manifest vừa trả cho nó. Tới đây nghĩa là ai đó gọi tay, hoặc kho vừa bị dọn
    giữa lúc một máy trạm đang tải — cả hai đều đáng trả `404` rõ ràng thay vì
    một luồng byte rỗng mà updater sẽ báo là "chữ ký sai".
    """

    error_code: ClassVar[str] = "system.update_package_not_found"
    http_status: ClassVar[int] = 404


class AttachmentStorageNotConfiguredError(DomainError):
    """Bản cài chưa trỏ `KET_ATTACHMENTS_DIR` tới thư mục nào (FR-NFR-053).

    `503` chứ không `500`: đây là một việc người quản trị làm được trong một
    phút, và thông điệp phải nói ra điều đó thay vì để người dùng nghĩ là phần
    mềm hỏng. Mặc định của bản cài là **chưa bật** — thư mục tệp đính kèm phải
    nằm trong phạm vi sao lưu, nên chọn chỗ cho nó là quyết định của người triển
    khai chứ không phải một đường dẫn ta đoán hộ.
    """

    error_code: ClassVar[str] = "attachment.storage_not_configured"
    http_status: ClassVar[int] = 503


class AttachmentTooLargeError(DomainError):
    """Tệp vượt trần dung lượng của bản cài."""

    error_code: ClassVar[str] = "attachment.too_large"
    http_status: ClassVar[int] = 413


class AttachmentEmptyError(DomainError):
    """Tệp rỗng.

    Chặn thay vì lưu: một tệp 0 byte gần như luôn là lỗi phía client (chọn nhầm,
    ổ mạng rớt giữa chừng), và nó sẽ nằm trong danh sách đính kèm trông y như
    một tệp thật cho tới lúc có người mở nó ra — thường là kiểm toán viên.
    """

    error_code: ClassVar[str] = "attachment.empty"


class AttachmentBranchRequiredError(DomainError):
    """Người dùng nhiều chi nhánh chưa chọn chi nhánh đang thao tác.

    Tệp đính kèm mang `branch_id` `NOT NULL` vì đó là neo cô lập RLS của nó
    (RT-04). Không đoán hộ chi nhánh khi tài khoản có nhiều: đoán sai nghĩa là
    tệp rơi vào ngăn của một chi nhánh khác và người đính nó không còn thấy nữa.
    """

    error_code: ClassVar[str] = "attachment.branch_required"


class AttachmentNotFoundError(DomainError):
    """Không có tệp đính kèm này trong dữ liệu kế toán đang mở (hoặc ngoài phạm vi chi nhánh)."""

    error_code: ClassVar[str] = "attachment.not_found"
    http_status: ClassVar[int] = 404


class AttachmentAlreadyAttachedError(DomainError):
    """Đúng tệp này đã đính vào đúng bản ghi này rồi.

    `409` chứ không im lặng gật đầu: người dùng vừa chọn nhầm cùng một tệp lần
    thứ hai, và câu trả lời hữu ích là "nó đã ở đây rồi" chứ không phải hai dòng
    giống hệt nhau trong danh sách đính kèm. Lần **gửi lại** thật sự (mất mạng
    rồi bấm lại) đi đường khác — khóa idempotency trả về chính bản ghi cũ.
    """

    error_code: ClassVar[str] = "attachment.already_attached"
    http_status: ClassVar[int] = 409


class AttachmentContentMissingError(DomainError):
    """Metadata còn nhưng tệp không có trên đĩa.

    `503` vì đây là sự cố phía máy chủ mà người quản trị sửa được (gắn lại ổ,
    khôi phục thư mục tệp), không phải "tệp đã bị xóa" — trả `404` sẽ khiến
    người dùng tin là mình mất tệp và đi tải lên lại, che mất một thư mục sao
    lưu chưa được gắn.
    """

    error_code: ClassVar[str] = "attachment.content_missing"
    http_status: ClassVar[int] = 503


class RequestBodyTooLargeError(DomainError):
    """Thân request vượt trần của bản cài (C1, FR-NFR-053).

    Khác `AttachmentTooLargeError` ở **nơi** phát hiện, và khác biệt đó có thật:
    lỗi này do middleware ném **trước khi** một byte nào chạm đĩa hoặc chạm tầng
    xác thực, còn `AttachmentTooLargeError` do kho tệp ném khi đã ghi tới ngưỡng.
    Hai lớp phòng thủ cho cùng một ngưỡng — lớp ngoài chặn kẻ chưa đăng nhập,
    lớp trong canh mọi đường gọi khác (job nhập liệu ở phase sau không đi qua
    HTTP).

    `413` ở cả hai, nên client xử lý y hệt nhau; mã lỗi khác nhau để nhật ký nói
    rõ request dừng ở đâu.
    """

    error_code: ClassVar[str] = "request.body_too_large"
    http_status: ClassVar[int] = 413


class AttachmentStorageUnavailableError(DomainError):
    """Thư mục tệp đính kèm có cấu hình nhưng máy chủ không ghi được vào đó.

    Ổ mạng chưa gắn, thư mục sai quyền, đĩa đầy. `503` cùng lý do với
    `AttachmentStorageNotConfiguredError`: người quản trị sửa được, và thông
    điệp phải nói ra điều đó thay vì để `500 "lỗi không mong muốn"` che mất.
    """

    error_code: ClassVar[str] = "attachment.storage_unavailable"
    http_status: ClassVar[int] = 503


class MasterDataNotFoundError(DomainError):
    """Không có bản ghi danh mục với mã/khóa đã cho."""

    error_code: ClassVar[str] = "master_data.not_found"
    http_status: ClassVar[int] = 404


class MasterDataInUseError(DomainError):
    """Danh mục đã xuất hiện trên chứng từ nên không xóa được (BR-SYS-02).

    `409` chứ không `422`: yêu cầu hợp lệ, chỉ là trạng thái hiện tại không cho
    phép — và trạng thái đó đổi được (chuyển chứng từ sang danh mục khác, hoặc
    dùng "Ngừng theo dõi" thay cho xóa, đúng lối FR-SYS-012 chỉ ra).

    `details.usage_count` để màn hình nói được "đang dùng ở 143 chứng từ" thay
    vì một câu từ chối trống rỗng mà người dùng không biết phải làm gì tiếp.
    """

    error_code: ClassVar[str] = "master_data.in_use"
    http_status: ClassVar[int] = 409


class MasterDataCycleError(DomainError):
    """Chuyển một nút vào chính nhánh con của nó (FR-SYS-011).

    Không phải lỗi lập trình mà là thao tác kéo-thả sai của người dùng trên cây
    danh mục, nên nó là lỗi nghiệp vụ có thông điệp — chặn ở đây thay vì để cây
    thành đồ thị có chu trình và mọi truy vấn nhánh chạy vô hạn.
    """

    error_code: ClassVar[str] = "master_data.parent_cycle"


class MasterDataParentScopeError(DomainError):
    """Gắn bản ghi vào một nhóm cha thuộc chi nhánh khác (FR-SYS-018).

    Danh mục dùng chung (`branch_id IS NULL`) phải nhìn thấy được từ mọi chi
    nhánh. Treo nó dưới một nhóm riêng của chi nhánh A thì nó vẫn nằm trong danh
    sách phẳng nhưng biến mất khỏi cây của mọi chi nhánh khác — một bản ghi
    "có mà không thấy", kiểu lỗi tốn nhiều giờ hỗ trợ nhất.
    """

    error_code: ClassVar[str] = "master_data.parent_scope_mismatch"


class MasterDataGroupNotPostableError(DomainError):
    """Chọn một nút nhóm ở chỗ chỉ nhận nút lá (`is_group = true`).

    Nhóm tồn tại để gom và để cộng tổng; hạch toán thẳng vào nhóm làm số liệu
    của nhóm không còn bằng tổng các con, và mọi báo cáo phân cấp lệch từ đó.
    """

    error_code: ClassVar[str] = "master_data.group_not_postable"


class ExchangeRateNotFoundError(DomainError):
    """Không có tỷ giá cho cặp (loại tiền, ngày, loại tỷ giá) (N5, FR-NFR-032).

    Cố ý **không** có giá trị mặc định. Mặc định 1 sẽ ghi một hóa đơn 5.000 USD
    thành 5.000 VND và không có gì trong sổ nói rằng tỷ giá bị thiếu — sai số
    đúng bằng ba bậc, phát hiện ra sau vài tháng khi đối chiếu công nợ.
    """

    error_code: ClassVar[str] = "currency.exchange_rate_not_found"


class BaseCurrencyMisconfiguredError(DomainError):
    """Không có đúng một đồng tiền hạch toán (`is_base`).

    Không có đồng nào thì không quy đổi được gì; có hai đồng thì mọi báo cáo
    tổng hợp phụ thuộc vào việc truy vấn trả về dòng nào trước.
    """

    error_code: ClassVar[str] = "currency.base_misconfigured"


class FiscalYearOverlapError(DomainError):
    """Năm tài chính mới chồng lấn năm đã có (FR-NFR-033).

    Nhiều năm tài chính song song là yêu cầu; **chồng lấn ngày** thì không —
    một chứng từ ngày 15/03 sẽ thuộc hai kỳ cùng lúc và số dư cuối kỳ tính ra
    hai giá trị khác nhau tùy đường truy vấn.
    """

    error_code: ClassVar[str] = "period.fiscal_year_overlap"


class PeriodNotFoundError(DomainError):
    """Ngày hạch toán không rơi vào kỳ kế toán nào đã khai."""

    error_code: ClassVar[str] = "period.not_found"


class PeriodClosedError(DomainError):
    """Kỳ đã khóa sổ — không ghi thêm, không sửa, không xóa (LD-12, FR-NFR-031).

    Khóa kỳ là cam kết với cơ quan thuế rằng số của kỳ đó đã cố định. Mở lại là
    thao tác có ghi vết của người có quyền, không phải một nhánh `if` mà đường
    ghi nào cũng có thể bỏ qua.
    """

    error_code: ClassVar[str] = "period.closed"


class AccountingSchemeLockedError(DomainError):
    """Đổi chế độ kế toán (TT200 ↔ TT133) trên năm đã có chứng từ (FR-SYS-004).

    Đây là nhóm thiết lập "chốt một lần" của nguyên tắc U14: hệ thống tài khoản,
    layout báo cáo tài chính và mẫu tờ khai đều dẫn xuất từ chế độ, nên đổi giữa
    chừng làm chứng từ đã ghi trỏ vào những tài khoản không còn tồn tại.
    """

    error_code: ClassVar[str] = "period.accounting_scheme_locked"


class NumberSequenceNotFoundError(DomainError):
    """Chưa khai quy tắc đánh số cho loại chứng từ / phạm vi này (FR-SYS-063).

    Không tự tạo dãy số với giá trị đoán: tiền tố và độ dài số chứng từ là thứ
    doanh nghiệp đã đăng ký và in trên giấy, nên một dãy sinh ngầm sẽ tạo ra
    những số chứng từ không ai nhận ra là của mình.
    """

    error_code: ClassVar[str] = "numbering.sequence_not_found"
