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


class RoleGrantTooWideError(DomainError):
    """Gán một vai trò mang quyền mà chính người thực hiện không có.

    Cùng luật với `BranchNotInScopeError` nhưng cho trục **quyền** thay vì trục
    chi nhánh: giữ `system.role.edit` nghĩa là được phân phát phần quyền mình
    đang có, không phải được tự nâng mình (hay đồng minh) lên `admin` trong một
    request (audit phase 1–3, H-1 trục bảo mật). Đường phá-kính tại máy chủ
    (`ket.admin grant-role`) được miễn — ai chạm được máy chủ thì đã chạm được DB.
    """

    error_code: ClassVar[str] = "auth.role_grant_too_wide"
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


class MasterDataParentNotGroupError(DomainError):
    """Chọn một nút **lá** làm nhóm cha (`is_group = false`).

    Chiều ngược của `MasterDataGroupNotPostableError`: lá tồn tại để hạch toán,
    nhóm tồn tại để gom. Một nút lá vừa có phát sinh vừa có con biến câu hỏi
    "số của nút này là số của chính nó hay tổng các con?" thành không trả lời
    được — và mọi báo cáo cộng dồn theo nhóm (LD-08) lệch từ đó. Chặn ở tầng
    dịch vụ vì mọi đường ghi cây (tạo, chuyển cha, nhập Excel) đều phải qua
    cùng một luật (audit phase 1–3, finding H2 trục kernel).
    """

    error_code: ClassVar[str] = "master_data.parent_not_group"


class MasterDataGroupNotPostableError(DomainError):
    """Chọn một nút nhóm ở chỗ chỉ nhận nút lá (`is_group = true`).

    Nhóm tồn tại để gom và để cộng tổng; hạch toán thẳng vào nhóm làm số liệu
    của nhóm không còn bằng tổng các con, và mọi báo cáo phân cấp lệch từ đó.
    """

    error_code: ClassVar[str] = "master_data.group_not_postable"


class MasterDataFilterUnknownError(DomainError):
    """`?flag=` mang một giá trị danh mục này không khai (`CatalogFlag`).

    Trả lỗi thay vì lặng lẽ bỏ qua bộ lọc: bỏ qua nghĩa là danh sách "khách
    hàng" trả về cả nhà cung cấp khi client gõ sai một chữ, và không ai phát
    hiện cho tới lúc có người gửi nhầm thư mời.
    """

    error_code: ClassVar[str] = "master_data.filter_unknown"


class MasterDataMergeRefusedError(DomainError):
    """Gộp hai bản ghi danh mục nhưng trạng thái hiện tại không cho phép (FR-SYS-016).

    `409` cùng lý do với `MasterDataInUseError`: yêu cầu hợp lệ, chỉ là phải làm
    một việc khác trước (chuyển nhánh con đi, gỡ tệp đính kèm, chọn bản ghi đích
    có phạm vi chi nhánh phủ được bản ghi nguồn). `details.reason` nêu **việc
    phải làm**, vì gộp là thao tác không hoàn tác được và một câu từ chối trống
    rỗng sẽ đẩy người dùng đi thử lại một cách ngẫu nhiên.
    """

    error_code: ClassVar[str] = "master_data.merge_refused"
    http_status: ClassVar[int] = 409


class ItemUnitDuplicatesBaseError(DomainError):
    """Khai đơn vị quy đổi trùng với **đơn vị chính** của mã hàng (FR-SYS-041).

    Đơn vị chính luôn có tỷ lệ 1 và không nằm trong bảng đơn vị quy đổi. Nhận
    dòng này sẽ tạo ra hai tỷ lệ cho cùng một đơn vị — một cái ngầm định bằng 1,
    một cái người dùng khai — và không câu truy vấn nào chọn được cái đúng.
    """

    error_code: ClassVar[str] = "item.unit_duplicates_base"


