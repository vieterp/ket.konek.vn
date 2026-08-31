"""`kernel/config/packages/loader.py` — đọc + kiểm hợp đồng 4 tệp (phase-05 §5A).

Không cần PostgreSQL: loader chỉ đọc đĩa/bộ nhớ và trả dữ liệu đã kiểm, không
chạm DB. Đường dẫn hạnh phúc đi qua thư mục cố định trên đĩa
(`tests/fixtures/config_packages/valid_min/`, đúng hợp đồng); từng nhánh
fail-closed dùng `load_package_from_texts` với nội dung dựng tại chỗ — nhanh
hơn và mỗi test chỉ đổi đúng một điều kiện đang kiểm.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ket.kernel.config.packages.loader import (
    ACCOUNTS_FILE,
    CLOSING_PAIRS_FILE,
    DEFAULT_ACCOUNTS_FILE,
    PACKAGE_MANIFEST_FILE,
    load_package_directory,
    load_package_from_texts,
)
from ket.kernel.errors import ConfigPackageDataInvalidError

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "config_packages"

_VALID_MANIFEST = (
    '{"code": "T1", "scheme": "TT99", "name": "Gói T1", "name_en": null, '
    '"description": null, "legal_reference": null, "effective_from": "2020-01-01", '
    '"effective_to": null, "version": 1}'
)
_VALID_ACCOUNTS = (
    "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
    "detail_tracking,is_locked\n"
    "111,Tiền mặt,,,0,0,0,,1\n"
    "112,Tiền gửi ngân hàng,,,0,1,0,,1\n"
    "1121,Tiền gửi VND,,112,0,0,0,bank_account,0\n"
    "131,Phải thu của khách hàng,,,2,0,0,customer,1\n"
)
_VALID_DEFAULT_ACCOUNTS = "document_type,purpose,account_code\n*,cash,111\n"
_VALID_CLOSING_PAIRS = "source_account,target_account,sequence,description\n111,131,1,Test\n"


def _valid_texts(**overrides: str) -> dict[str, str]:
    texts = {
        PACKAGE_MANIFEST_FILE: _VALID_MANIFEST,
        ACCOUNTS_FILE: _VALID_ACCOUNTS,
        DEFAULT_ACCOUNTS_FILE: _VALID_DEFAULT_ACCOUNTS,
        CLOSING_PAIRS_FILE: _VALID_CLOSING_PAIRS,
    }
    texts.update(overrides)
    return texts


def test_happy_path_loads_a_directory_matching_the_contract() -> None:
    loaded = load_package_directory(FIXTURES_DIR / "valid_min")

    assert loaded.manifest.code == "TEST-MIN-1"
    assert loaded.manifest.scheme == "TT99"
    assert [row.code for row in loaded.accounts] == ["111", "112", "1121", "131", "331", "911"]
    assert loaded.accounts[2].parent_code == "112"
    assert {row.purpose for row in loaded.default_accounts} == {"cash", "ar_trade"}
    assert loaded.closing_pairs[0].source_account == "911"


def test_happy_path_from_in_memory_texts() -> None:
    loaded = load_package_from_texts(_valid_texts())
    assert loaded.manifest.code == "T1"
    assert len(loaded.accounts) == 4


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigPackageDataInvalidError):
        load_package_directory(tmp_path / "khong-ton-tai")


def test_missing_required_file_is_rejected(tmp_path: Path) -> None:
    partial = tmp_path / "thieu-tep"
    partial.mkdir()
    (partial / PACKAGE_MANIFEST_FILE).write_text(_VALID_MANIFEST, encoding="utf-8")
    with pytest.raises(ConfigPackageDataInvalidError):
        load_package_directory(partial)


def test_malformed_manifest_json_is_rejected() -> None:
    with pytest.raises(ConfigPackageDataInvalidError):
        load_package_from_texts(_valid_texts(**{PACKAGE_MANIFEST_FILE: "{ khong phai JSON"}))


def test_unknown_scheme_is_rejected() -> None:
    manifest = _VALID_MANIFEST.replace('"scheme": "TT99"', '"scheme": "TT999-khong-ton-tai"')
    with pytest.raises(ConfigPackageDataInvalidError):
        load_package_from_texts(_valid_texts(**{PACKAGE_MANIFEST_FILE: manifest}))


def test_effective_to_before_effective_from_is_rejected() -> None:
    manifest = _VALID_MANIFEST.replace('"effective_to": null', '"effective_to": "2019-01-01"')
    with pytest.raises(ConfigPackageDataInvalidError):
        load_package_from_texts(_valid_texts(**{PACKAGE_MANIFEST_FILE: manifest}))


def test_wrong_accounts_header_is_rejected() -> None:
    bad = "code,name\n111,Tiền mặt\n"
    with pytest.raises(ConfigPackageDataInvalidError):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_duplicate_account_code_is_rejected() -> None:
    bad = _VALID_ACCOUNTS + "111,Trùng mã,,,0,0,0,,0\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="trùng"):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_missing_parent_account_is_rejected() -> None:
    """Cha phải xuất hiện **trước** con trong tệp — thiếu cha là lỗi cấu trúc cây."""
    bad = (
        "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
        "detail_tracking,is_locked\n"
        "1121,Tiền gửi VND,,112,0,0,0,bank_account,0\n"
    )
    with pytest.raises(ConfigPackageDataInvalidError, match="cha"):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_unknown_detail_tracking_token_is_rejected() -> None:
    bad = _VALID_ACCOUNTS.replace(
        "131,Phải thu của khách hàng,,,2,0,0,customer,1",
        "131,Phải thu của khách hàng,,,2,0,0,khong_ton_tai,1",
    )
    with pytest.raises(ConfigPackageDataInvalidError, match="detail_tracking"):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_balance_nature_out_of_range_is_rejected() -> None:
    bad = _VALID_ACCOUNTS.replace("111,Tiền mặt,,,0,0,0,,1", "111,Tiền mặt,,,9,0,0,,1")
    with pytest.raises(ConfigPackageDataInvalidError, match="balance_nature"):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_default_account_pointing_to_unknown_code_is_rejected() -> None:
    bad = "document_type,purpose,account_code\n*,cash,999999\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="999999"):
        load_package_from_texts(_valid_texts(**{DEFAULT_ACCOUNTS_FILE: bad}))


def test_closing_pair_pointing_to_unknown_code_is_rejected() -> None:
    bad = "source_account,target_account,sequence,description\n999999,131,1,Test\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="999999"):
        load_package_from_texts(_valid_texts(**{CLOSING_PAIRS_FILE: bad}))


def test_duplicate_default_account_key_is_rejected() -> None:
    bad = "document_type,purpose,account_code\n*,cash,111\n*,cash,112\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="trùng"):
        load_package_from_texts(_valid_texts(**{DEFAULT_ACCOUNTS_FILE: bad}))


def test_overlong_account_code_is_rejected() -> None:
    """Giá trị dài hơn cột DB phải chết ở lượt kiểm, không sống tới flush (DataError thô)."""
    long_code = "9" * 21  # cột `chart_of_accounts.code` là String(20)
    bad = _VALID_ACCOUNTS + f"{long_code},TK mã quá dài,,,0,0,0,,0\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="vượt trần"):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_overlong_account_name_is_rejected() -> None:
    bad = _VALID_ACCOUNTS + f"999,{'A' * 256},,,0,0,0,,0\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="vượt trần"):
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: bad}))


def test_overlong_purpose_is_rejected() -> None:
    bad = f"document_type,purpose,account_code\n*,{'p' * 51},111\n"
    with pytest.raises(ConfigPackageDataInvalidError, match="vượt trần"):
        load_package_from_texts(_valid_texts(**{DEFAULT_ACCOUNTS_FILE: bad}))


def test_overlong_manifest_code_is_rejected() -> None:
    bad = _VALID_MANIFEST.replace('"code": "T1"', f'"code": "{"C" * 51}"')
    with pytest.raises(ConfigPackageDataInvalidError, match="vượt trần"):
        load_package_from_texts(_valid_texts(**{PACKAGE_MANIFEST_FILE: bad}))


def test_loader_length_limits_match_orm_columns() -> None:
    """Bốn trần khai inline trong loader phải soi gương đúng độ dài cột ORM.

    `ConfigPackage`/`ClosingAccountPair` khai độ dài các cột này bằng số inline
    (không có hằng số tên) — test này là sợi dây giữ loader và schema không
    trôi khỏi nhau khi một bên đổi.
    """
    from ket.kernel.config.accounts_models import ClosingAccountPair, ConfigPackage
    from ket.kernel.config.packages import loader

    package_columns = ConfigPackage.__table__.c
    assert loader._PACKAGE_CODE_MAX_LENGTH == package_columns.code.type.length
    assert loader._PACKAGE_DESCRIPTION_MAX_LENGTH == package_columns.description.type.length
    assert loader._PACKAGE_REFERENCE_MAX_LENGTH == package_columns.legal_reference.type.length
    assert (
        loader._CLOSING_DESCRIPTION_MAX_LENGTH
        == ClosingAccountPair.__table__.c.description.type.length
    )


# ----------------------------------------------------- auto_posting_rules.csv


_RULES_HEADER = (
    "document_type,operation_code,operation_name,debit_purpose,credit_purpose,"
    "requires_partner,partner_kind,display_order\n"
)


def _texts_with_rules(rules_body: str) -> dict[str, str]:
    from ket.kernel.config.packages.loader import AUTO_POSTING_RULES_FILE

    return _valid_texts(**{AUTO_POSTING_RULES_FILE: _RULES_HEADER + rules_body})


def test_auto_posting_rules_parse_with_open_sides_and_partner_hint() -> None:
    """Ô purpose trống = cố ý để ngỏ (`None`); `partner_kind` trống = không gợi ý."""
    loaded = load_package_from_texts(
        _texts_with_rules(
            "PT,thu-khac,Thu khác,cash,,0,,7\n"
            "PT,thu-no-khach-hang,Khách hàng trả nợ,cash,cash,1,0,3\n"
        )
    )
    open_sided = loaded.auto_posting_rules[0]
    assert open_sided.debit_purpose == "cash"
    assert open_sided.credit_purpose is None
    assert open_sided.partner_kind is None
    assert loaded.auto_posting_rules[1].requires_partner is True
    assert loaded.auto_posting_rules[1].partner_kind == 0


def test_auto_posting_rule_with_unknown_purpose_is_rejected() -> None:
    """Purpose lạ bị chặn lúc nạp — gõ nhầm không được giả dạng "để ngỏ"."""
    with pytest.raises(ConfigPackageDataInvalidError, match="purpose"):
        load_package_from_texts(_texts_with_rules("PT,thu-khac,Thu khác,cash,khong_ton_tai,0,,1\n"))


def test_duplicate_auto_posting_operation_is_rejected() -> None:
    with pytest.raises(ConfigPackageDataInvalidError, match="khai trùng"):
        load_package_from_texts(
            _texts_with_rules(
                "PT,thu-khac,Thu khác,,cash,0,,1\nPT,thu-khac,Thu khác bản hai,,cash,0,,2\n"
            )
        )


def test_auto_posting_rule_with_bad_partner_kind_is_rejected() -> None:
    with pytest.raises(ConfigPackageDataInvalidError, match="partner_kind"):
        load_package_from_texts(_texts_with_rules("PT,thu-khac,Thu khác,,cash,0,9,1\n"))


def test_a_package_without_the_rules_file_is_still_valid() -> None:
    """`auto_posting_rules.csv` là tệp tùy chọn — gói nhập ngoài chỉ mang hệ
    thống TK vẫn hợp lệ, danh sách nghiệp vụ khi đó rỗng."""
    loaded = load_package_from_texts(_valid_texts())
    assert loaded.auto_posting_rules == ()


def test_a_deposit_account_without_the_bank_dimension_is_rejected() -> None:
    """Review 6G-1 M-8 — hai nửa của luật quy chủ phải khớp nhau.

    `bank/posting_mapper` GÁN chiều `bank_account` cho mọi dòng có số hiệu bắt
    đầu `112`, còn validator ghi sổ chỉ ĐÒI nó ở TK khai chuỗi ấy trong
    `accounts.csv`. Gói nào khai lệch thì sổ chi tiết tiền gửi thiếu im lặng
    đúng bằng phần lệch — không lỗi, không cảnh báo. Kiểm ở loader nên nó phủ
    cả gói người dùng nhập từ `.zip`.
    """
    accounts = (
        "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
        "detail_tracking,is_locked\n"
        "111,Tiền mặt,,,0,0,0,,1\n"
        "112,Tiền gửi ngân hàng,,,0,1,0,,1\n"
        "1121,Tiền gửi VND,,112,0,0,0,,0\n"
        "131,Phải thu của khách hàng,,,2,0,0,customer,1\n"
    )
    with pytest.raises(ConfigPackageDataInvalidError) as refused:
        load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: accounts}))
    assert "1121" in str(refused.value)


def test_a_summary_deposit_account_needs_no_dimension() -> None:
    """TK tổng hợp không hạch toán thẳng vào được, nên không đòi chiều — thiếu
    miễn trừ này thì mọi gói hợp lệ đều bị từ chối vì chính TK `112` cha."""
    accounts = (
        "code,name,name_en,parent_code,balance_nature,is_summary,is_foreign_currency,"
        "detail_tracking,is_locked\n"
        "111,Tiền mặt,,,0,0,0,,1\n"
        "112,Tiền gửi ngân hàng,,,0,1,0,,1\n"
        "1121,Tiền gửi VND,,112,0,0,0,bank_account,0\n"
        "131,Phải thu của khách hàng,,,2,0,0,customer,1\n"
    )
    loaded = load_package_from_texts(_valid_texts(**{ACCOUNTS_FILE: accounts}))
    assert [row.code for row in loaded.accounts] == ["111", "112", "1121", "131"]
