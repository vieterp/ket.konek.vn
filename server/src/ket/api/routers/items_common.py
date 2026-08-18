"""Bối cảnh dùng chung cho hai bảng con của vật tư hàng hóa.

Hai tệp router (`items_units.py`, `items_variants.py`) hỏi cùng ba câu trước mọi
thao tác: mã hàng chủ có tồn tại không, có nhìn thấy được từ chi nhánh đang thao
tác không, và người gọi có mã quyền của danh mục vật tư không. Chép ba câu ấy sang
tệp thứ hai là cách hai bản sao lệch nhau — và bản lệch sẽ là bản không ai đọc
lại, đúng bài học `_visible_to` của lát 3A.

Quyền dùng lại **chính mã quyền của danh mục vật tư** (`master.items.*`), cùng lối
`routers/partners.py`: đơn vị quy đổi và mã quy cách là một phần của hồ sơ mã
hàng, và một mã quyền riêng sẽ tạo ra tình huống "sửa được mã hàng nhưng không sửa
được đơn vị quy đổi của nó" mà không màn hình nào diễn đạt được.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends
from sqlalchemy.orm import Session

from ket.api.dependencies import AuthorizedRequest, require_permission
from ket.api.routers.master_data_guards import ensure_visible
from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.service import MasterDataService
from ket.kernel.security.permissions import Action

ITEM_SLUG: Final[str] = "items"
UNIT_SLUG: Final[str] = "units_of_measure"


def _item_spec() -> CatalogSpec:
    """Mô tả danh mục vật tư, lấy từ registry.

    Đọc registry chứ không gõ lại mã quyền: nếu một ngày `slug` của vật tư đổi thì
    hai tệp router phải đỏ ngay lúc nạp, không phải trả `403` cho người dùng thật.
    """
    spec = CATALOG_REGISTRY.get(ITEM_SLUG)
    if spec is None:  # pragma: no cover - registry nạp lúc import, thiếu là lỗi khởi động
        raise RuntimeError(f"Danh mục {ITEM_SLUG!r} chưa được đăng ký")
    return spec


SPEC: Final[CatalogSpec] = _item_spec()

ItemReader = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.VIEW)))
]
ItemAuthor = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.CREATE)))
]
ItemEditor = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.EDIT)))
]
ItemRemover = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.DELETE)))
]


def load_item(session: Session, item_id: int, authorized: AuthorizedRequest) -> None:
    """Mã hàng chủ phải tồn tại, **nhìn thấy được**, và không phải nút nhóm.

    Nhóm bị loại vì nó không bao giờ lên chứng từ (xem `MasterDataRow.is_group`),
    nên đơn vị quy đổi hay quy cách của một nhóm là dữ liệu không đường nào đọc
    tới — cùng lập luận đã áp cho nhóm đối tác ở `routers/partners.py`.
    """
    service: MasterDataService[MasterDataRow] = MasterDataService(session, SPEC.model)
    item = service.get(item_id)
    ensure_visible(item, authorized.scope.acting_branch_id, SPEC)
    if item.is_group:
        raise MasterDataNotFoundError(
            "Nhóm vật tư không có dữ liệu chi tiết — hãy chọn một mã hàng cụ thể",
            entity_type=SPEC.entity_type,
            entity_id=item_id,
        )