class ItemBaseUnitMissingError(DomainError):
    """Khai đơn vị quy đổi cho mã hàng **chưa có đơn vị chính** (FR-SYS-041).

    Tỷ lệ quy đổi mang nghĩa "bao nhiêu đơn vị chính cho một đơn vị này", nên khi
    chưa có đơn vị chính thì con số ấy không quy về đâu cả. Dịch vụ và dòng diễn
    giải được phép không có đơn vị chính; chúng cũng không có đơn vị quy đổi.

    `409`: yêu cầu hợp lệ, chỉ là phải làm một việc khác trước — mà việc đó là
    khai lại mã hàng, vì đơn vị chính chốt một lần lúc tạo (H69).
    """

    error_code: ClassVar[str] = "item.base_unit_missing"
    http_status: ClassVar[int] = 409


class ItemWarehouseNotAllowedError(DomainError):
    """Đặt kho ngầm định cho mã hàng **không** đi qua kho (SRS §6.2 tab Ngầm định).

    Có lớp lỗi riêng cho đường **sửa** vì đường **tạo** bắt cùng luật này ở thân
    request (validator của `ItemFields`) và trả câu tiếng Việt; không có nó thì
    đường sửa rơi xuống `CHECK` phía DB và trả về tên một ràng buộc nội bộ cho
    cùng một sai sót — xem `registry.UpdateGuard`.
    """

    error_code: ClassVar[str] = "item.warehouse_not_allowed"


class ItemVariantNotSupportedError(DomainError):
    """Khai mã quy cách cho mã hàng **không theo dõi tồn kho** (FR-SYS-046).

    Quy cách là một trục của báo cáo tồn kho (xem `models/item_variant.py`), nên
    quy cách của một dịch vụ là một chiều phân tích không có gì để phân tích —
    không màn hình nào hiển thị nó và không báo cáo nào cộng theo nó.
    """

    error_code: ClassVar[str] = "item.variant_not_supported"
    http_status: ClassVar[int] = 409


class DimensionNotFoundError(DomainError):
    """Không có chiều phân tích mở rộng với mã này (LD-08, FR-SYS-051)."""

    error_code: ClassVar[str] = "dimension.not_found"
    http_status: ClassVar[int] = 404


class DimensionValueNotFoundError(DomainError):
    """Giá trị không thuộc chiều đã cho, hoặc đã ngừng theo dõi.

    Gộp "không có" với "đã ngừng theo dõi" vào một mã, khác lối `MasterData*`
    tách đôi: ở đây cả hai đều dẫn tới đúng một việc người dùng phải làm — chọn
    một giá trị khác trong ô chọn. Tách mã chỉ có ích khi hai nhánh dẫn tới hai
    hành động khác nhau.
    """

    error_code: ClassVar[str] = "dimension.value_not_found"
    http_status: ClassVar[int] = 404


class DimensionValueSourceMismatchError(DomainError):
    """Thao tác không khớp nguồn giá trị của chiều (`list` vs `master`).

    Ví dụ: xin danh sách giá trị riêng của một chiều vốn lấy giá trị từ danh mục
    vật tư, hoặc khai một chiều `master` trỏ tới một `slug` không có thật. Cả hai
    là lỗi **cấu hình**, và chúng lộ ra ở lúc khai chứ không phải lúc có người mở
    ô chọn rồi thấy nó rỗng.
    """

    error_code: ClassVar[str] = "dimension.value_source_mismatch"


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


class IntegrityCheckUnknownError(DomainError):
    """Yêu cầu chạy một phép kiểm toàn vẹn không có trong registry."""

    error_code: ClassVar[str] = "integrity.check_unknown"


class PeriodNotLockedError(DomainError):
    """Mở khóa một kỳ đang mở — không có gì để mở.

    Lỗi riêng chứ không phải no-op im lặng: "mở kỳ" là thao tác có quyền riêng
    và có lý do bắt buộc; một lượt gọi vào kỳ đang mở gần như chắc chắn là nhầm
    kỳ, và nhật ký của một thao tác không-làm-gì sẽ đánh lừa người kiểm toán.
    """

    error_code: ClassVar[str] = "period.not_locked"


