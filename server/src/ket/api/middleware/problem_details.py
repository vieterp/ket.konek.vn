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

import json
from collections.abc import Sequence
from typing import Any, Final

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.requests import Request
from starlette.responses import JSONResponse

from ket.api.middleware.request_context import correlation_id_of
from ket.kernel.errors import (
    DomainError,
    DuplicateValueError,
    ReferenceNotFoundError,
    RowVersionConflictError,
)

logger = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE: Final[str] = "application/problem+json"

ERROR_TYPE_PREFIX: Final[str] = "https://konek.vn/errors/"
"""`type` của RFC 7807 phải là URI định danh **loại** lỗi. Không cần phân giải
được qua mạng — bản cài chạy offline — nhưng phải ổn định, vì client và tài liệu
đối chiếu theo nó."""

VALIDATION_ERROR_CODE: Final[str] = "request.validation_failed"
INTERNAL_ERROR_CODE: Final[str] = "system.internal_error"


class ProblemDetails(BaseModel):
    """Thân lỗi RFC 7807 — **hợp đồng công khai** với client.

    Khai thành model (dù handler vẫn dựng `dict` để mang thêm trường tùy lớp
    lỗi) vì đúng một lý do: nó phải có mặt trong đặc tả OpenAPI, và từ đó có mặt
    trong type sinh cho client (bước 14). Không khai thì client bắt lỗi bằng
    `any` — tức là phần *hay xảy ra nhất* của một API lại là phần duy nhất không
    có kiểu.

    `model_config` cho phép trường thừa: mỗi lớp lỗi được khai thêm trường riêng
    qua `problem_extra()` (`latest` của xung đột phiên bản, `errors` của lỗi
    kiểm dữ liệu). Chúng là phần **mở rộng** của hợp đồng, không phải phần vi
    phạm nó.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    """URI định danh loại lỗi, tiền tố `https://konek.vn/errors/`."""

    title: str
    status: int
    detail: str
    """Câu tiếng Việt cho người vận hành và cho log — **không** phải chuỗi UI in
    ra. Client dựng thông điệp hiển thị từ `error_code`."""

    error_code: str
    """Mã ổn định để client và tài liệu đối chiếu. Đổi mã = breaking change."""

    correlation_id: str | None = None
    details: dict[str, str | int | None] | None = None

    errors: list[dict[str, object]] | None = None
    """Danh sách lỗi kiểm dữ liệu của Pydantic (`request.validation_failed`).

    Khai tường minh dù `extra="allow"` đã cho nó đi qua: không khai thì type
    sinh cho client chỉ có `[key: string]: unknown`, tức là **hai thân lỗi hay
    gặp nhất** của API lại là hai thân duy nhất không có kiểu."""

    latest: dict[str, object] | None = None
    """Bản ghi mới nhất kèm theo xung đột phiên bản (FR-NFR-005), để màn hình
    hiện được "người kia vừa đổi gì" thay vì chỉ báo lỗi."""


def problem_response(
    *,
    status: int,
    error_code: str,
    detail: str,
    request: Request,
    extra: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Dựng một phản hồi RFC 7807.

    Lộ ra (không còn `_problem` riêng tư) vì **middleware** cũng phải trả lỗi:
    middleware chạy ngoài phạm vi của `add_exception_handler`, nên nó không thể
    ném `DomainError` và trông chờ handler bắt. Dùng chung hàm này là cách giữ
    cho API chỉ có **một** định dạng lỗi duy nhất.
    """
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
    return JSONResponse(
        status_code=status, content=body, media_type=PROBLEM_CONTENT_TYPE, headers=headers
    )


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
    return problem_response(
        status=exc.http_status,
        error_code=exc.error_code,
        detail=exc.message,
        request=request,
        # `problem_extra()` do chính lớp lỗi khai (mặc định rỗng) — xem
        # `DomainError.problem_extra`. Đặt sau `details` để không lớp con nào
        # vô tình ghi đè tham số thông điệp.
        extra={"details": exc.details, **exc.problem_extra()},
    )


