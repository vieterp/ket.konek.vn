"""BFF màn hình Thiết lập — phân nhóm theo **hệ quả** (nguyên tắc U14, FR-SYS-004).

Đây là một trong bốn BFF chính đáng của plan (§Định hướng UI/UX): màn hình Thiết
lập đọc **hai** nguồn khác nhau — bảng tùy chọn (`kernel/config`) và những quyết
định chốt theo năm tài chính (`kernel/periods`) — và người dùng không có lý do gì
để biết rằng hai thứ đó nằm ở hai chỗ. Ghép ở client nghĩa là hai lượt gọi và
một luật phân nhóm chép sang TypeScript, tức luật đó có hai bản.

Cái BFF này **không** làm: nó không ghi. Sửa một tùy chọn vẫn đi qua
`/api/v1/settings`, sửa năm tài chính đi qua đường của năm tài chính. BFF là
đọc-only, đúng quy ước RT-21.

**Giới hạn đã biết:** cột "sửa được hay không" của nhóm chốt-một-lần hiện chỉ
xét *năm đã quyết toán chưa*. Điều kiện thật của FR-SYS-004 còn có "năm đã phát
sinh chứng từ chưa", mà chứng từ đầu tiên ra đời ở phase 6 —
`PeriodService.ensure_scheme_changeable` đã nhận `has_documents` từ **ngoài** vì
kernel không được biết bảng chứng từ nào tồn tại (luật phụ thuộc #5). Khi phase 4
có `gl_postings`, chỗ nối là một tham số duy nhất truyền vào `_fiscal_year_items`.
Ở phase 3 chưa có chứng từ nào nên câu trả lời hiện tại **đúng với dữ liệu đang
có**, chỉ là nó sẽ không tự đúng thêm.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.setup_schemas import (
    ANYTIME_GROUP,
    DECIDED_ONCE_GROUP,
    SettingsGroupsResponse,
    SetupGroup,
    SetupItem,
)
from ket.kernel.config.catalog import CATALOG, SettingDefinition
from ket.kernel.config.settings_service import effective_settings
from ket.kernel.periods.models import FiscalYear
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import SYSTEM_MODULE, Action, permission_code

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

SETTINGS_VIEW: Final[str] = permission_code(SYSTEM_MODULE, "setting", Action.VIEW)

SettingsReader = Annotated[AuthorizedRequest, Depends(require_permission(SETTINGS_VIEW))]

FISCAL_YEAR_DECISIONS: Final[tuple[tuple[str, str], ...]] = (
    ("accounting_scheme", "Chế độ kế toán"),
    ("base_currency", "Đồng tiền hạch toán"),
    ("inventory_valuation_method", "Phương pháp tính giá xuất kho"),
    ("vat_method", "Phương pháp tính thuế GTGT"),
)
"""Bốn quyết định chốt một lần cho cả một niên độ (xem `periods/models.py`).

Danh sách khai tường minh chứ không "mọi cột của `fiscal_years`": `code`,
`start_date`, `is_closed` cũng là cột nhưng không phải quyết định thiết lập, và
một vòng lặp trên toàn bộ cột sẽ đẩy chúng lên màn hình Thiết lập mỗi lần bảng
thêm cột."""

CLOSED_YEAR_REASON: Final[str] = "Năm tài chính đã quyết toán"


@router.get("/settings-groups", response_model=SettingsGroupsResponse)
def settings_groups(authorized: SettingsReader, factory: SessionFactory) -> SettingsGroupsResponse:
    """Thiết lập chia hai nhóm: đổi bất kỳ lúc nào / chốt một lần (U14).

    Phân nhóm theo hệ quả là toàn bộ điểm của màn hình này. Một danh sách phẳng
    xếp "ngôn ngữ giao diện" cạnh "phương pháp tính giá xuất kho" nói rằng hai
    thứ đó cùng mức rủi ro — trong khi đổi cái thứ hai giữa năm làm giá vốn của
    mọi phiếu xuất còn lại tính theo một luật khác.
    """
    with unit_of_work(factory, authorized.scope) as session:
        settings_items = _setting_items(session, user_id=authorized.scope.user_id)
        fiscal_items = _fiscal_year_items(session)

    anytime = [item for item in settings_items if not _is_decided_once(item.key)]
    decided_once = [item for item in settings_items if _is_decided_once(item.key)] + fiscal_items

    return SettingsGroupsResponse(
        groups=[
            SetupGroup(
                key=ANYTIME_GROUP,
                title="Đổi được bất kỳ lúc nào",
                description=(
                    "Thay đổi ở đây chỉ ảnh hưởng cách hiển thị và thao tác, "
                    "không ảnh hưởng số liệu đã ghi sổ."
                ),
                items=anytime,
            ),
            SetupGroup(
                key=DECIDED_ONCE_GROUP,
                title="Chốt một lần",
                description=(
                    "Thay đổi ở đây làm số liệu ghi sau khác luật với số liệu đã ghi. "
                    "Hệ thống khóa lại khi năm tài chính đã quyết toán."
                ),
                items=decided_once,
            ),
        ]
    )


def _is_decided_once(key: str) -> bool:
    definition: SettingDefinition | None = CATALOG.get(key)
    return definition is not None and definition.decided_once


def _setting_items(session: Session, *, user_id: int) -> list[SetupItem]:
    """Tùy chọn trong catalog, kèm giá trị **đang có hiệu lực** cho người gọi."""
    return [
        SetupItem(
            key=setting.key,
            title=CATALOG[setting.key].description,
            value=setting.raw_value,
            source="setting",
            fiscal_year_code=None,
            # Tùy chọn không bị năm tài chính khóa: cấp hệ thống và cấp người
            # dùng đều sửa được qua `/api/v1/settings` bất kỳ lúc nào. Nhóm
            # "chốt một lần" ở đây là **lời cảnh báo về hệ quả**, không phải một
            # cái khóa — khóa thật thuộc về năm tài chính bên dưới.
            is_editable=True,
            locked_reason=None,
        )
        for setting in effective_settings(session, user_id=user_id)
    ]


def _fiscal_year_items(session: Session) -> list[SetupItem]:
    """Bốn quyết định của **từng** năm tài chính đang có.

    Từng năm chứ không "năm hiện hành": nhiều năm tồn tại song song
    (FR-NFR-033), và tháng 1 năm sau người ta vẫn đang chốt sổ năm trước. Màn
    hình phải nói được năm nào còn sửa được, năm nào đã khóa.
    """
    years = (
        session.execute(select(FiscalYear).order_by(FiscalYear.start_date.desc())).scalars().all()
    )
    items: list[SetupItem] = []
    for year in years:
        for field, title in FISCAL_YEAR_DECISIONS:
            items.append(
                SetupItem(
                    key=f"fiscal_years.{year.code}.{field}",
                    title=f"{title} ({year.code})",
                    value=str(getattr(year, field)),
                    source="fiscal_year",
                    fiscal_year_code=year.code,
                    is_editable=not year.is_closed,
                    locked_reason=None if not year.is_closed else CLOSED_YEAR_REASON,
                )
            )
    return items