class PeriodLockSequenceError(DomainError):
    """Khóa/mở kỳ phải tuần tự trong một niên độ (bàn giao 4B→4D, review L-B).

    Recalc snapshot viết lại cả kỳ **đã khóa** khi kỳ trước nó đổi — đúng thiết
    kế, nhưng chỉ an toàn khi "đã khóa" đồng nghĩa "mọi kỳ trước cũng đã khóa":
    lúc đó không còn đường ghi nào đổi được kỳ trước nữa. Khóa kỳ 2 khi kỳ 1
    còn mở phá đúng bất biến đó; mở kỳ 3 khi kỳ 4 còn khóa cũng vậy, ở chiều
    ngược lại.
    """

    error_code: ClassVar[str] = "period.lock_out_of_sequence"


class PeriodRecalcPendingError(DomainError):
    """Khóa kỳ khi hàng đợi tính lại còn dấu bẩn chạm tới kỳ đó.

    Số của kỳ đã khóa là số **đã chốt**; một dấu bẩn còn nằm trong hàng đợi
    nghĩa là snapshot của kỳ sẽ còn đổi sau khi khóa — báo cáo in ra trước và
    sau lượt tính lại sẽ khác nhau. Chạy tính lại số dư cho hết dấu bẩn rồi
    khóa (phase-04 §RT-09: kiểm **bên trong** transaction khóa, không phải
    trước đó).
    """

    error_code: ClassVar[str] = "period.recalc_pending"


class PeriodLockChecklistError(DomainError):
    """Một mục **chặn** trong danh mục kiểm tra khóa sổ chưa xanh (U11).

    Phase 4 mới có một mục chặn: số dư ban đầu của năm phải cân Nợ/Có trước khi
    khóa kỳ đầu năm — 4C ghi lệch là *cảnh báo* đúng chữ FR-OPB-006, và chặn
    cứng được hoãn tới cổng khóa sổ này. Danh mục đầy đủ (bút toán kết chuyển,
    đối chiếu tồn kho…) thuộc phase 10a; mỗi mục thêm vào đây phải kèm `details`
    chỉ thẳng chỗ sửa.
    """

    error_code: ClassVar[str] = "period.lock_checklist_failed"


class PeriodLockScopeError(DomainError):
    """Khóa kỳ đòi phạm vi mọi chi nhánh của dữ liệu kế toán.

    Kỳ kế toán không thuộc chi nhánh nào — khóa nó là chốt số của **tất cả**
    chi nhánh cùng lúc. Phép kiểm "hàng đợi tính lại rỗng" và "chứng từ chưa
    ghi sổ" chạy dưới RLS, nên một phạm vi thiếu chi nhánh sẽ nhìn hàng đợi
    của chi nhánh vắng mặt như thể rỗng — khóa mà không thấy việc còn dở
    (bàn giao 4C→4D).
    """

    error_code: ClassVar[str] = "period.lock_scope_incomplete"


