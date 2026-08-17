"""Bộ đếm số chứng từ và sổ cấp số — thuật toán ở `service.py` (RT-12).

Kế toán VN có hai loại số với hai yêu cầu khác hẳn nhau:

* **Liên tục, không đứt quãng** (hóa đơn): mất số phải giải trình. Cấp bằng
  `SELECT … FOR UPDATE` trên đúng dòng bộ đếm, **trong** transaction ghi chứng
  từ — nối tiếp nhau, chậm hơn, nhưng không thủng dãy.
* **Nội bộ** (phiếu thu, phiếu nhập): đứt quãng chấp nhận được, ưu tiên không
  chặn nhau khi nhiều người cùng nhập.

`allow_gaps` khai loại nào là loại nào ngay trên dữ liệu, để phase 3 không phải
đoán và để người triển khai đổi được mà không sửa code (FR-NFR-055).

Bộ đếm nằm **trong schema dataset**, nên hai doanh nghiệp đánh số độc lập —
một hệ quả trực tiếp của ADR-017.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import DatasetBase


class ResetRule(StrEnum):
    """Khi nào bộ đếm quay về 1 (FR-SYS-063).

    Không phải cột điều khiển một nhánh `if` trong lúc cấp số: chu kỳ reset nằm
    **trong** `scope_key` (`PC|BRANCH:3|2026-08`), nên mỗi chu kỳ là một dòng bộ
    đếm riêng và việc "quay về 1" xảy ra một cách tự nhiên khi khóa phạm vi đổi.
    Cột này giữ **ý định** để dựng được khóa đó và để màn hình thiết lập nói ra
    quy tắc đang áp dụng.
    """

    NEVER = "never"

    YEARLY = "yearly"
    """Cắt theo **năm dương lịch**, không theo niên độ kế toán — chốt có chủ đích.

    Doanh nghiệp niên độ lệch (01/04–31/03, thường là FDI) vì thế sẽ thấy số
    chứng từ quay về 1 vào 01/01, tức là **giữa** niên độ. Đó là điều đúng: số
    chứng từ và số hóa đơn ở Việt Nam đánh theo năm dương lịch, và mẫu hóa đơn
    đã đăng ký cũng mang năm dương lịch. Gắn nó vào niên độ sẽ bắt dịch vụ cấp
    số phải biết kỳ kế toán — một phụ thuộc ngược chiều, và đổi lấy một cách
    đánh số mà cơ quan thuế không mong đợi.

    Ghi ra đây vì đây đúng loại quyết định sẽ bị ai đó "sửa" nhầm chiều khi thấy
    `PeriodService` có hỗ trợ niên độ lệch."""

    MONTHLY = "monthly"


def _sql_values(enum_type: type[StrEnum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class NumberSequence(DatasetBase):
    """Một dãy số: khóa phạm vi → giá trị kế tiếp.

    Cố ý **không** `Audited`: mỗi lần cấp số là một `UPDATE` trên đường nóng
    nhất của hệ (trong chính transaction ghi chứng từ), nên ghi vết nó vừa làm
    chậm vừa nhấn chìm những dòng nhật ký mà kiểm toán viên thật sự cần đọc.
    Dấu vết của số đã cấp nằm ở **chứng từ** mang số đó; việc con người đổi
    *cấu hình* dãy số sẽ được ghi ở tầng dịch vụ (phase 3) bằng
    `record_action`, chứ không phải mỗi lần bộ đếm nhích lên.
    """

    __tablename__ = "number_sequences"
    __table_args__ = (
        CheckConstraint(f"reset_rule IN ({_sql_values(ResetRule)})", name="reset_rule_known"),
        CheckConstraint("padding BETWEEN 1 AND 12", name="padding_in_range"),
        CheckConstraint("next_value >= 1", name="next_value_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    scope_key: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    """Phạm vi dãy số, ví dụ `cash_receipt/2026/HN`. Chi nhánh và năm nằm
    **trong** khóa chứ không thành cột riêng: quy tắc "đánh số theo gì" là cấu
    hình của người dùng, và mỗi lần nó đổi mà phải `ALTER TABLE` thì đã vi phạm
    "cấu hình thay vì sửa code"."""

    prefix: Mapped[str] = mapped_column(String(50), nullable=False, default="", server_default="")
    padding: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    next_value: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )

    allow_gaps: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    """`False` = số phải liên tục (hóa đơn) → cấp số nối tiếp nhau trong
    transaction. `True` = cho phép đứt quãng (chứng từ nội bộ)."""

    document_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="", server_default=""
    )
    """Loại chứng từ dùng dãy này — cùng mã với registry loại chứng từ của RBAC
    (`{module}.{chứng từ}.{hành vi}` ở lát 2B-1b). Là **dữ liệu**, không phải
    khóa tra cứu: khóa tra cứu là `scope_key`, vì cùng một loại chứng từ có
    nhiều dãy (mỗi chi nhánh, mỗi tháng) chạy song song."""

    suffix: Mapped[str] = mapped_column(String(50), nullable=False, default="", server_default="")

    reset_rule: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ResetRule.NEVER.value, server_default=ResetRule.NEVER
    )


class AllocatedNumber(DatasetBase):
    """Sổ cấp số: số nào đã cấp cho chứng từ nào (RT-10, BR-EIV-02).

    Chỉ ghi cho dãy **liên tục** (hóa đơn). Với số nội bộ thì dòng này là chi
    phí thuần trên đường nóng nhất của hệ, còn với hóa đơn nó là thứ phải trình
    ra: cơ quan thuế hỏi "số 0000123 đâu rồi" thì câu trả lời hợp lệ là "cấp cho
    chứng từ này, sau đó hủy, đây là biên bản hủy" — chứ không phải một khoảng
    trống trong dãy mà không ai giải thích được.

    `UNIQUE (scope_key, number)` là **hàng rào cuối**: cơ chế cấp số đã nối tiếp
    bằng khóa dòng, nên ràng buộc này lẽ ra không bao giờ chạm tới. Nó tồn tại
    cho đúng cái ngày một đường ghi mới ở phase sau quên đi qua dịch vụ — và khi
    đó thà một lỗi ghi thất bại còn hơn hai hóa đơn cùng số.

    Không `Audited`: mỗi lần cấp số là một dòng ở đây, và bản thân dòng này đã
    là dấu vết. Ghi vết cho một bảng chỉ-thêm là ghi vết hai lần cùng một sự việc.
    """

    __tablename__ = "allocated_numbers"
    __table_args__ = (
        Index("uq_allocated_numbers_scope_number", "scope_key", "number", unique=True),
        Index("ix_allocated_numbers_document_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(150), nullable=False)
    number: Mapped[str] = mapped_column(String(100), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    """`uid` của chứng từ, không phải khóa `int`: chứng từ chưa tồn tại lúc cấp
    số (số nằm **trên** chứng từ), nên khóa phải là thứ người gọi sinh được
    trước khi ghi — đúng vai trò của `uid` UUIDv7 (RT-19)."""

    allocated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
