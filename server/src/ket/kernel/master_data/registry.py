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

from dataclasses import dataclass, replace
from typing import Final, Protocol

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ket.kernel.bank_import.profile_merge import BankStatementProfileMergeHook
from ket.kernel.master_data.bank_account_service import PartnerBankAccountMergeHook
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.item_discount_tier_service import ItemDiscountTierMergeHook
from ket.kernel.master_data.item_price_level_service import (
    ItemPriceLevelOfItemMergeHook,
    UnitOfMeasurePriceMergeHook,
)
from ket.kernel.master_data.item_unit_service import (
    ItemUnitOfItemMergeHook,
    UnitOfMeasureMergeHook,
)
from ket.kernel.master_data.item_variant_service import ItemVariantMergeHook
from ket.kernel.master_data.models.asset_type import (
    AssetType,
    AssetTypeFields,
    asset_type_row_rules,
)
from ket.kernel.master_data.models.bank import Bank, BankFields, bank_row_rules
from ket.kernel.master_data.models.company_bank_account import (
    CompanyBankAccount,
    CompanyBankAccountFields,
)
from ket.kernel.master_data.models.contract import Contract
from ket.kernel.master_data.models.cost_object import CostObject
from ket.kernel.master_data.models.document_type import DocumentTypeCatalog
from ket.kernel.master_data.models.employee import (
    Employee,
    EmployeeFields,
    employee_row_rules,
)
from ket.kernel.master_data.models.excise_tax_table import ExciseTaxTable
from ket.kernel.master_data.models.expense_item import ExpenseItem
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.master_data.models.item import (
    Item,
    ItemEditableFields,
    ItemFields,
    ItemUpdateGuard,
    item_row_rules,
)
from ket.kernel.master_data.models.partner import (
    Partner,
    PartnerFields,
    partner_row_rules,
)
from ket.kernel.master_data.models.payment_term import (
    PaymentTerm,
    PaymentTermFields,
    payment_term_row_rules,
)
from ket.kernel.master_data.models.pit_table import PitTable
from ket.kernel.master_data.models.price_list import (
    PriceList,
    PriceListFields,
    price_list_row_rules,
)
from ket.kernel.master_data.models.project import Project
from ket.kernel.master_data.models.project_type import ProjectType
from ket.kernel.master_data.models.resource_tax_table import ResourceTaxTable
from ket.kernel.master_data.models.timekeeping_symbol import TimekeepingSymbol
from ket.kernel.master_data.models.tool_type import (
    ToolType,
    ToolTypeFields,
    tool_type_row_rules,
)
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.models.warehouse import Warehouse
from ket.kernel.master_data.price_list_line_service import (
    PriceListLineOfItemMergeHook,
    PriceListLineOfPriceListMergeHook,
    PriceListLineOfUnitMergeHook,
)
from ket.kernel.master_data.row_rules import RowRule
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