class AccountingSchemeLockedError(DomainError):
    """Đổi chế độ kế toán (TT99 ↔ TT133) trên năm đã có chứng từ (FR-SYS-004).

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


# ---------------------------------------------------------------------------
# Nhập liệu Excel (lát 3C-1, FR-SYS-080..085)
#
# Bốn lỗi đầu là lỗi **của cả tệp**: chúng dừng lượt nhập trước khi có dòng nào
# được xét. Sai sót của từng dòng KHÔNG ném ra ở đây — chúng đi vào
# `ImportReport.errors` kèm số dòng và tên cột, vì một tệp 10.000 dòng có thể có
# 10.000 sai sót và người dùng cần đọc chúng cùng lúc chứ không phải từng cái
# một qua mười nghìn lượt gửi lại.
# ---------------------------------------------------------------------------


class ImportFileUnreadableError(DomainError):
    """Tệp không mở được: hỏng, mã hóa, hoặc là .xls cũ đổi đuôi thành .xlsx."""

    error_code: ClassVar[str] = "import.file_unreadable"


class ImportSheetMissingError(DomainError):
    """Tệp không có sheet dữ liệu đúng tên (FR-SYS-082).

    Trả kèm danh sách sheet **đang có** để câu thông báo nói được "tệp đang có
    `Sheet1`" — không có nó thì người dùng phải tự mở tệp ra so tên bằng mắt,
    đúng việc mà máy chủ vừa làm xong.
    """

    error_code: ClassVar[str] = "import.sheet_missing"

    def __init__(
        self, message: str, *, expected: str, found: list[str], **details: str | int | None
    ) -> None:
        super().__init__(message, expected=expected, **details)
        self.found = found

    def problem_extra(self) -> dict[str, Any]:
        return {"found_sheets": self.found}


class ImportTemplateMismatchError(DomainError):
    """Dòng tiêu đề lệch khỏi tệp mẫu (FR-SYS-082, bước 13).

    Bốn danh sách chứ không một câu: `missing` và `unexpected` là thứ người dùng
    sửa được ngay, còn `expected`/`found` cho màn hình dựng được bảng so sánh
    hai cột. Gộp chúng thành một chuỗi ở máy chủ là quyết định thay client cách
    trình bày một thứ mà client trình bày tốt hơn nhiều.
    """

    error_code: ClassVar[str] = "import.template_mismatch"

    def __init__(
        self,
        message: str,
        *,
        expected: list[str],
        found: list[str],
        missing: list[str],
        unexpected: list[str],
        **details: str | int | None,
    ) -> None:
        super().__init__(message, **details)
        self.expected = expected
        self.found = found
        self.missing = missing
        self.unexpected = unexpected

    def problem_extra(self) -> dict[str, Any]:
        return {
            "expected_columns": self.expected,
            "found_columns": self.found,
            "missing_columns": self.missing,
            "unexpected_columns": self.unexpected,
        }


class ImportTooManyRowsError(DomainError):
    """Tệp vượt trần số dòng của một lượt nhập."""

    error_code: ClassVar[str] = "import.too_many_rows"


class BankStatementFileUnreadableError(DomainError):
    """Tệp sao kê không mở được (hỏng, sai định dạng, sai bảng mã)."""

    error_code: ClassVar[str] = "bank_statement.file_unreadable"


class BankStatementColumnMissingError(DomainError):
    """Hồ sơ khai một cột mà tệp sao kê không có.

    Lỗi **cấu trúc**, nên nó dừng cả lượt thay vì thành một dòng lỗi: hồ sơ không
    khớp tệp thì mọi dòng đều hỏng theo cùng một cách, và vài nghìn dòng lỗi
    giống hệt nhau che mất đúng một việc người dùng phải làm — sửa hồ sơ, hoặc
    nộp đúng tệp.
    """

    error_code: ClassVar[str] = "bank_statement.column_missing"


class BankStatementColumnAmbiguousError(DomainError):
    """Tệp sao kê có nhiều cột cùng tên mà hồ sơ trỏ tới.

    Lỗi **cấu trúc** như `BankStatementColumnMissingError`: lấy đại cột đầu tiên
    là đúng loại lỗi mà việc định vị theo tên sinh ra để tránh — mọi dòng vẫn
    đọc được, mọi con số lấy từ nhầm cột.
    """

    error_code: ClassVar[str] = "bank_statement.column_ambiguous"


class BankStatementFormatUnsupportedError(DomainError):
    """Dạng tệp khai trong hồ sơ chưa có bộ đọc trong bản cài này (MT940)."""

    error_code: ClassVar[str] = "bank_statement.format_unsupported"


class ExportTooManyRowsError(DomainError):
    """Danh mục có nhiều dòng hơn mức một tệp xuất chứa được.

    Ném ở **hai** ngưỡng, và cả hai đều phục vụ một bất biến: tệp xuất ra phải
    nhập lại được. `exporter.MAX_EXPORT_ROWS` chặn theo số dòng *trước* khi dựng
    tệp; phép đo byte sau giải nén chặn theo dung lượng *sau* khi dựng — cần cả
    hai vì số byte mỗi dòng đổi theo **nội dung** (chữ tiếng Việt tốn 3 byte
    UTF-8 mỗi ký tự), nên không hằng số nào đoán trước được nó.
    """

    error_code: ClassVar[str] = "export.too_many_rows"


class ImportSourceNotValidatedError(DomainError):
    """Bước ghi trỏ vào một lượt kiểm không dùng được (H78).

    Bốn tình huống chung một mã vì với người dùng chúng là **một**: lượt kiểm
    không còn hợp lệ, và việc phải làm là kiểm lại. Tách mã ra sẽ là bốn câu
    thông báo cho cùng một nút bấm.
    """

    error_code: ClassVar[str] = "import.source_not_validated"


class ImportFileMissingError(DomainError):
    """Tệp đã kiểm không còn trong kho — khôi phục dở dang, hoặc đã bị dọn."""

    error_code: ClassVar[str] = "import.file_missing"


class JobNotDirectlyEnqueueableError(DomainError):
    """Loại job này phải xếp hàng qua endpoint riêng của nó (`JobType.direct_enqueue`).

    Không phải lỗi phân quyền: người gọi **có** quyền dùng loại job đó. Điều
    thiếu là phạm vi — endpoint riêng biết tham số nên kiểm được quyền trên đúng
    đối tượng, còn endpoint hàng đợi chung thì không.
    """

    error_code: ClassVar[str] = "job.not_directly_enqueueable"


class ImportParentUnresolvableError(DomainError):
    """Còn dòng không tạo được sau khi đã duyệt hết các cấp cây.

    Nguyên nhân là một câu hỏi về **đồ thị**, không về từng dòng: `parent_code`
    tạo chu trình (A trỏ B, B trỏ A), một dòng tự trỏ vào chính mã của nó, hoặc
    cây sâu hơn mức cho phép. Bước kiểm theo dòng không bắt được loại này, nên nó
    ném ở bước ghi — và ném chứ không bỏ qua, vì bỏ qua là mất dữ liệu im lặng.
    """

    error_code: ClassVar[str] = "import.parent_unresolvable"


class ConfigPackageNotFoundError(DomainError):
    """Không có gói cấu hình nào hiệu lực cho chế độ kế toán tại ngày này.

    Xảy ra khi ghi sổ vào một ngày mà chưa gói TT99/TT133 nào phủ
    (`effective_from`/`effective_to`). Cách sửa nằm ở màn hình gói cấu hình,
    không phải ở chứng từ — nên thông điệp phải chỉ về đó.
    """

    error_code: ClassVar[str] = "config.package_not_found"


class AccountNotFoundError(DomainError):
    """Số hiệu tài khoản không có trong gói cấu hình đang hiệu lực."""

    error_code: ClassVar[str] = "account.not_found"


class VoucherNotFoundError(DomainError):
    """Chứng từ không tồn tại trong dữ liệu kế toán đang mở."""

    error_code: ClassVar[str] = "voucher.not_found"
    http_status: ClassVar[int] = 404


class VoucherTransitionError(DomainError):
    """Thao tác không hợp lệ với trạng thái hiện tại của chứng từ.

    Mã lỗi mang cả trạng thái hiện tại lẫn thao tác bị từ chối: client dựng
    được câu "chứng từ đã ghi sổ nên không xóa được — bỏ ghi sổ trước", thay vì
    một câu chung chung bắt người dùng tự đoán bước còn thiếu.
    """

    error_code: ClassVar[str] = "voucher.invalid_transition"


class VoucherBranchImmutableError(DomainError):
    """Chi nhánh của chứng từ đã cất không đổi được.

    Số chứng từ cấp theo **dãy của chi nhánh** (`per_branch`, FR-NFR-006): mang
    số của chi nhánh A sang chi nhánh B thì hoặc đụng ràng buộc duy nhất, hoặc
    chiếm chỗ một số mà bộ đếm của B chưa cấp — và lần Cất kế tiếp của B đổ vĩnh
    viễn. Đổi chi nhánh = xóa chứng từ rồi lập lại ở chi nhánh đúng, mỗi bước có
    vết riêng.
    """

    error_code: ClassVar[str] = "voucher.branch_immutable"


class PostingViolation:
    """Một vi phạm mà validator ghi sổ tìm thấy — dạng dữ liệu, không phải ngoại lệ.

    Lớp thường chứ không dataclass Pydantic: kernel không phụ thuộc tầng API,
    và thứ duy nhất cần là chuyển được thành JSON cho `problem_extra`.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        ledger: int | None = None,
        line_no: int | None = None,
        **details: str | int | None,
    ) -> None:
        self.code = code
        self.message = message
        self.ledger = ledger
        self.line_no = line_no
        self.details = details

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.ledger is not None:
            body["ledger"] = self.ledger
        if self.line_no is not None:
            body["line_no"] = self.line_no
        if self.details:
            body["details"] = self.details
        return body


