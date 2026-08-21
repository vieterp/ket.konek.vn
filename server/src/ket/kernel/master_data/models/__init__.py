"""Bảng danh mục — một tệp cho mỗi danh mục.

Một tệp cho mỗi bảng chứ không một tệp `models.py` khổng lồ: tới cuối phase 3 ở
đây có hơn hai mươi danh mục, và những cái nặng nhất (vật tư hàng hóa với đơn vị
quy đổi, bảng giá nhiều mức, định mức nguyên vật liệu — FR-SYS-040..046) kéo theo
vài bảng con mỗi cái.

Lát 3A dựng khung (`base.py`, `tree_path.py`, `service.py`) cùng **hai** danh mục
thật để khung có người dùng thật thay vì một model chỉ-để-test: đối tượng tập hợp
chi phí và khoản mục chi phí. Cả hai là chiều phân tích lõi của LD-08, cả hai là
cây thuần không bảng con, và phase 4 cần chúng để gắn vào dòng phát sinh sổ cái.

Lát 3B-1 thêm mười ba danh mục còn lại thuộc loại "cây thuần" và bốn cột riêng
lẻ (H50). Lát 3B-2 thêm đối tác (gộp khách hàng + nhà cung cấp, FR-SYS-031) cùng
bảng con tài khoản ngân hàng của nó, và nhân viên. Lát 3B-3 thêm vật tư hàng hóa
cùng **hai** bảng con MUST của nó — đơn vị quy đổi (FR-SYS-041) và mã quy cách
(FR-SYS-046, là trục khóa của bảng tồn kho phase 8 nên không hoãn được, H65);
bảng giá, định mức nguyên vật liệu và bậc chiết khấu về phase 7/9 nơi có chỗ đọc
chúng. Còn thiếu: hệ thống tài khoản (phase 5 — là gói cấu hình).

Danh sách `__all__` ở đây là **danh sách nạp model** cho `ket.model_registry`;
thứ quyết định danh mục nào hiện ra API là `registry.py` bên cạnh.
"""

from __future__ import annotations

from ket.kernel.master_data.models.asset_type import AssetType
from ket.kernel.master_data.models.bank import Bank
from ket.kernel.master_data.models.company_bank_account import CompanyBankAccount
from ket.kernel.master_data.models.contract import Contract
from ket.kernel.master_data.models.cost_object import CostObject
from ket.kernel.master_data.models.document_type import DocumentTypeCatalog
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.excise_tax_table import ExciseTaxTable
from ket.kernel.master_data.models.expense_item import ExpenseItem
from ket.kernel.master_data.models.invoice_form import InvoiceForm
from ket.kernel.master_data.models.item import Item
from ket.kernel.master_data.models.item_unit import ItemUnit
from ket.kernel.master_data.models.item_variant import ItemVariant
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.models.partner_bank_account import PartnerBankAccount
from ket.kernel.master_data.models.payment_term import PaymentTerm
from ket.kernel.master_data.models.pit_table import PitTable
from ket.kernel.master_data.models.project import Project
from ket.kernel.master_data.models.project_type import ProjectType
from ket.kernel.master_data.models.resource_tax_table import ResourceTaxTable
from ket.kernel.master_data.models.timekeeping_symbol import TimekeepingSymbol
from ket.kernel.master_data.models.tool_type import ToolType
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.master_data.models.warehouse import Warehouse

__all__ = [
    "AssetType",
    "Bank",
    "CompanyBankAccount",
    "Contract",
    "CostObject",
    "DocumentTypeCatalog",
    "Employee",
    "ExciseTaxTable",
    "ExpenseItem",
    "InvoiceForm",
    "Item",
    "ItemUnit",
    "ItemVariant",
    "Partner",
    "PartnerBankAccount",
    "PaymentTerm",
    "PitTable",
    "Project",
    "ProjectType",
    "ResourceTaxTable",
    "TimekeepingSymbol",
    "ToolType",
    "UnitOfMeasure",
    "Warehouse",
]
