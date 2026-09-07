"""Danh mục các phép kiểm toàn vẹn (FR-NFR-007, FR-GLE-032/033) — 7 của
phase 4, đối chiếu sổ quỹ thủ quỹ của phase 6 (BR-WHK-03), và đối chiếu số đã
đối trừ với dòng đối trừ của lát 7A (BR-QUY-02).

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

Thư mục này có MỘT tệp `.sql` **không** nằm trong `CHECKS`:
`opening_detail_matches_control.sql`. Nó đứng trong registry từ 4C và đỏ trên
dữ liệu ĐÚNG suốt từ đó — lát 7C-5 phát hiện khi đi đóng điều kiện cuối của
`arap_matches_control` và **gỡ nó ra** (quyết định user 2026-09-06); đầu tệp
ghi đủ hai nguyên nhân và điều kiện đăng ký lại.

Chiều ngược lại xảy ra cùng lát: `arap_matches_control.sql` đứng ngoài registry
suốt năm lát (7A → 7C-4) rồi **vào** ở 7C-5, sau khi chín điều kiện đóng hết.

Một luật cho cả hai chiều, và cho mọi check sau: **viết tệp trước, đăng ký sau,
và chỉ đăng ký khi nó xanh trên dữ liệu đúng** — một check kêu sai dạy người
dùng bỏ qua mọi check còn lại. Luật ấy có hiệu lực **cả sau khi đã đăng ký**:
tìm ra một ca đỏ oan là lý do đủ để gỡ một check đang chạy.
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
        code="usage_counter_accurate",
        title="Bộ đếm sử dụng danh mục khớp tham chiếu thực tế",
        rule="BR-SYS-02",
    ),
    IntegrityCheck(
        code="treasurer_book_matches_ledger",
        title="Sổ quỹ thủ quỹ khớp sổ kế toán TK tiền mặt",
        rule="BR-WHK-03",
    ),
    IntegrityCheck(
        code="settlement_matches_subledger",
        title="Số đã đối trừ trên sổ phụ khớp dòng đối trừ của chứng từ đã ghi sổ",
        rule="BR-QUY-02",
    ),
    IntegrityCheck(
        code="arap_matches_control",
        title="Sổ phụ công nợ khớp số dư tài khoản công nợ trên sổ cái",
        rule="BR-GLE-05",
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
