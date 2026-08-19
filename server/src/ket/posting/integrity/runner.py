"""Chạy các phép kiểm toàn vẹn và gom báo cáo chênh lệch (FR-NFR-007).

Mỗi check là một câu SELECT trong tệp `.sql` (LD-14 — phép so khối lượng lớn
nằm trọn trong PostgreSQL); Python ở đây chỉ bọc câu lệnh, đếm tổng, cắt mẫu
và chuẩn hóa giá trị về nguyên thủy JSON. Báo cáo **chỉ ra** chênh lệch kèm
đường lần về chứng từ — không sửa gì (phase-04 §Bộ kiểm tra toàn vẹn).

Hai câu cho một check (COUNT + LIMIT) thay vì đọc hết rồi đếm ở Python: một
check đổ lỗi trên dữ liệu hỏng nặng có thể trả hàng trăm nghìn dòng, và kết
quả job là một cột JSONB — nó cần con số tổng thật và một MẪU đủ lần vết, chứ
không cần chở cả bảng chênh lệch về.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ket.kernel.persistence.types import JsonPrimitive
from ket.posting.integrity.checks.registry import CHECKS, IntegrityCheck, check_of

SAMPLE_LIMIT: Final[int] = 50
"""Số dòng chênh nêu đích danh cho mỗi check — đủ để lần vết, không biến
`jobs.result` thành một bản sao của bảng lỗi."""


class CheckOutcome(BaseModel):
    """Kết quả một phép kiểm — `total = 0` nghĩa là xanh."""

    code: str
    title: str
    rule: str
    total: int
    sample: list[dict[str, JsonPrimitive]]


class IntegrityReport(BaseModel):
    branch_id: int
    checks: list[CheckOutcome]

    @property
    def total_discrepancies(self) -> int:
        return sum(outcome.total for outcome in self.checks)


def _primitive(value: object) -> JsonPrimitive:
    """Giá trị SQL → nguyên thủy JSON, cùng khuôn với nhật ký (`AuditValues`).

    `Decimal` thành chuỗi chứ không `float`: báo cáo chênh lệch mà bản thân
    con số lệch vì nhị phân hóa là tự phủ định mình.
    """
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def run_check(
    session: Session,
    check: IntegrityCheck,
    *,
    branch_id: int,
    sample_limit: int = SAMPLE_LIMIT,
) -> CheckOutcome:
    # Câu check đến từ tệp `.sql` ĐÓNG GÓI cùng mã nguồn (registry đọc bằng
    # `importlib.resources`) — cùng hạng tin cậy với chính chuỗi bọc ở đây,
    # không phải dữ liệu ngoài. Giá trị động duy nhất (`branch_id`,
    # `sample_limit`) đi qua tham số ràng buộc, đúng luật B1.
    sql = check.sql()
    count_statement = "SELECT count(*) FROM (\n" + sql + "\n) AS discrepancies"  # noqa: S608 — sql là tệp đóng gói, không phải dữ liệu ngoài
    sample_statement = (
        "SELECT * FROM (\n" + sql + "\n) AS discrepancies LIMIT :integrity_sample_limit"  # noqa: S608 — như trên
    )
    # Check không nhắc tới `:branch_id` (vd `usage_counter_accurate` — bộ đếm
    # danh mục không thuộc chi nhánh nào) thì không nhận tham số đó: TextClause
    # không khai tham số này nên đưa vào là lỗi, không phải được lờ đi.
    params: dict[str, int] = {"branch_id": branch_id} if ":branch_id" in sql else {}
    total = session.execute(text(count_statement), params).scalar_one()
    sample: list[dict[str, JsonPrimitive]] = []
    if total:
        rows = session.execute(
            text(sample_statement), {**params, "integrity_sample_limit": sample_limit}
        ).mappings()
        sample = [{key: _primitive(value) for key, value in row.items()} for row in rows]
    return CheckOutcome(
        code=check.code, title=check.title, rule=check.rule, total=total, sample=sample
    )


def resolve_checks(codes: list[str] | None) -> tuple[IntegrityCheck, ...]:
    """Danh sách check theo yêu cầu; không nêu = chạy đủ bộ."""
    if not codes:
        return CHECKS
    return tuple(check_of(code) for code in codes)