class PostingValidationError(DomainError):
    """Chứng từ không đủ điều kiện ghi sổ — mang **toàn bộ** vi phạm.

    Validator chạy hết rồi mới ném (phase-04 §Posting engine): người dùng sửa
    một lượt thay vì sửa–gửi–lại-lỗi từng vòng. Danh sách vi phạm đi trong
    `problem_extra` vì `details` chỉ nhận vô hướng.
    """

    error_code: ClassVar[str] = "posting.invalid"

    def __init__(self, message: str, *, violations: list[PostingViolation]) -> None:
        super().__init__(message, violation_count=len(violations))
        self.violations = violations

    def problem_extra(self) -> dict[str, Any]:
        return {"violations": [violation.as_json() for violation in self.violations]}


class DocumentTypeUnknownError(DomainError):
    """Loại chứng từ chưa được module nào đăng ký với posting engine."""

    error_code: ClassVar[str] = "voucher.document_type_unknown"


class OpeningPeriodMissingError(DomainError):
    """Năm tài chính chưa có kỳ kế toán nào — số dư ban đầu không có kỳ để đổ vào."""

    error_code: ClassVar[str] = "opening.period_missing"


class OpeningPeriodLockedError(DomainError):
    """Kỳ đầu năm đã khóa sổ — số dư ban đầu chỉ sửa được sau khi mở khóa (FR-OPB-011)."""

    error_code: ClassVar[str] = "opening.period_locked"


