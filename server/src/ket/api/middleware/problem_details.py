"""Hợp đồng lỗi HTTP: RFC 7807 `application/problem+json` (FR-NFR-050, bước 12).

Server trả **mã lỗi**, client dựng câu hiển thị (plan.md §Quy ước REST API). Vì
vậy `error_code` là phần quan trọng nhất của thân phản hồi, còn `detail` là câu
tiếng Việt dành cho người vận hành và cho log — không phải chuỗi mà UI in ra.

Hai bất biến mà tệp này giữ:

1. **Không bao giờ lộ traceback hay thông điệp ngoại lệ thô.** Ngoại lệ không
   lường trước trả về đúng một mã tham chiếu (`correlation_id`); chi tiết nằm ở
   log phía server. Thông điệp thô của một lỗi DB tiết lộ tên bảng, tên cột và
   đôi khi cả dữ liệu.
2. **Mã HTTP do lớp lỗi khai** (`DomainError.http_status`), không do một bảng
   ánh xạ đặt ở đây — bảng ánh xạ là thứ người thêm lớp lỗi mới sẽ quên.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from ket.api.middleware.request_context import correlation_id_of
from ket.kernel.errors import DomainError

logger = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE: Final[str] = "application/problem+json"

ERROR_TYPE_PREFIX: Final[str] = "https://konek.vn/errors/"
"""`type` của RFC 7807 phải là URI định danh **loại** lỗi. Không cần phân giải
được qua mạng — bản cài chạy offline — nhưng phải ổn định, vì client và tài liệu
đối chiếu theo nó."""

VALIDATION_ERROR_CODE: Final[str] = "request.validation_failed"
INTERNAL_ERROR_CODE: Final[str] = "system.internal_error"


def _problem(
    *,
    status: int,
    error_code: str,
    detail: str,
    request: Request,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    correlation_id = correlation_id_of(request)
    body: dict[str, object] = {
        "type": f"{ERROR_TYPE_PREFIX}{error_code}",
        "title": error_code,
        "status": status,
        "detail": detail,
        "error_code": error_code,
        "correlation_id": str(correlation_id) if correlation_id is not None else None,
    }
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_CONTENT_TYPE)


async def handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """Lỗi nghiệp vụ: mã lỗi + tham số, mã HTTP do lớp lỗi khai."""
    if not isinstance(exc, DomainError):  # pragma: no cover - FastAPI chỉ gọi đúng loại
        raise exc
    logger.info(
        "domain_error",
        error_code=exc.error_code,
        status=exc.http_status,
        details=exc.details,
    )
    return _problem(
        status=exc.http_status,
        error_code=exc.error_code,
        detail=exc.message,
        request=request,
        extra={"details": exc.details},
    )


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Thân/tham số request sai kiểu — lỗi của client, không phải của nghiệp vụ.

    Giữ nguyên danh sách `errors()` của Pydantic: nó nêu đúng trường nào sai và
    sai thế nào, và đây là loại lỗi mà người đọc là **lập trình viên client**.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    return _problem(
        status=422,
        error_code=VALIDATION_ERROR_CODE,
        detail="Dữ liệu gửi lên không hợp lệ",
        request=request,
        extra={"errors": exc.errors()},
    )


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Ngoại lệ không lường trước: 500 + mã tham chiếu, **không** chi tiết.

    `exc_info=True` đẩy traceback đầy đủ vào log phía server. Người dùng chỉ nhận
    `correlation_id` — đủ để bộ phận hỗ trợ tìm đúng dòng log, không đủ để ai đó
    dò cấu trúc DB qua thông điệp lỗi.
    """
    logger.error("unhandled_exception", exc_info=exc)
    return _problem(
        status=500,
        error_code=INTERNAL_ERROR_CODE,
        detail=(
            "Đã xảy ra lỗi không mong muốn. Cung cấp mã tham chiếu bên dưới cho "
            "bộ phận hỗ trợ để tra nhật ký."
        ),
        request=request,
    )


def register_problem_handlers(app: FastAPI) -> None:
    """Gắn ba handler vào app. Gọi trong `create_app`."""
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
