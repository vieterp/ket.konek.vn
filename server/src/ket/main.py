"""FastAPI app factory.

Đã dựng: nền dữ liệu và bảo mật (engine + pool, schema-per-dataset, vai trò DB
tách đôi, RLS, nhật ký bất biến, `Decimal`), **danh tính** (đăng nhập, phiên thu
hồi được, 2FA, hợp đồng lỗi RFC 7807) và **phân quyền** — định tuyến dataset
theo header mỗi request, RBAC tới cấp `{module}.{chứng từ}.{hành vi}`, phạm vi
chi nhánh cho RLS.

Lát 2B-2a thêm **hợp đồng ghi**: idempotency cùng transaction (FR-NFR-004),
khóa lạc quan bằng `row_version` (FR-NFR-005), tùy chọn hai cấp (FR-SYS-060) và
hạn mức request.

Lát 2B-2b thêm **hàng đợi tác vụ nền**: bảng `jobs` + tiến trình worker riêng
(`python -m ket.worker`) với lease/heartbeat/reaper (RT-13), và đường ống sinh
type TypeScript từ OpenAPI cho client.

Lát 2C-5 thêm **tệp đính kèm** (FR-NFR-053): nội dung nằm ngoài DB trong kho
định địa chỉ theo nội dung, bảng `attachments` giữ metadata và chịu cùng luật
RLS chi nhánh như mọi bảng nghiệp vụ.

Còn lại của phase 2: spike S1 (esign) và S4 (đóng gói) — cả hai chờ phần cứng.

Luồng nghiệp vụ đi qua REST + OpenAPI (LD-03). Client **không bao giờ** nối
thẳng PostgreSQL và **không** dùng API Tauri cho nghiệp vụ — giữ đường mở lên
chế độ trình duyệt trong LAN (topology, docs/system-architecture.md).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import Engine

from ket import __version__
from ket.api.dependencies import BRANCH_HEADER, DATASET_HEADER
from ket.api.idempotency import IDEMPOTENCY_HEADER
from ket.api.middleware.body_size_limit import (
    MULTIPART_OVERHEAD_BYTES,
    BodySizeLimitMiddleware,
)
from ket.api.middleware.problem_details import ProblemDetails, register_problem_handlers
from ket.api.middleware.rate_limit import RateLimitMiddleware
from ket.api.middleware.request_context import CORRELATION_HEADER, RequestContextMiddleware
from ket.api.middleware.schema_version_gate import (
    CLIENT_VERSION_HEADER,
    SchemaVersionGateMiddleware,
)
from ket.api.routers.accounts import router as accounts_router
from ket.api.routers.attachments import ATTACHMENTS_PREFIX
from ket.api.routers.attachments import router as attachments_router
from ket.api.routers.auth import router as auth_router
from ket.api.routers.auto_posting import router as auto_posting_router
from ket.api.routers.bank import router as bank_router
from ket.api.routers.bank_statements import router as bank_statements_router
from ket.api.routers.cash_book import router as cash_book_router
from ket.api.routers.cashflow import router as cashflow_router
from ket.api.routers.config_packages import CONFIG_PACKAGES_PREFIX
from ket.api.routers.config_packages import router as config_packages_router
from ket.api.routers.dimensions import router as dimensions_router
from ket.api.routers.exports import router as exports_router
from ket.api.routers.fiscal_years import router as fiscal_years_router
from ket.api.routers.gl_journal import router as gl_journal_router
from ket.api.routers.imports import router as imports_router
from ket.api.routers.items_prices import router as item_prices_router
from ket.api.routers.items_units import router as item_units_router
from ket.api.routers.items_variants import router as item_variants_router
from ket.api.routers.jobs import router as jobs_router
from ket.api.routers.ledger import router as ledger_router
from ket.api.routers.master_data import router as master_data_router
from ket.api.routers.opening_balances import router as opening_balances_router
from ket.api.routers.partners import router as partner_bank_accounts_router
from ket.api.routers.period_lock import router as period_lock_router
from ket.api.routers.price_list_lines import router as price_list_lines_router
from ket.api.routers.pricing import router as pricing_router
from ket.api.routers.printing import router as printing_router
from ket.api.routers.purchase import router as purchase_router
from ket.api.routers.reports import router as reports_router
from ket.api.routers.setup import router as setup_router
from ket.api.routers.statements import router as statements_router
from ket.api.routers.system import router as system_router
from ket.api.routers.system_settings import router as settings_router
from ket.api.routers.treasurer import router as treasurer_router
from ket.api.routers.updates import router as updates_router
from ket.api.routers.vouchers import router as vouchers_router
from ket.kernel.config.packages.importer import MAX_ARCHIVE_BYTES as IMPORTER_MAX_ARCHIVE_BYTES
from ket.kernel.datasets.bootstrap import verify_control_schema
from ket.kernel.datasets.provisioning import find_alembic_config, verify_dataset_schema_version
from ket.kernel.datasets.service import list_datasets
from ket.kernel.errors import UnsupportedPostgresVersionError
from ket.kernel.logging_setup import configure_logging, get_logger
from ket.kernel.persistence.engine import create_app_engine
from ket.kernel.persistence.session import create_session_factory
from ket.kernel.versions import parse_required_version
from ket.settings import Settings, get_settings

logger = get_logger(__name__)


class HealthResponse(BaseModel):
    """Trả lời của `/health`. Pydantic ở mọi ranh giới API (ADR-015)."""

    status: Literal["ok"]
    version: str
    deployment_mode: str


def verify_postgres_version(engine: Engine, settings: Settings) -> None:
    """Từ chối khởi động trên cụm PostgreSQL cũ hơn phiên bản đích (D4).

    Đọc `server_version_num` (số nguyên dạng `160004`) chứ không phân tích chuỗi
    `version()`: chuỗi đó khác nhau giữa bản Homebrew, Debian và container, và
    đã có tiền lệ dự án khác vấp vì so sánh chuỗi.
    """
    with engine.connect() as connection:
        raw = connection.exec_driver_sql("SHOW server_version_num").scalar_one()
    major = int(raw) // 10000
    if major < settings.minimum_postgres_version:
        raise UnsupportedPostgresVersionError(
            "Cụm PostgreSQL cũ hơn phiên bản đích của bản cài",
            expected=settings.minimum_postgres_version,
            found=major,
        )


def verify_schema_versions(engine: Engine, settings: Settings) -> None:
    """Từ chối khởi động nếu DB lệch phiên bản với mã nguồn (LD-05, FR-NFR-054).

    Kiểm **từng** dataset chứ không chỉ một: mỗi schema có lịch sử migration
    riêng, và một dataset bị bỏ quên khi nâng cấp (đang khôi phục, tạm vô hiệu
    hóa) sẽ là dataset mà binary mới ghi sổ vào cấu trúc cũ. Thà không khởi
    động còn hơn ghi hỏng một bộ sổ.
    """
    verify_control_schema(engine)
    config = find_alembic_config(settings.alembic_ini_path)
    for dataset in list_datasets(engine, include_inactive=True):
        verify_dataset_schema_version(engine, dataset.schema_name, config)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Vòng đời tiến trình: dựng pool, kiểm phiên bản schema, dọn khi tắt."""
    settings: Settings = app.state.settings
    engine = create_app_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    # Hai cổng dưới đây tắt được bằng cấu hình, và mỗi lần tắt đều phải để lại
    # dấu vết trong log. Một bản cài chạy tháng này qua tháng khác với cổng đã
    # tắt — vì ai đó gỡ rối rồi quên bật lại — là bản cài không còn ai canh việc
    # binary và schema có khớp nhau không, và triệu chứng đầu tiên sẽ là số liệu
    # sai chứ không phải một thông báo lỗi.
    if settings.verify_postgres_version_on_startup:
        verify_postgres_version(engine, settings)
        logger.info("postgres_version_verified")
    else:
        logger.warning(
            "postgres_version_gate_disabled",
            minimum_postgres_version=settings.minimum_postgres_version,
        )

    if settings.verify_schema_on_startup:
        verify_schema_versions(engine, settings)
        logger.info("schema_version_verified")
    else:
        logger.warning("schema_version_gate_disabled")

    try:
        yield
    finally:
        engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Dựng ứng dụng FastAPI.

    Truyền `settings` tường minh để test không phụ thuộc biến môi trường.
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(level=resolved.log_level, json_output=not resolved.debug)

    app = FastAPI(
        title="Konek Két — App Server",
        version=__version__,
        lifespan=lifespan,
        debug=resolved.debug,
        # Hợp đồng lỗi khai **một lần cho mọi endpoint**: RFC 7807 áp cho toàn
        # API (FR-NFR-050), nên khai theo từng route là chép cùng một dòng vào
        # hàng trăm chỗ và quên ở chỗ thứ một trăm lẻ một. Nhờ đó `ProblemDetails`
        # có mặt trong OpenAPI, và client bắt lỗi **có kiểu** thay vì `any`.
        responses={"default": {"model": ProblemDetails, "description": "Lỗi (RFC 7807)"}},
    )
    app.state.settings = resolved

    # Thứ tự **ngược** với thứ tự chạy: Starlette bọc theo chiều ngược lại, nên
    # middleware thêm **sau cùng** là lớp ngoài cùng. Ở đây điều đó có nghĩa là
    # mã tương quan được gắn trước, rồi mới tới hạn mức — để một phản hồi `429`
    # cũng mang `correlation_id` và tra được vào log như mọi lỗi khác.
    #
    # Cổng phiên bản nằm **trong cùng** trong ba lớp này: một client quá cũ gọi
    # dồn dập vẫn phải chạm hạn mức trước, nếu không thì `426` trở thành một
    # phản hồi rẻ mà kẻ gọi lặp vô hạn được.
    app.add_middleware(
        SchemaVersionGateMiddleware,
        minimum=parse_required_version(resolved.minimum_client_version),
    )
    # Trần thân request nằm **trong** hạn mức nhưng **ngoài** router: nó phải
    # chạy trước khi FastAPI đọc `multipart/form-data`, vì phép đọc đó ghi trọn
    # tệp xuống đĩa trước cả lớp xác thực (xem `body_size_limit`). Đặt sau
    # `RateLimitMiddleware` trong danh sách nghĩa là nó nằm **trong** hạn mức,
    # nên một kẻ gọi dồn dập vẫn chạm trần request/phút trước.
    app.add_middleware(
        BodySizeLimitMiddleware,
        overrides=(
            (
                ATTACHMENTS_PREFIX,
                resolved.attachment_max_bytes + MULTIPART_OVERHEAD_BYTES,
            ),
            # Gói cấu hình `.zip` (RT-07): trần riêng của `importer.py`
            # (`MAX_ARCHIVE_BYTES`) cộng phụ phí multipart, cùng khuôn với
            # tệp đính kèm ở trên — không dùng trần mặc định 1 MiB.
            (CONFIG_PACKAGES_PREFIX, IMPORTER_MAX_ARCHIVE_BYTES + MULTIPART_OVERHEAD_BYTES),
        ),
    )
    app.add_middleware(
        RateLimitMiddleware,
        default_per_minute=resolved.rate_limit_per_minute,
        auth_per_minute=resolved.rate_limit_auth_per_minute,
    )
    app.add_middleware(RequestContextMiddleware)
    # CORS thêm **sau cùng** nên nó là lớp NGOÀI CÙNG, và điều đó bắt buộc:
    # webview của Tauri không chạy ở origin của app server (`tauri://localhost`
    # trên macOS, `http(s)://tauri.localhost` trên Windows), nên mọi lời gọi từ
    # client desktop đều là xuyên origin. Nằm ngoài cùng thì preflight `OPTIONS`
    # được trả lời ngay, và **mọi** phản hồi — kể cả `429` của hạn mức hay `426`
    # của cổng phiên bản — đều mang header CORS. Nằm trong thì một phản hồi bị
    # chặn sẽ tới trình duyệt mà thiếu header, và người dùng thấy "lỗi mạng"
    # thay vì lý do thật.
    #
    # Không dùng `allow_credentials`: phiên đi bằng header `Authorization`, chứ
    # không bằng cookie — nên trình duyệt không cần gửi thông tin đăng nhập
    # xuyên site, và bật cờ đó chỉ mở rộng bề mặt tấn công CSRF.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_allowed_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            CLIENT_VERSION_HEADER,
            DATASET_HEADER,
            BRANCH_HEADER,
            IDEMPOTENCY_HEADER,
            CORRELATION_HEADER,
        ],
    )
    register_problem_handlers(app)
    app.include_router(auth_router)
    app.include_router(system_router)
    app.include_router(settings_router)
    app.include_router(jobs_router)
    app.include_router(attachments_router)
    app.include_router(updates_router)
    # **Trước** `master_data_router`, và thứ tự này là bắt buộc: hai bộ dùng
    # chung tiền tố `/api/v1/master`, FastAPI khớp route theo thứ tự đăng ký,
    # và `GET /{slug}/{record_id}` khớp cả `GET /{slug}/template`. Đăng ký sau
    # thì mọi lượt tải tệp mẫu nhận `422` ("template" không phải số nguyên)
    # thay vì nhận tệp. `tests/test_import_api.py` canh thứ tự này.
    app.include_router(imports_router)
    # Cùng cái bẫy thứ tự với `imports_router` ngay trên: `GET /{slug}/export`
    # bị `GET /{slug}/{record_id}` của `master_data_router` nuốt nếu đăng ký sau.
    app.include_router(exports_router)
    app.include_router(master_data_router)
    app.include_router(partner_bank_accounts_router)
    app.include_router(item_units_router)
    app.include_router(item_variants_router)
    app.include_router(item_prices_router)
    app.include_router(price_list_lines_router)
    app.include_router(pricing_router)
    app.include_router(setup_router)
    app.include_router(dimensions_router)
    # Phase 4 — posting engine: chứng từ nghiệp vụ khác + hành động chứng từ
    # dùng chung (ghi sổ / bỏ ghi sổ / xóa).
    app.include_router(accounts_router)
    app.include_router(auto_posting_router)
    app.include_router(fiscal_years_router)
    app.include_router(gl_journal_router)
    app.include_router(cash_book_router)
    app.include_router(bank_router)
    app.include_router(bank_statements_router)
    app.include_router(treasurer_router)
    # BFF chỉ-đọc gộp quỹ + ngân hàng cho màn hình "Tiền vào tiền ra" (lát
    # 6E-1). Sau hai router module vì nó là lớp ĐỌC dựng trên chúng, không
    # phải một phân hệ thứ ba.
    app.include_router(cashflow_router)
    app.include_router(vouchers_router)
    app.include_router(ledger_router)
    app.include_router(opening_balances_router)
    app.include_router(period_lock_router)
    # Lát 5A — máy móc quanh gói cấu hình pháp lý (TT99/TT133): liệt kê, xem
    # cây TK của một gói, kích hoạt, nhập gói `.zip` đã ký (RT-07).
    app.include_router(config_packages_router)
    # Lát 5B — BCTC từ layout công thức (FR-GLE-043): danh sách mẫu + xem trước.
    app.include_router(statements_router)
    # Lát 5C — report engine metadata-driven (FR-RPT-001): danh mục báo cáo,
    # spec tham số, kết xuất PDF/XLSX (FR-RPT-006).
    app.include_router(reports_router)
    # Lát 5D — in chứng từ theo mẫu + sổ theo dõi lần in (FR-RPT-008/011).
    app.include_router(printing_router)
    # Lát 7B — hóa đơn mua hàng + chi phí mua hàng (SRS 04 §3); ghi sổ qua
    # endpoint chứng từ dùng chung, sổ phụ công nợ qua `ArApSubledger`.
    app.include_router(purchase_router)

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        """Smoke check — không chạm cơ sở dữ liệu."""
        return HealthResponse(
            status="ok",
            version=__version__,
            deployment_mode=resolved.deployment_mode,
        )

    return app


app = create_app()
