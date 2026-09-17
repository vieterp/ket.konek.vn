"""Khung dựng bối cảnh cho test phân hệ Bán hàng (lát 7C-2).

Bồi lên `posting_support.seed_posting_context` cùng lối `purchase_support`:
gói `TT99-TEST` thắng `resolve_package` trong dataset test, nên TK doanh thu /
thuế đầu ra / giảm trừ doanh thu và nghiệp vụ `SAL` (FR-SYS-025) phải được gieo
vào CHÍNH gói đó — idempotent theo khóa tự nhiên để nhiều tệp test dùng chung
dataset không giẫm nhau (kể cả giẫm lên phần `cash_book_support` và
`purchase_support` gieo: cùng 515/635 và cùng cặp `(*, fx_gain/fx_loss)`).

`131` ở đây theo dõi **khách hàng** — chính phép kiểm mà
`SalesInvoiceService._verify_customer_tracked_account` canh; `1311` là TK phải
thu **thứ hai** để kiểm ca "đối trừ vào khoản nợ nằm trên TK khác", đối xứng
với `3388` của chiều mua.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ket.kernel.config.accounts_models import BalanceNature, ChartOfAccount, DefaultAccount
from ket.kernel.config.auto_posting_models import AutoPostingRule
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.master_data.models.employee import Employee
from ket.kernel.master_data.models.item_variant import ItemVariant
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.persistence.unit_of_work import unit_of_work
from posting_support import PostingContext, posting_scope

SEED_ACTOR_ID = 1

_EXTRA_ACCOUNT_SPECS: tuple[tuple[str, str, int, list[str] | None], ...] = (
    # (code, name, balance_nature, detail_tracking)
    ("131", "Phải thu của khách hàng", BalanceNature.DUAL, ["customer"]),
    # TK phải thu THỨ HAI theo dõi khách hàng — để kiểm "đối trừ vào khoản nợ
    # nằm trên TK khác" mà không phải bịa một TK không theo dõi đối tác.
    ("1311", "Phải thu khách hàng — nhóm hai", BalanceNature.DUAL, ["customer"]),
    ("511", "Doanh thu bán hàng và cung cấp dịch vụ", BalanceNature.CREDIT, None),
    ("5111", "Doanh thu bán hàng hóa", BalanceNature.CREDIT, None),
    ("5112", "Doanh thu cung cấp dịch vụ", BalanceNature.CREDIT, None),
    ("521", "Các khoản giảm trừ doanh thu", BalanceNature.CREDIT, None),
    ("33311", "Thuế GTGT đầu ra phải nộp", BalanceNature.CREDIT, None),
    ("515", "Doanh thu hoạt động tài chính", BalanceNature.NONE, None),
    ("635", "Chi phí tài chính", BalanceNature.NONE, None),
)

_DEFAULT_ACCOUNTS: tuple[tuple[str, str], ...] = (("fx_gain", "515"), ("fx_loss", "635"))

_RULES: tuple[tuple[str, str, str, bool, int | None, int], ...] = (
    # (document_type, code, name, requires_partner, partner_kind, order)
    ("SAL", "ban-hang-hoa", "Bán hàng hóa trong nước", True, 0, 1),
    ("SAL", "ban-dich-vu", "Bán dịch vụ", True, 0, 2),
    ("SAL", "ban-hang-dai-ly", "Bán hàng qua đại lý", True, 0, 3),
    ("SAL", "tra-lai-hang-ban", "Hàng bán bị trả lại", True, 0, 4),
    ("SAL", "giam-gia-hang-ban", "Giảm giá hàng bán", True, 0, 5),
    ("SAL", "dieu-chinh-tang-hoa-don", "Điều chỉnh tăng hóa đơn đã phát hành", True, 0, 6),
    ("SAL", "dieu-chinh-giam-hoa-don", "Điều chỉnh giảm hóa đơn đã phát hành", True, 0, 7),
    # Nghiệp vụ gói khai sai loại đối tác — để kiểm module từ chối.
    ("SAL", "ban-cho-ncc", "Nghiệp vụ khai nhầm cho nhà cung cấp", True, 1, 9),
)


def seed_sales_package_data(
    session_factory: sessionmaker[Session],
    dataset: DatasetRef,
    context: PostingContext,
) -> dict[str, int]:
    """Gieo TK + purpose + nghiệp vụ bán vào gói test; trả bảng số hiệu → id."""
    accounts = dict(context.accounts)
    scope = posting_scope(dataset, context, user_id=SEED_ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        for code, name, nature, tracking in _EXTRA_ACCOUNT_SPECS:
            row = session.scalar(
                select(ChartOfAccount).where(
                    ChartOfAccount.package_id == context.package_id, ChartOfAccount.code == code
                )
            )
            if row is None:
                row = ChartOfAccount(
                    package_id=context.package_id,
                    code=code,
                    name=name,
                    path="0.",
                    balance_nature=nature,
                    is_summary=False,
                    detail_tracking=tracking,
                )
                session.add(row)
                session.flush()
                row.path = f"{row.id}."
                session.flush()
            accounts[code] = row.id

        for purpose, account_code in _DEFAULT_ACCOUNTS:
            if session.get(DefaultAccount, (context.package_id, "*", purpose)) is None:
                session.add(
                    DefaultAccount(
                        package_id=context.package_id,
                        document_type="*",
                        purpose=purpose,
                        account_code=account_code,
                    )
                )
        for document_type, code, name, requires, partner_kind, order in _RULES:
            existing = session.scalar(
                select(AutoPostingRule.id).where(
                    AutoPostingRule.package_id == context.package_id,
                    AutoPostingRule.document_type == document_type,
                    AutoPostingRule.operation_code == code,
                )
            )
            if existing is None:
                session.add(
                    AutoPostingRule(
                        package_id=context.package_id,
                        document_type=document_type,
                        operation_code=code,
                        operation_name=name,
                        requires_partner=requires,
                        partner_kind=partner_kind,
                        display_order=order,
                    )
                )
        session.flush()
    return accounts


def ensure_customer_group(
    session: Session, *, partner_id: int, code: str, parent_id: int | None = None
) -> int:
    """Một NÚT NHÓM của cây đối tác — chiều gộp "theo nhóm khách hàng"
    (SRS 06 §5.2 #9).

    Nhóm khách hàng không phải một danh mục riêng: nó là chính cây của
    `MasterDataRow` (`parent_id` + `is_group`), xem docstring `partner.py`. Nút
    nhóm được miễn ràng buộc "phải là khách hoặc nhà cung cấp" — nó chỉ để gom
    cây và không bao giờ lên chứng từ.
    """
    if session.get(Partner, partner_id) is None:
        path = f"{partner_id}." if parent_id is None else f"{parent_id}.{partner_id}."
        session.add(
            Partner(
                id=partner_id,
                code=code,
                name=f"Nhóm {code}",
                path=path,
                parent_id=parent_id,
                is_group=True,
            )
        )
        session.flush()
    return partner_id


def ensure_customer(
    session: Session,
    *,
    partner_id: int,
    code: str,
    credit_limit: Decimal | None = None,
    payment_term_id: int | None = None,
    province: str | None = None,
    group_id: int | None = None,
) -> int:
    """Một khách hàng với `id` cố định — idempotent để fixture module dùng lại.

    `province` là chiều gộp "theo địa phương" của SRS 06 §5.1 #1: nó nằm trên
    danh mục khách hàng, không trên chứng từ, nên báo cáo theo địa phương chỉ có
    nhóm khi bước gieo khai nó.

    `group_id` treo khách hàng dưới một nút nhóm (`ensure_customer_group`), kể cả
    một nút nhóm nằm sâu trong cây — `path` ghép từ path của nhóm cha, nên bước
    gieo dựng được cây nhiều cấp.
    `path` ghép tay ở đây vì bước gieo đi thẳng vào model chứ không qua service
    — đúng lối mọi helper khác của tệp này; đường sinh path thật nằm ở
    `tree_path`, và không đường ghi SẢN PHẨM nào ghép chuỗi path.
    """
    if group_id is None:
        path = f"{partner_id}."
    else:
        group = session.get(Partner, group_id)
        assert group is not None, f"nhóm {group_id} chưa được gieo"
        path = f"{group.path}{partner_id}."
    existing = session.get(Partner, partner_id)
    if existing is None:
        session.add(
            Partner(
                id=partner_id,
                code=code,
                name=f"Khách hàng {code}",
                path=path,
                parent_id=group_id,
                is_customer=True,
                credit_limit=credit_limit,
                payment_term_id=payment_term_id,
                province=province,
            )
        )
    else:
        existing.credit_limit = credit_limit
        existing.payment_term_id = payment_term_id
        existing.province = province
        existing.parent_id = group_id
        existing.path = path
    session.flush()
    return partner_id


def ensure_variant(session: Session, *, variant_id: int, item_id: int, code: str) -> int:
    """Một quy cách của một mã hàng, `id` cố định.

    Mã quy cách chỉ duy nhất TRONG một mã hàng
    (`uq_item_variants_item_code`), nên hai mã hàng dùng chung một mã quy cách là
    dữ liệu hợp lệ — và là dữ liệu duy nhất phân biệt được "gộp theo quy cách"
    làm đúng với "gộp theo quy cách trộn hai mã hàng".
    """
    if session.get(ItemVariant, variant_id) is None:
        session.add(
            ItemVariant(
                id=variant_id,
                item_id=item_id,
                code=code,
                name=f"Quy cách {code}",
            )
        )
        session.flush()
    return variant_id


def ensure_salesperson(session: Session, *, employee_id: int, code: str) -> int:
    """Một nhân viên bán hàng với `id` cố định — bộ đếm tham chiếu đọc bảng này."""
    if session.get(Employee, employee_id) is None:
        session.add(
            Employee(
                id=employee_id,
                code=code,
                name=f"Nhân viên {code}",
                path=f"{employee_id}.",
            )
        )
        session.flush()
    return employee_id