def _serializable_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Danh sách lỗi của Pydantic, đã bỏ những giá trị không tuần tự hóa được.

    Pydantic v2 gắn **chính đối tượng ngoại lệ** vào `ctx["error"]` cho lỗi loại
    `value_error` — tức là mọi `@field_validator` / `@model_validator` ném
    `ValueError`. `json.dumps` không mã hóa được nó, nên phản hồi `422` đổ ngay
    trong handler và người dùng nhận `500 lỗi không mong muốn` cho một lỗi nhập
    liệu bình thường.

    Đây không phải trường hợp lý thuyết: nó lộ ra ở luật liên-trường đầu tiên
    của repo (`PaymentTermFields`, lát 3B-1), và từ phase 6 mỗi chứng từ có vài
    luật như vậy. `ctx` được **giữ lại** dưới dạng chuỗi thay vì bỏ đi, vì nó
    chứa tham số của thông điệp (`{'le': 100}`, `{'max_length': 50}`) mà client
    dựng câu tiếng Việt từ đó.
    """
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        entry = {key: _json_safe(value) for key, value in dict(error).items()}
        # `url` là đường dẫn tài liệu của Pydantic, không phải thông tin về lỗi.
        entry.pop("url", None)
        cleaned.append(entry)
    return cleaned


def _json_safe(value: object) -> object:
    """Giá trị nếu `json.dumps` mã hóa được nó, ngược lại là bản đã làm sạch.

    Thử mã hóa chứ không liệt kê kiểu được phép: danh sách kiểu là thứ sẽ thiếu
    một dòng khi Pydantic thêm một loại `ctx` mới, và thiếu ở đây nghĩa là `500`.
    Phép thử thì đúng theo định nghĩa — câu hỏi cần trả lời **chính là** "cái
    này tuần tự hóa được không".

    Đi **đệ quy** vào `dict`/`list` thay vì `str()` cả cụm (sửa sau review M-1):
    `ctx` là hợp đồng công khai (đã sinh type TypeScript) mà client đọc để dựng
    câu tiếng Việt — `{"le": 100}` phải ra một object có trường `le`, không phải
    chuỗi `"{'le': Decimal('100')}"`. Chỉ `str()` cả cụm thì hình dạng của `ctx`
    đổi tùy theo bên trong nó có gì, tức là kiểu **không ổn định** ở đúng chỗ
    client phải phân giải.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        pass
    else:
        return value

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return str(value)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Thân/tham số request sai kiểu — lỗi của client, không phải của nghiệp vụ.

    Giữ danh sách `errors()` của Pydantic: nó nêu đúng trường nào sai và sai thế
    nào, và người đọc loại lỗi này là **lập trình viên client**. Chỉ lọc phần
    không tuần tự hóa được — xem `_serializable_errors`.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    return problem_response(
        status=422,
        error_code=VALIDATION_ERROR_CODE,
        detail="Dữ liệu gửi lên không hợp lệ",
        request=request,
        extra={"errors": _serializable_errors(exc.errors())},
    )


