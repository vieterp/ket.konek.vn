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


class AuditContextMissingError(DomainError):
    """Có thay đổi trên bảng cần ghi nhật ký nhưng transaction không khai người thực hiện.

    Fail-closed có chủ đích (FR-NFR-012/013): thà chặn thao tác còn hơn ghi
    một thay đổi không có vết. Sửa bằng cách mở transaction qua
    `ket.kernel.persistence.unit_of_work.unit_of_work`, không phải bằng cách
    nới điều kiện ở listener.
    """

    error_code: ClassVar[str] = "audit.context_missing"


class SchemaVersionMismatchError(DomainError):
    """Schema DB lệch phiên bản migration mà mã nguồn đang mong đợi.

    App server **từ chối khởi động** trong trường hợp này (LD-05, FR-NFR-054):
    một binary cũ ghi sổ vào schema mới (hoặc ngược lại) là con đường ngắn nhất
    tới dữ liệu hỏng âm thầm.
    """

    error_code: ClassVar[str] = "system.schema_version_mismatch"
