"""Dữ liệu `auto_posting_rules.csv` của hai gói dựng sẵn (FR-SYS-025, lát 6A).

Không cần PostgreSQL: kiểm thẳng trên dữ liệu đã nạp qua loader — đúng bộ luật
mà seed/importer sẽ ghi. Hai bất biến mà không constraint DB nào canh được:

* **Đổi chế độ không đổi nghiệp vụ** (LD-06, cùng tinh thần `test_scheme_switch`):
  TT99 và TT133 phải khai cùng tập `(loại chứng từ, mã nghiệp vụ)` — khác nhau
  ở TK gán vào, không ở danh sách nghiệp vụ mà form hiển thị.
* **Purpose của nghiệp vụ phải phân giải ra TK hạch-toán-được**: validator ghi
  sổ từ chối TK tổng hợp (BR-SYS-03), nên một purpose trỏ vào TK `is_summary`
  là nghiệp vụ điền sẵn một định khoản chắc chắn bị từ chối — chính là lớp lỗi
  "TT133 `cash=111` trong khi 111 là TK tổng hợp" mà lát 6A đã sửa.
"""

from __future__ import annotations

from ket.kernel.config.packages.loader import LoadedPackage, load_builtin_package
from ket.kernel.config.packages.seed import BUILTIN_PACKAGE_SLUGS


def _operation_keys(loaded: LoadedPackage) -> set[tuple[str, str]]:
    return {(rule.document_type, rule.operation_code) for rule in loaded.auto_posting_rules}


def test_both_builtin_packages_declare_the_same_operations() -> None:
    first, second = (load_builtin_package(slug) for slug in BUILTIN_PACKAGE_SLUGS)
    assert _operation_keys(first) == _operation_keys(second)
    assert _operation_keys(first), "gói dựng sẵn phải mang bộ nghiệp vụ FR-SYS-025"


def test_every_rule_purpose_resolves_to_a_postable_account() -> None:
    for slug in BUILTIN_PACKAGE_SLUGS:
        loaded = load_builtin_package(slug)
        summary_codes = {row.code for row in loaded.accounts if row.is_summary}
        account_by_purpose = {row.purpose: row.account_code for row in loaded.default_accounts}
        for rule in loaded.auto_posting_rules:
            for purpose in (rule.debit_purpose, rule.credit_purpose):
                if purpose is None:
                    continue
                code = account_by_purpose[purpose]  # loader đã bảo đảm purpose tồn tại
                assert code not in summary_codes, (
                    f"gói {slug}: nghiệp vụ {rule.operation_code} trỏ purpose "
                    f"{purpose!r} vào TK tổng hợp {code} — định khoản điền sẵn "
                    "sẽ bị validator ghi sổ từ chối (BR-SYS-03)"
                )


def test_cash_and_bank_defaults_are_postable_in_every_builtin_package() -> None:
    """Hai purpose nền của mọi phiếu tiền (`cash`, `bank`) phải hạch toán được
    — kiểm riêng vì chúng được điền vào **mọi** nghiệp vụ của phân hệ 6."""
    for slug in BUILTIN_PACKAGE_SLUGS:
        loaded = load_builtin_package(slug)
        summary_codes = {row.code for row in loaded.accounts if row.is_summary}
        account_by_purpose = {row.purpose: row.account_code for row in loaded.default_accounts}
        for purpose in ("cash", "bank"):
            assert account_by_purpose[purpose] not in summary_codes, (
                f"gói {slug}: purpose {purpose!r} trỏ vào TK tổng hợp"
            )
