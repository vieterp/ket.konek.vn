"""Sổ đăng ký danh mục — nguồn **duy nhất** cho route, quyền và (lát 3C) tệp mẫu Excel.

Tiêu chí thành công của phase 3 nói thẳng: *"thêm một loại danh mục mới chỉ cần
thêm model + descriptor, không thêm router"*. Điều đó chỉ đúng nếu có đúng một
chỗ trả lời câu hỏi "hệ thống có những danh mục nào" — và mọi thứ dẫn xuất từ
danh mục (đường dẫn HTTP, mã quyền, tệp mẫu nhập liệu) đọc từ chỗ đó thay vì
giữ bản sao riêng.

Ba bản sao mà nơi này thay thế, kèm cách chúng hỏng nếu để rời:

* **Router**: hai mươi bộ CRUD chép tay, và bộ thứ mười một quên lọc chi nhánh.
* **Quyền**: một danh sách mã cố định, và danh mục thêm ở phase 8 không có mã
  nào — tức là *ai cũng* sửa được nó, vì `access.require` không được gọi.
* **Tệp mẫu Excel** (3C): một bảng ánh xạ thứ ba, lệch với hai bảng trên.

Đăng ký một danh mục **cũng đăng ký luôn loại quyền của nó** vào
`kernel/security/permissions.py`. Đó là điểm mấu chốt: không có đường nào thêm
được một danh mục mà quên phần quyền, vì cả hai là một thao tác.

Vì sao `CatalogRegistry` nhận `PermissionRegistry` qua tham số thay vì luôn ghi
vào registry toàn cục: test dựng registry riêng để kiểm chính luật ở trên, và
`PermissionRegistry.register` ném khi trùng khóa — nên một registry test không
tách được sẽ hoặc nổ, hoặc làm bẩn registry thật cho các test chạy sau.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel

from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.models.asset_type import AssetType, AssetTypeFields
from ket.kernel.master_data.models.bank import Bank, BankFields
from ket.kernel.master_data.models.contract import Contract
from ket.kernel.master_data.models.cost_object import CostObject
from ket.kernel.master_data.models.document_type import DocumentTypeCatalog
from ket.kernel.master_data.models.excise_tax_table import ExciseTaxTable
from ket.kernel.master_data.models.expense_item import ExpenseItem
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.master_data.models.payment_term import PaymentTerm, PaymentTermFields
from ket.kernel.master_data.models.pit_table import PitTable
from ket.kernel.master_data.models.project import Project
from ket.kernel.master_data.models.project_type import ProjectType
from ket.kernel.master_data.models.resource_tax_table import ResourceTaxTable
from ket.kernel.master_data.models.timekeeping_symbol import TimekeepingSymbol
from ket.kernel.master_data.models.tool_type import ToolType, ToolTypeFields
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.models.warehouse import Warehouse
from ket.kernel.security.permissions import (
    CATALOG_ACTIONS,
    MASTER_MODULE,
    Action,
    DocumentType,
    PermissionRegistry,
    permission_code,
)
from ket.kernel.security.permissions import REGISTRY as PERMISSION_REGISTRY
from ket.kernel.security.rls import validate_identifier


@dataclass(frozen=True)
class CatalogSpec:
    """Một danh mục được đăng ký: đủ để dựng route, mã quyền và tệp mẫu."""

    slug: str
    """Định danh trên URL (`/api/v1/master/{slug}`) **và** trong mã quyền.

    Khai tường minh chứ không suy từ `model.__tablename__`: tên bảng là chi tiết
    cài đặt có thể đổi khi gộp/tách bảng, còn `slug` đã đi vào type TypeScript
    sinh ở máy khách và vào dữ liệu phân quyền của khách hàng. Hai thứ có vòng
    đời khác nhau thì không nên là một biến.

    Dạng `snake_case` ASCII, cùng luật với mọi identifier khác
    (`security/rls.validate_identifier`) — vì nó là **một đoạn của mã quyền**, và
    mã quyền tách bằng dấu chấm.
    """

    model: type[MasterDataRow]
    title: str
    """Nhãn tiếng Việt, dùng cho phần mô tả OpenAPI và tên sheet tệp mẫu (3C)."""

    extra_fields: type[BaseModel] | None = None
    """Cột riêng ngoài bộ chung của `MasterDataRow`, nếu danh mục có.

    `None` cho danh mục thuần cây — mười một trong mười bảy danh mục hiện tại.
    Khi có, model này phải khai **đúng** tập cột riêng của bảng: thừa một trường
    thì API nhận một giá trị không có chỗ lưu, thiếu một trường thì có cột không
    ai đặt được. `test_master_data_registry.py` canh cả hai chiều.
    """

    def __post_init__(self) -> None:
        validate_identifier(self.slug)

    @property
    def entity_type(self) -> str:
        """Tên bảng — khóa trong `master_data_usage` và `audit_log`."""
        return str(self.model.__tablename__)

    def document_type(self) -> DocumentType:
        """Mục đăng ký quyền tương ứng: bốn hành vi của một danh mục.

        `CATALOG_ACTIONS` chứ không toàn bộ `Action`: danh mục không ghi sổ và
        không in chứng từ, nên `post`/`unpost`/`print` ở đây chỉ làm loãng màn
        hình phân quyền bằng những ô không bao giờ có tác dụng.
        """
        return DocumentType(module=MASTER_MODULE, code=self.slug, actions=CATALOG_ACTIONS)

    def permission_code(self, action: Action) -> str:
        return permission_code(MASTER_MODULE, self.slug, action)


class CatalogRegistry:
    """Danh mục của một tiến trình, tra được theo `slug`."""

    def __init__(self, permissions: PermissionRegistry) -> None:
        self._specs: dict[str, CatalogSpec] = {}
        self._permissions = permissions

    def register(self, spec: CatalogSpec) -> None:
        """Đăng ký một danh mục **và** loại quyền của nó.

        Hai việc trong một lời gọi, có chủ đích: tách ra thì sẽ có một danh mục
        được đăng ký mà quên phần quyền, và hậu quả im lặng — endpoint chạy
        được, `access.require` gọi một mã không tồn tại trong bảng `permissions`,
        và tùy cách phân giải quyền mà nó thành "không ai vào được" hoặc tệ hơn.

        Trùng `slug` → ném ngay. Ghi đè im lặng sẽ khiến danh mục thua cuộc biến
        mất khỏi API tùy theo thứ tự import — lỗi không tái hiện được.
        """
        if spec.slug in self._specs:
            raise ValueError(f"Danh mục {spec.slug!r} đã được đăng ký")
        # Đăng ký quyền **trước**: nếu nó ném (trùng mã với một loại chứng từ
        # khác chẳng hạn) thì registry danh mục không được giữ lại một mục đã
        # nửa vời — trạng thái đó sẽ làm lần đăng ký lại sau đó báo "đã tồn tại".
        self._permissions.register(spec.document_type())
        self._specs[spec.slug] = spec

    def specs(self) -> tuple[CatalogSpec, ...]:
        """Mọi danh mục, sắp theo `slug` — thứ tự ổn định cho OpenAPI đã commit.

        Không sắp thì đặc tả sinh ra đổi thứ tự theo thứ tự import, và
        `test_openapi_contract.py` sẽ đỏ ngẫu nhiên.
        """
        return tuple(self._specs[slug] for slug in sorted(self._specs))

    def get(self, slug: str) -> CatalogSpec | None:
        return self._specs.get(slug)


REGISTRY: Final[CatalogRegistry] = CatalogRegistry(PERMISSION_REGISTRY)
"""Registry danh mục của tiến trình.