class OpeningFiscalYearClosedError(DomainError):
    """Năm tài chính đã quyết toán (`fiscal_years.is_closed`) — chặn cả kỳ 13."""

    error_code: ClassVar[str] = "opening.fiscal_year_closed"


class OpeningCarryForwardTargetMissingError(DomainError):
    """Chưa có năm tài chính liền sau để nhận số dư chuyển sang (FR-OPB-010).

    Cách sửa nằm ở màn hình năm tài chính (tạo năm mới), không ở chính lượt
    chuyển — thông điệp phải chỉ về đó.
    """

    error_code: ClassVar[str] = "opening.carry_forward_target_missing"


class OpeningCarryForwardExistsError(DomainError):
    """Năm nhận đã có số dư ban đầu — ghi đè phải là lựa chọn tường minh.

    Cùng triết lý `ImportMode.CREATE_ONLY` (H80): lượt chạy lại là chuyện bình
    thường (chốt lại số cuối năm sau kiểm toán), nhưng nó **thay trọn** số dư
    người dùng có thể đã sửa tay — nên client phải gửi `overwrite=true` sau khi
    người dùng đọc cảnh báo, không phải job tự quyết.
    """

    error_code: ClassVar[str] = "opening.carry_forward_exists"


class OpeningBranchRequiredError(DomainError):
    """Người dùng nhiều chi nhánh chưa chọn chi nhánh đang thao tác.

    Số dư ban đầu ghi theo chi nhánh (FR-OPB-009) và job chạy dưới phạm vi RLS
    của chi nhánh đang thao tác. Không chặn ở đây thì lượt xếp hàng nhận `202`
    rồi job chắc chắn fail — người dùng biết muộn hơn đúng một vòng đợi.
    """

    error_code: ClassVar[str] = "opening.branch_required"


# ---------------------------------------------------------------------------
# Gói cấu hình pháp lý — máy móc quanh TT99/TT133 (phase 5, lát 5A)
# ---------------------------------------------------------------------------


class ConfigPackageIdUnknownError(DomainError):
    """Không có gói cấu hình nào mang `id` này.

    Khác `ConfigPackageNotFoundError` (không gói nào hiệu lực **tại một ngày**
    cho một chế độ): lỗi này là gọi nhầm `id`, thường ở đường kích hoạt hoặc
    đường tra tài khoản của một gói cụ thể.
    """

    error_code: ClassVar[str] = "config.package_unknown"
    http_status: ClassVar[int] = 404


