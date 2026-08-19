"""Danh mục 7 phép kiểm toàn vẹn của phase 4 (FR-NFR-007, FR-GLE-032/033).

Mỗi phép kiểm là MỘT tệp `.sql` độc lập trả về danh sách dòng chênh lệch —
không UPDATE, không tự sửa (chữ của phase file: "chỉ chỉ ra"). Registry là
dữ liệu chứ không phải chuỗi `if` trong runner: phase 8 thêm đối chiếu
kho ↔ sổ cái là thêm một tệp `.sql` + một dòng ở đây, và người review nhìn
thấy đúng hai thứ đó trong diff.

Hợp đồng của một tệp check:

* chỉ SELECT; tham số duy nhất được cấp là `:branch_id` (dùng hay không tùy
  check — RLS đã lọc chi nhánh, tham số là phòng thủ lớp hai);
* không `ORDER BY` ở mức ngoài cùng — runner bọc câu lệnh vào
  `SELECT count(*)` và `LIMIT` nên thứ tự không có hợp đồng;
* cột trả về tự mô tả được dòng chênh (khóa + hai vế lệch), và dòng nào lần
  về chứng từ được thì mang `voucher_id` — U11 đòi mỗi lỗi dẫn tới chỗ sửa.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Final

from ket.kernel.errors import IntegrityCheckUnknownError


@dataclass(frozen=True)
class IntegrityCheck:
    """Một phép kiểm: mã ổn định (đi vào API + kết quả job) và tệp SQL."""

    code: str
    title: str
    rule: str
    """Mã FR/BR mà phép kiểm canh — hiện trên báo cáo chênh lệch."""

    def sql(self) -> str:
        return (
            resources.files("ket.posting.integrity.checks")
            .joinpath(f"{self.code}.sql")
            .read_text("utf-8")
        )


CHECKS: Final[tuple[IntegrityCheck, ...]] = (
    IntegrityCheck(
        code="ledger_balanced",
        title="Mỗi chứng từ cân Nợ/Có trên từng sổ",
        rule="BR-GLE-01",
    ),
    IntegrityCheck(
        code="trial_balance_balanced",
        title="Bảng cân đối tài khoản cân cả ba cột",
        rule="BR-GLE-02",
    ),
    IntegrityCheck(
        code="snapshot_matches_postings",
        title="Snapshot số dư khớp tổng hợp lại từ sổ cái",
        rule="ADR-011",
    ),
    IntegrityCheck(
        code="detail_matches_control",
        title="Dòng phát sinh đủ chiều chi tiết mà tài khoản đòi",
        rule="BR-GLE-05",
    ),
    IntegrityCheck(
        code="opening_balance_balanced",
        title="Số dư ban đầu cân Nợ/Có",
        rule="BR-OPB-01",
    ),
    IntegrityCheck(
        code="opening_detail_matches_control",
        title="Chi tiết hóa đơn đầu kỳ khớp dòng số dư cha",
        rule="BR-OPB-02",
    ),
    IntegrityCheck(
        code="usage_counter_accurate",
        title="Bộ đếm sử dụng danh mục khớp tham chiếu thực tế",
        rule="BR-SYS-02",
    ),
)

_BY_CODE: Final[dict[str, IntegrityCheck]] = {check.code: check for check in CHECKS}


def check_of(code: str) -> IntegrityCheck:
    check = _BY_CODE.get(code)
    if check is None:
        raise IntegrityCheckUnknownError("Không có phép kiểm toàn vẹn nào mang mã này", check=code)
    return check


def codes() -> tuple[str, ...]:
    return tuple(check.code for check in CHECKS)
