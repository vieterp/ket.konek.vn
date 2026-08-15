"""Registry loại chứng từ → mã quyền (FR-SYS-071).

Bộ test này canh đúng một lời hứa kiến trúc, và là lời hứa mà rủi ro trong plan
nêu đích danh: *"RBAC thiếu chiều loại chứng từ → phase 6 phải đổi schema
quyền"*. Nếu thêm một loại chứng từ mới đòi sửa bảng, sửa migration hay sửa một
danh sách mã viết tay ở đâu đó, thì `test_a_new_document_type_needs_no_schema_change`
sẽ là chỗ điều đó lộ ra.

Không cần PostgreSQL: registry là cấu trúc trong bộ nhớ, và đó chính là điểm —
`permissions` chỉ là **ảnh chiếu** của nó xuống DB (`role_service.sync_permissions`).
"""

from __future__ import annotations

import pytest

from ket.kernel.security.auth_models import SessionScope, session_scope_of
from ket.kernel.security.permissions import (
    REGISTRY,
    SYSTEM_MODULE,
    Action,
    DocumentType,
    PermissionRegistry,
    permission_code,
)


def test_permission_code_has_exactly_three_parts() -> None:
    """Dạng `{module}.{chứng từ}.{hành vi}` là hợp đồng công khai (FR-SYS-071)."""
    code = permission_code("cash_book", "receipt", Action.POST)
    assert code == "cash_book.receipt.post"
    assert len(code.split(".")) == 3


def test_a_new_document_type_needs_no_schema_change() -> None:
    """Hai loại chứng từ mới → mã quyền có ngay, không đụng bảng nào.

    Dựng registry riêng thay vì đăng ký vào `REGISTRY` thật: một test làm bẩn
    registry toàn cục sẽ làm test khác hỏng theo thứ tự chạy.
    """
    registry = PermissionRegistry()
    registry.register(
        DocumentType(
            module="cash_book",
            code="receipt",
            actions=frozenset({Action.VIEW, Action.CREATE, Action.POST, Action.UNPOST}),
        )
    )
    registry.register(
        DocumentType(
            module="bank",
            code="payment_order",
            actions=frozenset({Action.VIEW, Action.CREATE}),
            requires_second_factor=True,
        )
    )

    assert registry.codes() == (
        "bank.payment_order.create",
        "bank.payment_order.view",
        "cash_book.receipt.create",
        "cash_book.receipt.post",
        "cash_book.receipt.unpost",
        "cash_book.receipt.view",
    )
    # FR-SYS-074: quyền ngân hàng điện tử kéo theo 2FA, quyền quỹ thì không.
    assert registry.second_factor_codes() == {
        "bank.payment_order.create",
        "bank.payment_order.view",
    }


def test_registering_the_same_document_type_twice_is_an_error() -> None:
    """Ghi đè im lặng sẽ khiến bên thua cuộc mất quyền theo thứ tự import."""
    registry = PermissionRegistry()
    doc = DocumentType(module="sales", code="invoice", actions=frozenset({Action.VIEW}))
    registry.register(doc)
    with pytest.raises(ValueError, match="đã được đăng ký"):
        registry.register(doc)


@pytest.mark.parametrize(
    ("module", "code"),
    [("sales.x", "invoice"), ("sales", "in voice"), ("Sales", "invoice"), ("sales", "hóa_đơn")],
)
def test_identifiers_outside_the_whitelist_are_refused(module: str, code: str) -> None:
    """Mã quyền được tách bằng dấu chấm và đi vào DB — một ký tự lạ làm lệch cả hai."""
    with pytest.raises(ValueError, match="Identifier không hợp lệ"):
        DocumentType(module=module, code=code, actions=frozenset({Action.VIEW}))


def test_a_document_type_without_actions_is_refused() -> None:
    with pytest.raises(ValueError, match="không có hành vi nào"):
        DocumentType(module="sales", code="invoice", actions=frozenset())


def test_the_process_registry_covers_this_slice_and_marks_admin_codes() -> None:
    """Registry thật phải có đủ mã mà router `system` dựa vào.

    Danh sách viết thẳng chứ không sinh lại từ registry: một test sinh mã bằng
    chính hàm đang kiểm sẽ xanh cả khi registry rỗng.
    """
    codes = set(REGISTRY.codes())
    assert {
        f"{SYSTEM_MODULE}.branch.view",
        f"{SYSTEM_MODULE}.branch.create",
        f"{SYSTEM_MODULE}.role.edit",
        f"{SYSTEM_MODULE}.user.edit",
        f"{SYSTEM_MODULE}.audit_log.view",
        f"{SYSTEM_MODULE}.setting.edit",
    } <= codes

    sensitive = REGISTRY.second_factor_codes()
    # Quản lý tài khoản và vai trò = tự cấp được mọi quyền còn lại (FR-NFR-016).
    assert f"{SYSTEM_MODULE}.role.edit" in sensitive
    assert f"{SYSTEM_MODULE}.user.edit" in sensitive
    # Xem danh mục chi nhánh thì không — bắt 2FA cho mọi thứ là cách nhanh nhất
    # để người dùng tìm đường vòng.
    assert f"{SYSTEM_MODULE}.branch.view" not in sensitive


def test_audit_log_cannot_be_written_through_permissions() -> None:
    """Bất biến chỉ-thêm của nhật ký không được có một cửa sau ở tầng quyền."""
    codes = set(REGISTRY.codes())
    for action in (Action.CREATE, Action.EDIT, Action.DELETE):
        assert f"{SYSTEM_MODULE}.audit_log.{action.value}" not in codes


# --------------------------------------------------------------------------
# Phạm vi phiên — hướng hỏng phải là CHẶN
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("full", SessionScope.FULL),
        ("totp_enrollment", SessionScope.TOTP_ENROLLMENT),
        ("", SessionScope.TOTP_ENROLLMENT),
        ("admin", SessionScope.TOTP_ENROLLMENT),
        ("FULL", SessionScope.TOTP_ENROLLMENT),
    ],
)
def test_an_unknown_session_scope_downgrades_instead_of_upgrading(
    raw: str, expected: SessionScope
) -> None:
    """Giá trị lạ trong cột `scope` phải hạ xuống phiên hạn chế, không nâng lên.

    Ràng buộc `CHECK` của bảng đã chặn giá trị lạ, nên đường duy nhất tới đây là
    một bản khôi phục từ dump thiếu ràng buộc hoặc một lần sửa tay — đúng những
    lúc không nên tin dữ liệu. Nâng lên `full` ở đó là biến một dòng rác thành
    một phiên đầy quyền.
    """
    assert session_scope_of(raw) is expected