class ConfigPackageDataInvalidError(DomainError):
    """Bộ dữ liệu của một gói cấu hình (thư mục hoặc gói `.zip`) không hợp lệ.

    Ném ở **loader**, trước khi bất kỳ dòng nào được ghi (fail-closed, RT-07):
    mã tham chiếu sai, số hiệu trùng, `detail_tracking` chứa token lạ, tính
    chất dư ngoài khoảng — mọi sai sót của gói cấu hình đều đi qua đây, mang
    theo đủ ngữ cảnh (`file`, `code`, `reason`) để người biên soạn gói sửa
    đúng dòng, không phải dò cả tệp.
    """

    error_code: ClassVar[str] = "config.package_data_invalid"


class DefaultAccountNotConfiguredError(DomainError):
    """Gói cấu hình đang hiệu lực chưa khai tài khoản ngầm định cho mục đích này.

    Khác lỗi "gõ nhầm mã TK": đây là một **khoảng trống cấu hình** — gói thiếu
    dòng `default_accounts` cho `purpose` này (cả bản ghi riêng lẫn bản ghi
    dùng chung `'*'`). Cách sửa nằm ở màn hình gói cấu hình, không ở chứng từ.
    """

    error_code: ClassVar[str] = "config.default_account_not_configured"


class ConfigPackageSignatureInvalidError(DomainError):
    """Gói `.zip` không qua được lớp ký số (RT-07): sai chữ ký, sai khóa, hoặc lệch checksum.

    Toàn bộ gói bị từ chối — không có "nhập một phần": một tệp trong gói bị sửa
    sau khi ký thì không có cách nào biết những tệp còn lại có đáng tin không.
    """

    error_code: ClassVar[str] = "config.package_signature_invalid"


class ConfigPackageArchiveInvalidError(DomainError):
    """Gói `.zip` không đúng cấu trúc cho phép: tệp lạ, đường dẫn thoát thư mục,
    tệp bị thiếu, hoặc vượt trần dung lượng.

    Kiểm **trước** khi verify chữ ký nội dung từng tệp: một gói cố tình mang
    theo đường dẫn `../../etc/passwd` phải bị chặn ngay ở bước đọc danh sách
    tệp, không phải chờ tới lúc ghi xuống đĩa.
    """

    error_code: ClassVar[str] = "config.package_archive_invalid"


class StatementFormulaInvalidError(DomainError):
    """Công thức chỉ tiêu BCTC không hợp lệ (FR-GLE-043): sai grammar, tham
    chiếu chỉ tiêu không tồn tại, hoặc các chỉ tiêu tham chiếu vòng nhau.

    Ném ở parser/evaluator (`kernel/config/statements/formula/`); loader gói
    cấu hình bọc thêm ngữ cảnh (layout nào, chỉ tiêu nào) thành
    `ConfigPackageDataInvalidError` để người biên soạn gói sửa đúng dòng.
    """

    error_code: ClassVar[str] = "config.statement_formula_invalid"


class StatementLayoutNotFoundError(DomainError):
    """Gói cấu hình đang hiệu lực không có mẫu BCTC mang mã này.

    404 chứ không 422: mã layout nằm trên đường dẫn URL — sai mã là "tài nguyên
    không tồn tại", cùng khuôn `ConfigPackageIdUnknownError`.
    """

    error_code: ClassVar[str] = "config.statement_layout_unknown"
    http_status: ClassVar[int] = 404


class ReportSpecInvalidError(DomainError):
    """`spec` của layout/bộ tham số báo cáo sai hình dạng (FR-RPT-001).

    Ném ở `kernel/config/reports/spec.py` — ranh giới duy nhất đọc JSONB thô.
    Nổ lúc gieo/nhập (fail-closed) hoặc lúc render nếu dữ liệu bị sửa tay trong
    DB; không bao giờ thành `KeyError` giữa một lượt kết xuất.
    """

    error_code: ClassVar[str] = "report.spec_invalid"


