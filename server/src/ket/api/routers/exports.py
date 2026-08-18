"""Xuất danh mục ra Excel — một endpoint cho mỗi danh mục, sinh từ registry.

| Đường dẫn | Việc |
| --- | --- |
| `GET /api/v1/master/{slug}/export` | Tải dữ liệu danh mục dưới dạng .xlsx (FR-SYS-014) |

Tệp riêng chứ không thêm vào `routers/imports.py`: hai bên chia nhau
`TemplateDescriptor` nhưng không chia dòng mã nào khác — nhập có kho tệp, hàng
đợi job, bảng đệm và hai lớp quyền; xuất là một câu `SELECT` và một phản hồi.
Gộp chúng thì tệp nhập liệu vốn đã dài nhận thêm một mối bận tâm không liên quan.

**Đồng bộ, không qua hàng đợi.** Nhập phải là job vì nó chạy hàng phút và **ghi**
(H78: cần thanh tiến độ, nút Hủy, lease/reaper). Xuất chỉ đọc, và trần của nó là
`exporter.MAX_EXPORT_ROWS` = 10.000 dòng — đo được **≈1,5 giây CPU** mỗi lượt ở
mức trần. Dựng một job cho việc đó thì phải dựng thêm cả đường tải artifact của
job, tức máy móc mới cho một đường chỉ đọc.

Con số ấy là **lời biện minh**, nên nó phải đúng: bản đầu chép lại "dưới một
giây" và "bằng trần của nhập" — cả hai đều sai sau khi trần xuất hạ xuống 10.000
(trần nhập là `reader.MAX_ROWS` = 100.000). Cổng đo nó nằm ở
`tests/test_export.py`.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Response

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.kernel.excel.descriptors import template_for
from ket.kernel.excel.exporter import build_export, export_file_name
from ket.kernel.excel.template import XLSX_MEDIA_TYPE
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action

router = APIRouter(prefix="/api/v1/master", tags=["master-data-export"])

PREFIX: Final[str] = "/api/v1/master"


def _mount(spec: CatalogSpec) -> None:
    """Gắn endpoint xuất của một danh mục."""
    descriptor = template_for(spec)

    @router.get(
        f"/{spec.slug}/export",
        summary=f"{spec.title} — xuất dữ liệu ra Excel",
        # `Response` chứ không `StreamingResponse`: thân hàm dựng trọn tệp trong
        # bộ nhớ rồi trả một lần. Khai `StreamingResponse` ở đây là nói sai với
        # đặc tả OpenAPI về một thứ client sinh type từ đó — và nó không làm phản
        # hồi trở nên streaming, chỉ làm tài liệu lệch với hành vi.
        response_class=Response,
        responses={
            200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Tệp .xlsx"},
            422: {"description": "Danh mục vượt trần số dòng của một tệp xuất"},
        },
    )
    def export_records(
        authorized: Annotated[
            AuthorizedRequest, Depends(require_permission(spec.permission_code(Action.VIEW)))
        ],
        factory: SessionFactory,
        include_inactive: Annotated[
            bool, Query(description="Kèm cả bản ghi đã ngừng theo dõi")
        ] = False,
    ) -> Response:
        """Dữ liệu danh mục theo đúng hình dạng tệp mẫu nhập liệu (FR-SYS-014).

        Quyền `view`, cùng mức với đường đọc danh sách: tệp này chứa đúng những
        dòng mà `GET /api/v1/master/{slug}` đã trả về cho chính người dùng ấy,
        chỉ khác định dạng.

        **Phạm vi chi nhánh không phải tham số của client** — cùng luật với
        `routers/master_data.py`: lọc theo `acting_branch_id` của phiên, vì bảng
        danh mục cố ý không bật RLS (H39) nên đây là lớp lọc duy nhất.
        """
        with unit_of_work(factory, authorized.scope) as session:
            content = build_export(
                session,
                spec,
                descriptor,
                branch_id=authorized.scope.acting_branch_id,
                include_inactive=include_inactive,
            )
        return Response(
            content=content,
            media_type=XLSX_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{export_file_name(spec)}"',
                "X-Content-Type-Options": "nosniff",
            },
        )


for _spec in CATALOG_REGISTRY.specs():
    _mount(_spec)
