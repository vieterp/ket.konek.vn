"""Lớp bọc mỏng của tầng API quanh `reporting.rendering.options_factory`.

Phần dựng `RenderOptions` chuyển về `reporting` ở lát 5E vì thân job render nền
cũng cần nó mà không có `Settings` (worker chỉ cấp `storage_root` qua
`JobContext`). Tầng API giữ lại đúng một việc: bóc `attachments_dir` ra khỏi
lớp cấu hình ứng dụng — thứ mà `reporting` không được import (C5, LD-13).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ket.reporting.rendering.options import RenderOptions
from ket.reporting.rendering.options_factory import (
    build_render_options as _build_from_storage_root,
)
from ket.settings import Settings


def build_render_options(
    session: Session,
    *,
    settings: Settings,
    dataset_schema: str,
    user_id: int,
    include_logo: bool = True,
) -> RenderOptions:
    """Tùy chọn trình bày đang hiệu lực cho một lượt render qua HTTP."""
    return _build_from_storage_root(
        session,
        storage_root=settings.attachments_dir,
        dataset_schema=dataset_schema,
        user_id=user_id,
        include_logo=include_logo,
    )