class MergeHook(Protocol):
    """Luật hợp nhất riêng của một danh mục khi gộp hai bản ghi (FR-SYS-016).

    `merge_service` chuyển khóa ngoại bằng một câu `UPDATE` vô danh — nó không
    biết bảng con nào mang ràng buộc duy nhất theo cột danh mục, và cũng không
    nên biết. Danh mục nào có bảng con như vậy thì **tự khai** cách chuẩn bị,
    ngay cạnh dịch vụ quản lý bảng con đó.

    Cổng `test_master_data_merge.py` canh chiều ngược lại: bảng có khóa ngoại tới
    một danh mục **và** một ràng buộc duy nhất chứa cột đó thì danh mục ấy phải
    khai hook. Bảng con thứ hai ra đời ở phase sau sẽ làm nó đỏ.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        """Chạy trước khi chuyển khóa ngoại, trong cùng transaction."""
        ...

    def after_move(self, session: Session, *, target_id: int) -> None:
        """Chạy sau khi chuyển xong, trước khi xóa bản ghi nguồn."""
        ...


class UpdateGuard(Protocol):
    """Luật liên-trường của một danh mục ở đường **sửa** (review H-4).

    Vì sao không phải một validator Pydantic như luật của đường tạo: những luật
    này cần biết giá trị **đang có** của một cột mà thân request sửa cố ý không
    mang theo (trường chốt một lần, `extra_update_fields`). Vật tư hàng hóa là ca
    đầu tiên: "kho ngầm định chỉ cho thứ đi qua kho" cần `nature`, mà `nature`
    khai một lần lúc tạo nên nó không có trong thân request sửa.

    Không thay `CHECK` phía DB — chỗ bảo đảm vẫn là DB, vì mọi đường ghi đi qua
    nó kể cả nhập liệu và SQL của gói cấu hình phase 5. Thứ nó thêm được là **câu
    tiếng Việt nêu ô phải sửa**: không có nó, đường sửa trả `422` kèm tên một
    ràng buộc nội bộ trong khi đường tạo cùng dữ liệu trả một câu đọc được — đúng
    kiểu lệch mà review L3 của lát 3B-2 đã bắt ở nhân viên.
    """

    def check(self, record: MasterDataRow, payload: BaseModel) -> None:
        """Ném `DomainError` khi thân request mâu thuẫn với bản ghi đang có."""
        ...


@dataclass(frozen=True)
class CatalogFlag:
    """Một bộ lọc `?flag=` trên đường đọc danh sách, dựa vào một cột boolean.

    Sinh ra vì đối tác (FR-SYS-031): một bản ghi mang cả `is_customer` lẫn
    `is_vendor`, nhưng giao diện có **hai** danh sách. Lọc ở máy khách không thay
    thế được — danh sách có phân trang (H52), nên "trang 1 của khách hàng" phải
    là câu hỏi của máy chủ.

    Khai ở registry thay vì viết một route riêng cho đối tác: route riêng là
    đường đọc thứ hai trên cùng bảng, và hai đường đọc là hai chỗ để phép lọc
    chi nhánh trôi lệch nhau — đúng bài học `_visible_to` của lát 3A.
    """

    value: str
    """Giá trị trên URL: `?flag=customer`."""

    column: str
    """Cột boolean phải `TRUE`. Đối chiếu với cột đã ánh xạ trong test registry."""

    title: str
    """Nhãn tiếng Việt cho mô tả OpenAPI."""


@dataclass(frozen=True)
class CatalogReference:
    """Một cột của danh mục trỏ sang **danh mục khác** bằng khóa ngoại.

    Router dùng nó để kiểm bản ghi được trỏ tới có **nhìn thấy được** từ chi
    nhánh đang thao tác hay không. Không có bước đó thì khóa ngoại chỉ trả lời
    "id có tồn tại", và một đối tác của chi nhánh A gắn được điều khoản thanh
    toán riêng của chi nhánh B: giá trị hiện ra rỗng trên màn hình của A (đường
    đọc lọc chi nhánh) trong khi hạn nợ vẫn tính theo nó.
    """

    field: str
    """Tên cột trên chính danh mục này (`payment_term_id`)."""

    slug: str
    """`slug` của danh mục đích trong registry (`payment_terms`)."""


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

    `None` cho danh mục thuần cây — mười một trong mười tám danh mục hiện tại.
    Khi có, model này phải khai **đúng** tập cột riêng của bảng: thừa một trường
    thì API nhận một giá trị không có chỗ lưu, thiếu một trường thì có cột không
    ai đặt được. `test_master_data_registry.py` canh cả hai chiều.

    Đây là tập cột riêng của đường **tạo mới**; đường sửa dùng
    `extra_update_fields` khi danh mục có trường chốt một lần.
    """

    extra_update_fields: type[BaseModel] | None = None
    """Tập cột riêng **sửa được**, khi nó hẹp hơn `extra_fields` (H69).

    `None` = sửa được hết, đúng bằng `extra_fields`. Có giá trị nghĩa là danh mục
    có trường **chỉ khai lúc tạo**: vật tư hàng hóa chốt `nature` và
    `base_unit_id` một lần, vì đổi chúng làm số tồn và mọi tỷ lệ quy đổi đã khai
    sai lặng lẽ — con số giữ nguyên, nghĩa của nó đổi.

    Diễn đạt bằng **quan hệ kế thừa** (`extra_fields` là lớp con của
    `extra_update_fields`) chứ không bằng một danh sách tên trường: nhờ vậy thân
    request tạo mới thừa hưởng trọn validator của thân request sửa, nên không có
    luật nào chỉ áp cho một trong hai đường. Một danh sách tên trường thì lại là
    một chỗ nữa gõ tên cột bằng chuỗi. `test_master_data_registry.py` khẳng định
    đúng quan hệ ấy.
    """

    flags: tuple[CatalogFlag, ...] = ()
    """Bộ lọc boolean cho đường đọc danh sách — xem `CatalogFlag`."""

    references: tuple[CatalogReference, ...] = ()
    """Cột khóa ngoại trỏ sang danh mục khác — xem `CatalogReference`."""

    update_guard: UpdateGuard | None = None
    """Luật liên-trường ở đường sửa, khi nó cần giá trị **đang có** — xem `UpdateGuard`.

    Một, không phải tuple như `merge_hooks`: hook gộp gắn với **từng bảng con**
    nên số lượng đi theo số bảng con, còn luật liên-trường của một danh mục thì
    thuộc về chính danh mục ấy và viết gọn trong một chỗ.
    """

    row_rules: tuple[RowRule, ...] = ()
    """Luật liên-trường mà **bước nhập liệu** phải kiểm được (H3) — xem `row_rules.py`.

    Phản chiếu các ràng buộc `CHECK` liên-trường của bảng. Không thay chúng: DB
    vẫn là chỗ bảo đảm, còn đây là chỗ nói ra bằng tiếng Việt kèm đúng số dòng và
    đúng tên ô, thay vì để bước ghi đổ với một `IntegrityError` thô sau khi bước
    kiểm đã báo "hợp lệ".

    `tests/test_import_row_rules.py` canh hai chiều: mọi `CHECK` liên-trường của
    một bảng danh mục phải có luật ở đây, và mọi luật phải trỏ tới một `CHECK`
    có thật. Một ràng buộc thêm ở phase 5 vì thế làm bộ test đỏ.
    """

    merge_hooks: tuple[MergeHook, ...] = ()
    """Luật hợp nhất bảng con khi gộp hai bản ghi — xem `MergeHook`.

    Nhiều hook chứ không một (H70): vật tư hàng hóa có **hai** bảng con mang ràng
    buộc duy nhất (đơn vị quy đổi và mã quy cách), và `item_units` mang ràng buộc
    duy nhất theo **cả hai** cột danh mục của nó nên đơn vị tính cũng cần một hook.
    Một trường đơn sẽ buộc gộp chúng vào một hook tổng hợp biết tên mọi bảng con —
    trái đúng lý do H64 đặt luật hợp nhất cạnh dịch vụ của chính bảng con.

    Chạy theo thứ tự khai. Hook nào từ chối cả lần gộp (`base_unit_differs`) nên
    đứng trước hook chỉ dọn dữ liệu, để lần gộp không hợp lệ dừng trước khi có gì
    bị xóa — dù transaction quay lui thì nhật ký cũng không phải kể một chuyện
    không xảy ra.
    """

    def __post_init__(self) -> None:
        validate_identifier(self.slug)

    def flag(self, value: str) -> CatalogFlag | None:
        return next((item for item in self.flags if item.value == value), None)

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

    @property
    def auto_creatable(self) -> bool:
        """Danh mục này có tạo được một bản ghi hợp lệ từ **chỉ một cái mã** không?

        Câu trả lời cho FR-NFR-062 ("tùy chọn tự tạo danh mục còn thiếu"). Lát
        3C-1 đã từ chối giao tính năng ấy với đúng lý do này: một lượt nhập vật tư
        tự tạo ra đơn vị tính và kho là **ghi vào danh mục khác** với dữ liệu duy
        nhất là một cái mã — và với vật tư hàng hóa thì bản ghi sinh ra như vậy
        còn không hợp lệ (H76: hàng hóa bắt buộc có đơn vị chính).

        **Suy ra, không khai tay.** Hai điều kiện, cả hai đều chỉ có thể siết chặt
        thêm theo thời gian:

        * không cột riêng nào **bắt buộc** — nếu có thì bản ghi tối thiểu thiếu nó;
        * không luật liên-trường nào (`row_rules`) — một luật có thể đúng là thứ
          mà "mã + tên" vi phạm, và biết chắc điều đó đòi chạy SQL. Loại cả nhóm
          là câu trả lời an toàn.

        Chiều trôi vì thế luôn an toàn: thêm một ràng buộc ở phase sau chỉ khiến
        một danh mục **thôi** tự tạo được, không bao giờ khiến nó tự tạo được một
        bản ghi sai. `tests/test_import_missing_references.py` ghim danh sách hiện
        tại để một thay đổi ngoài ý muốn vẫn phải được nhìn thấy.

        Ví dụ hôm nay: đơn vị tính và kho tự tạo được; đối tác thì **không**
        (`partner_is_customer_or_vendor` — một bản ghi không phải khách, không
        phải NCC, không phải nhóm sẽ không hiện ở danh sách nào), vật tư hàng hóa
        cũng không.
        """
        if self.row_rules:
            return False
        if self.extra_fields is None:
            return True
        return not any(info.is_required() for info in self.extra_fields.model_fields.values())


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

    def extend_merge_hooks(self, slug: str, hook: MergeHook) -> None:
        """Cho MODULE gắn thêm hook gộp vào một danh mục kernel (lát 6D).

        Tồn tại vì chiều phụ thuộc: bảng con mang ràng buộc duy nhất có thể
        thuộc một module (`bank_statement_lines` → `company_bank_accounts`),
        mà kernel không import được module (luật phụ thuộc #5) — nên hook viết
        ở module và tự gắn lúc import, cùng thời điểm mọi đăng ký khác của
        `ket.model_registry`. Cổng `test_master_data_merge.py` đọc registry lúc
        chạy nên vẫn canh đủ: thiếu lời gọi này là cổng đỏ.
        """
        spec = self._specs.get(slug)
        if spec is None:
            raise ValueError(f"Danh mục {slug!r} chưa được đăng ký — gắn hook không có chỗ đặt")
        self._specs[slug] = replace(spec, merge_hooks=(*spec.merge_hooks, hook))

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
    """Hai mươi danh mục của lát 3A + 3B-1 + 3B-2 + 3B-3, cộng tài khoản
    ngân hàng doanh nghiệp của lát 6A.

    Xếp theo nhóm nghiệp vụ chứ không theo bảng chữ cái: người đọc tệp này đang
    tìm "danh mục kho nằm ở đâu", không tìm chữ cái. Thứ tự **xuất ra** thì đã
    được `specs()` sắp lại nên nhóm ở đây không ảnh hưởng đặc tả OpenAPI.
    """
    # Sáu chiều phân tích lõi của LD-08. `branch_id` là chiều thứ sáu và nằm ở
    # `branches` — bảng đó không phải danh mục (H38) nên không có mặt ở đây.
    for spec in (
        # Đối tượng công nợ và con người
        CatalogSpec(
            slug="partners",
            model=Partner,
            title="Đối tác",
            extra_fields=PartnerFields,
            # FR-SYS-031: **một** bản ghi, hai danh sách.
            flags=(
                CatalogFlag(value="customer", column="is_customer", title="Khách hàng"),
                CatalogFlag(value="vendor", column="is_vendor", title="Nhà cung cấp"),
            ),
            references=(CatalogReference(field="payment_term_id", slug="payment_terms"),),
            # Bảng con `partner_bank_accounts` mang hai ràng buộc duy nhất theo
            # `partner_id`, nên gộp phải có bước chuẩn bị (review H1).
            merge_hooks=(PartnerBankAccountMergeHook(),),
            row_rules=partner_row_rules(),
        ),
        CatalogSpec(
            slug="employees",
            model=Employee,
            title="Nhân viên",
            extra_fields=EmployeeFields,
            references=(CatalogReference(field="bank_id", slug="banks"),),
            row_rules=employee_row_rules(),
        ),
        CatalogSpec(slug="cost_objects", model=CostObject, title="Đối tượng tập hợp chi phí"),
        CatalogSpec(slug="expense_items", model=ExpenseItem, title="Khoản mục chi phí"),
        CatalogSpec(slug="projects", model=Project, title="Công trình"),
        CatalogSpec(slug="project_types", model=ProjectType, title="Loại công trình"),
        CatalogSpec(slug="contracts", model=Contract, title="Hợp đồng"),
        # Vật tư – kho
        CatalogSpec(
            slug="items",
            model=Item,
            title="Vật tư hàng hóa",
            extra_fields=ItemFields,
            # `nature` và `base_unit_id` chốt một lần lúc tạo (H69).
            extra_update_fields=ItemEditableFields,
            update_guard=ItemUpdateGuard(),
            references=(
                CatalogReference(field="base_unit_id", slug="units_of_measure"),
                CatalogReference(field="warehouse_id", slug="warehouses"),
            ),
            # Hook từ chối (`base_unit_differs`) đứng trước hook dọn dữ liệu, và
            # hai hook giá dựa vào chính phép từ chối ấy: ngưỡng bậc chiết khấu và
            # dòng giá "theo đơn vị chính" chỉ so được khi hai mã hàng cùng đơn vị
            # chính.
            merge_hooks=(
                ItemUnitOfItemMergeHook(),
                ItemVariantMergeHook(),
                ItemPriceLevelOfItemMergeHook(),
                ItemDiscountTierMergeHook(),
                PriceListLineOfItemMergeHook(),
            ),
            row_rules=item_row_rules(),
        ),
        CatalogSpec(
            slug="price_lists",
            model=PriceList,
            title="Bảng giá",
            extra_fields=PriceListFields,
            references=(
                CatalogReference(field="partner_id", slug="partners"),
                CatalogReference(field="contract_id", slug="contracts"),
            ),
            # `price_list_lines` mang hai chỉ số duy nhất chứa `price_list_id`, nên
            # gộp hai bảng giá đụng chúng ở mọi mã hàng cả hai cùng khai — mà hai
            # bảng giá đáng gộp thì gần như chắc chắn có mã hàng chung.
            merge_hooks=(PriceListLineOfPriceListMergeHook(),),
            row_rules=price_list_row_rules(),
        ),
        CatalogSpec(slug="warehouses", model=Warehouse, title="Kho"),
        CatalogSpec(
            slug="units_of_measure",
            model=UnitOfMeasure,
            title="Đơn vị tính",
            # `item_units` và `item_price_levels` đều mang ràng buộc duy nhất theo
            # **cả** `unit_id`, nên gộp hai đơn vị tính đụng chúng ở mọi mã hàng đã
            # khai tỷ lệ hoặc giá cho cả hai. Hook từ chối (`unit_factor_conflicts`)
            # đứng trước: hook giá dọn dẹp dựa vào việc hai đơn vị đã chứng minh là
            # một.
            merge_hooks=(
                UnitOfMeasureMergeHook(),
                UnitOfMeasurePriceMergeHook(),
                PriceListLineOfUnitMergeHook(),
            ),
        ),
        # Tài sản
        CatalogSpec(
            slug="asset_types",
            model=AssetType,
            title="Loại tài sản cố định",
            extra_fields=AssetTypeFields,
            row_rules=asset_type_row_rules(),
        ),
        CatalogSpec(
            slug="tool_types",
            model=ToolType,
            title="Loại công cụ dụng cụ",
            extra_fields=ToolTypeFields,
            row_rules=tool_type_row_rules(),
        ),
        # Thanh toán – ngân hàng
        CatalogSpec(
            slug="payment_terms",
            model=PaymentTerm,
            title="Điều khoản thanh toán",
            extra_fields=PaymentTermFields,
            row_rules=payment_term_row_rules(),
        ),
        CatalogSpec(
            slug="banks",
            model=Bank,
            title="Ngân hàng",
            extra_fields=BankFields,
            row_rules=bank_row_rules(),
            # `bank_statement_profiles` mang ràng buộc duy nhất `(bank_id, name)`
            # từ lát 3C-2, nên gộp hai ngân hàng phải có bước hợp nhất (RT-26).
            merge_hooks=(BankStatementProfileMergeHook(),),
        ),
        # Danh mục thứ 21 (lát 6A) — tài khoản ngân hàng CỦA DOANH NGHIỆP.
        # Ở kernel chứ không trong module bank vì sheet số dư đầu kỳ nhóm
        # ngân hàng (posting, hoãn từ 4C) cũng đọc nó — xem docstring model.
        CatalogSpec(
            slug="company_bank_accounts",
            model=CompanyBankAccount,
            title="Tài khoản ngân hàng doanh nghiệp",
            extra_fields=CompanyBankAccountFields,
            references=(CatalogReference(field="bank_id", slug="banks"),),
        ),
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