class ReportDatasetInvalidError(DomainError):
    """`sql_text` của dataset báo cáo vi phạm hợp đồng: placeholder ngoài
    `allowed_params`, hoặc thiếu cột mà lớp bọc phạm vi cần (`branch_id`/`ledger`)."""

    error_code: ClassVar[str] = "report.dataset_invalid"


class ReportNotFoundError(DomainError):
    """Không có báo cáo mang mã này. 404 — mã nằm trên URL, cùng khuôn
    `StatementLayoutNotFoundError`."""

    error_code: ClassVar[str] = "report.not_found"
    http_status: ClassVar[int] = 404


class ReportParamsInvalidError(DomainError):
    """Tham số render không qua được bộ kiểm sinh từ `param_set.spec`
    (FR-RPT-002): thiếu tham số bắt buộc, sai kiểu, hoặc tham số lạ."""

    error_code: ClassVar[str] = "report.params_invalid"


class ReportRenderNotReadyError(DomainError):
    """Tác vụ kết xuất chưa có tệp để tải: đang chạy, đã hỏng, hoặc đã hủy.

    `409` chứ không `404`: job TỒN TẠI và người gọi thấy nó — thiếu là *trạng
    thái*, không phải *tài nguyên*. Client đọc `job_status` trong details để
    phân biệt "chờ thêm" với "đã hỏng, đừng chờ nữa".
    """

    error_code: ClassVar[str] = "report.render_not_ready"
    http_status: ClassVar[int] = 409


class ReportDatasetNotExecutableError(DomainError):
    """Dataset không-builtin chưa chạy được: role read-only RLS-bound cho SQL
    từ gói nhập ngoài (RT-07) đi cùng lát dựng ĐƯỜNG NHẬP dataset ngoài (quyết
    định 5D: chưa có đường nhập nào tạo được dataset không-builtin, nên một cơ
    chế quyền chưa có người dùng thật là rủi ro chứ không phải phòng thủ) — từ
    chối fail-closed thay vì chạy SQL không tin cậy bằng quyền runtime đầy đủ."""

    error_code: ClassVar[str] = "report.dataset_not_executable"
    http_status: ClassVar[int] = 409
    """409 chứ không 422: tham số người dùng không sai — trạng thái HỆ THỐNG
    (chưa có role read-only) chưa cho phép chạy dataset này (review 5C, L2)."""


class PrintTemplateNotFoundError(DomainError):
    """Không có mẫu in cho loại chứng từ này (FR-RPT-008): mã mẫu lạ, hoặc
    loại chứng từ chưa có mẫu mặc định."""

    error_code: ClassVar[str] = "print.template_not_found"
    http_status: ClassVar[int] = 404


class PrintNotAllowedError(DomainError):
    """Chứng từ ở trạng thái không in được (FR-RPT-011) — hiện chỉ một trường
    hợp: Đã hủy. Chứng từ chưa ghi sổ VẪN in được nhưng mang dấu BẢN NHÁP."""

    error_code: ClassVar[str] = "print.not_allowed"
    http_status: ClassVar[int] = 409


class CountSheetInvalidError(DomainError):
    """Biên bản kiểm kê quỹ không hợp lệ (FR-QUY-030): tổng theo mệnh giá lệch
    số đếm, TK không phải tiền mặt, hoặc chưa có năm tài chính phủ ngày kiểm."""

    error_code: ClassVar[str] = "count_sheet.invalid"


class CountSheetNotFoundError(DomainError):
    """Không tìm thấy biên bản kiểm kê quỹ."""

    error_code: ClassVar[str] = "count_sheet.not_found"
    http_status: ClassVar[int] = 404


class CountSheetAdjustmentError(DomainError):
    """Không sinh được phiếu xử lý chênh lệch (FR-QUY-031): không có chênh
    lệch, đã xử lý rồi, hoặc gói cấu hình thiếu nghiệp vụ/TK xử lý.

    409 chứ không 422: yêu cầu hợp lệ, trạng thái hiện tại (hoặc cấu hình gói)
    không cho phép — cùng lập luận `MasterDataInUseError`."""

    error_code: ClassVar[str] = "count_sheet.adjustment_conflict"
    http_status: ClassVar[int] = 409
