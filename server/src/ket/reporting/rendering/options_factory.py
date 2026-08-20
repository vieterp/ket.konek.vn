"""Dựng `RenderOptions` từ settings của dữ liệu kế toán (FR-RPT-010/012).

Nằm ở `reporting` (chuyển từ `ket.api.render_options`, lát 5E) vì có HAI người
gọi mà chỉ một trong hai có `Settings`: request HTTP (tầng API) và thân job
render nền (`reporting/render_job.py`). Job chạy trong worker — nơi `kernel`
cấp `storage_root` qua `JobContext` chứ không cấp lớp cấu hình ứng dụng — nên
hàm này nhận thẳng `storage_root: Path | None`; tầng API bóc nó ra từ
`Settings.attachments_dir` trong lớp bọc mỏng của mình.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from sqlalchemy.orm import Session

from ket.kernel.attachments.storage import blob_path
from ket.kernel.config.catalog import (
    QUANTITY_DECIMALS_KEY,
    REPORT_FONT_SIZE_KEY,
    REPORT_LOGO_HASH_KEY,
    REPORT_LOGO_MEDIA_KEY,
)
from ket.kernel.config.settings_service import value_of
from ket.reporting.rendering.options import (
    DEFAULT_FONT_SIZE_PT,
    DEFAULT_QUANTITY_DECIMALS,
    LogoAsset,
    RenderOptions,
)

logger = structlog.get_logger(__name__)


def build_render_options(
    session: Session,
    *,
    storage_root: Path | None,
    dataset_schema: str,
    user_id: int,
    include_logo: bool = True,
) -> RenderOptions:
    """Tùy chọn trình bày đang hiệu lực cho một lượt render.

    Logo hỏng (hash sai, tệp mất, kho chưa cấu hình) làm bản in **thiếu logo**
    kèm một dòng log — không làm hỏng lượt render: người cần Sổ Cái để chốt
    thuế không nên bị chặn bởi một tệp trang trí.
    """
    quantity = value_of(session, key=QUANTITY_DECIMALS_KEY, user_id=user_id)
    font_size = value_of(session, key=REPORT_FONT_SIZE_KEY, user_id=user_id)
    return RenderOptions(
        quantity_decimals=(quantity if isinstance(quantity, int) else DEFAULT_QUANTITY_DECIMALS),
        font_size_pt=font_size if isinstance(font_size, int) else DEFAULT_FONT_SIZE_PT,
        # Preview lưới không nhúng logo — đọc blob mỗi lượt là I/O thừa (L4).
        logo=(
            _load_logo(
                session,
                storage_root=storage_root,
                dataset_schema=dataset_schema,
                user_id=user_id,
            )
            if include_logo
            else None
        ),
    )


def _load_logo(
    session: Session, *, storage_root: Path | None, dataset_schema: str, user_id: int
) -> LogoAsset | None:
    content_hash = value_of(session, key=REPORT_LOGO_HASH_KEY, user_id=user_id)
    if not isinstance(content_hash, str) or not content_hash:
        return None
    if storage_root is None:
        logger.warning("report_logo.storage_not_configured")
        return None
    media_type = value_of(session, key=REPORT_LOGO_MEDIA_KEY, user_id=user_id)
    try:
        path = blob_path(storage_root, dataset_schema, content_hash)
        content = path.read_bytes()
    except (ValueError, OSError):
        logger.warning("report_logo.unreadable", content_hash=content_hash)
        return None
    return LogoAsset(content=content, media_type=str(media_type))