Nạp lúc import gói `ket.kernel.master_data` (xem `__init__.py`), cùng lối đã
dùng cho `kernel.jobs.builtin`. `ket.model_registry` import gói này, nên mọi
điểm vào đọc `PermissionRegistry.codes()` — trong đó có `role_service` lúc cấp
dữ liệu kế toán mới — đều thấy đủ mã quyền danh mục.
"""


def _register_all() -> None:
    """Mười bảy danh mục của lát 3A + 3B-1.

    Xếp theo nhóm nghiệp vụ chứ không theo bảng chữ cái: người đọc tệp này đang
    tìm "danh mục kho nằm ở đâu", không tìm chữ cái. Thứ tự **xuất ra** thì đã
    được `specs()` sắp lại nên nhóm ở đây không ảnh hưởng đặc tả OpenAPI.
    """
    # Sáu chiều phân tích lõi của LD-08. `branch_id` là chiều thứ sáu và nằm ở
    # `branches` — bảng đó không phải danh mục (H38) nên không có mặt ở đây.
    for spec in (
        CatalogSpec(slug="cost_objects", model=CostObject, title="Đối tượng tập hợp chi phí"),
        CatalogSpec(slug="expense_items", model=ExpenseItem, title="Khoản mục chi phí"),
        CatalogSpec(slug="projects", model=Project, title="Công trình"),
        CatalogSpec(slug="project_types", model=ProjectType, title="Loại công trình"),
        CatalogSpec(slug="contracts", model=Contract, title="Hợp đồng"),
        # Vật tư – kho
        CatalogSpec(slug="warehouses", model=Warehouse, title="Kho"),
        CatalogSpec(slug="units_of_measure", model=UnitOfMeasure, title="Đơn vị tính"),
        # Tài sản
        CatalogSpec(
            slug="asset_types",
            model=AssetType,
            title="Loại tài sản cố định",
            extra_fields=AssetTypeFields,
        ),
        CatalogSpec(
            slug="tool_types",
            model=ToolType,
            title="Loại công cụ dụng cụ",
            extra_fields=ToolTypeFields,
        ),
        # Thanh toán – ngân hàng
        CatalogSpec(
            slug="payment_terms",
            model=PaymentTerm,
            title="Điều khoản thanh toán",
            extra_fields=PaymentTermFields,
        ),
        CatalogSpec(slug="banks", model=Bank, title="Ngân hàng", extra_fields=BankFields),
        # Chứng từ – hóa đơn
        CatalogSpec(slug="document_types", model=DocumentTypeCatalog, title="Loại chứng từ"),
        CatalogSpec(slug="invoice_forms", model=InvoiceForm, title="Mẫu số hóa đơn"),
        # Lương – thuế (chỉ bảng đầu; bậc thuế thuộc phase 9 — H50)
        CatalogSpec(slug="timekeeping_symbols", model=TimekeepingSymbol, title="Ký hiệu chấm công"),
        CatalogSpec(slug="pit_tables", model=PitTable, title="Biểu tính thuế thu nhập cá nhân"),
        CatalogSpec(
            slug="excise_tax_tables", model=ExciseTaxTable, title="Biểu thuế tiêu thụ đặc biệt"
        ),
        CatalogSpec(
            slug="resource_tax_tables", model=ResourceTaxTable, title="Biểu thuế tài nguyên"
        ),
    ):
        REGISTRY.register(spec)


_register_all()