async def handle_stale_data_error(request: Request, exc: Exception) -> JSONResponse:
    """`StaleDataError` của SQLAlchemy → cùng `409` với xung đột phiên bản.

    Đây là nhánh **hai request cùng đọc một phiên bản rồi cùng ghi**: lớp kiểm
    sớm (`require_row_version`) để lọt vì cả hai đều đọc đúng phiên bản mới
    nhất, và chỉ mệnh đề `WHERE row_version = ?` ở PostgreSQL phân xử được.

    Không kèm `latest` như nhánh kia: transaction vừa đổ, và mở thêm một
    transaction ở tầng handler chỉ để đọc lại bản ghi sẽ đặt một lượt truy vấn
    DB vào đúng chỗ không có ngữ cảnh nào để biết nên đọc bảng nào. Client gọi
    lại `GET` — nó vốn phải làm thế để dựng lại form.

    Bắt ở đây chứ không ở từng endpoint vì mọi bảng `RowVersioned` của mọi phase
    sau đều sinh ra lỗi này, và endpoint quên bắt sẽ trả `500` cho một tình
    huống hoàn toàn bình thường.
    """
    if not isinstance(exc, StaleDataError):  # pragma: no cover - FastAPI chỉ gọi đúng loại
        raise exc
    logger.info("row_version_conflict", detail=str(exc))
    return problem_response(
        status=RowVersionConflictError.http_status,
        error_code=RowVersionConflictError.error_code,
        detail="Bản ghi đã được người khác sửa — hãy tải lại rồi lưu lại",
        request=request,
        extra={"details": {}, "latest": None},
    )


async def handle_integrity_error(request: Request, exc: Exception) -> JSONResponse:
    """Ràng buộc DB bị vi phạm → lỗi **của người dùng**, không phải lỗi hệ thống.

    Không có handler này, mã chi nhánh trùng — lỗi gõ tay thường gặp nhất — trả
    về `500 "Đã xảy ra lỗi không mong muốn, cung cấp mã tham chiếu cho bộ phận
    hỗ trợ"`. Người nhập liệu không có gì để làm với câu đó, còn thứ họ cần chỉ
    là "mã này đã có".

    Bắt ở tầng chung chứ không ở từng endpoint: từ phase 6, mỗi phân hệ có hàng
    chục ràng buộc duy nhất (số chứng từ, mã danh mục), và endpoint quên bắt sẽ
    lặp lại đúng cái `500` này.

    **Chỉ** lộ tên ràng buộc, không bao giờ lộ câu lệnh hay thông điệp thô của
    PostgreSQL — chúng chứa tên bảng, tên cột và cả giá trị dữ liệu.
    """
    if not isinstance(exc, IntegrityError):  # pragma: no cover - FastAPI chỉ gọi đúng loại
        raise exc

    original = exc.orig
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    logger.info("integrity_error", constraint=constraint, kind=type(original).__name__)

    if isinstance(original, UniqueViolation):
        error: DomainError = DuplicateValueError(
            "Giá trị này đã có bản ghi khác sử dụng", constraint=constraint
        )
    elif isinstance(original, ForeignKeyViolation):
        error = ReferenceNotFoundError(
            "Bản ghi được tham chiếu không tồn tại", constraint=constraint
        )
    else:
        # Ràng buộc khác (CHECK, NOT NULL): vẫn là dữ liệu không hợp lệ, nhưng
        # không có thông điệp riêng nào đúng cho mọi trường hợp — trả `422` với
        # tên ràng buộc để người vận hành tra được.
        error = DomainError("Dữ liệu vi phạm ràng buộc của cơ sở dữ liệu", constraint=constraint)

    return await handle_domain_error(request, error)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Ngoại lệ không lường trước: 500 + mã tham chiếu, **không** chi tiết.

    `exc_info=True` đẩy traceback đầy đủ vào log phía server. Người dùng chỉ nhận
    `correlation_id` — đủ để bộ phận hỗ trợ tìm đúng dòng log, không đủ để ai đó
    dò cấu trúc DB qua thông điệp lỗi.
    """
    logger.error("unhandled_exception", exc_info=exc)
    return problem_response(
        status=500,
        error_code=INTERNAL_ERROR_CODE,
        detail=(
            "Đã xảy ra lỗi không mong muốn. Cung cấp mã tham chiếu bên dưới cho "
            "bộ phận hỗ trợ để tra nhật ký."
        ),
        request=request,
    )


def register_problem_handlers(app: FastAPI) -> None:
    """Gắn năm handler vào app. Gọi trong `create_app`."""
    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StaleDataError, handle_stale_data_error)
    app.add_exception_handler(IntegrityError, handle_integrity_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
