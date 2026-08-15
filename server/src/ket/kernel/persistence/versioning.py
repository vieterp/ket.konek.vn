"""Khóa lạc quan: một cột đếm, một câu `UPDATE … WHERE row_version = ?` (FR-NFR-005).

Tình huống thật, không phải giả định: hai kế toán viên cùng mở một chứng từ vào
buổi sáng đối chiếu công nợ. Người thứ nhất sửa số tiền rồi lưu; người thứ hai
sửa diễn giải rồi lưu **trên bản đã cũ** — và ghi đè luôn số tiền của người kia
mà cả hai đều không biết. Không có gì trong màn hình nói rằng điều đó vừa xảy
ra; sổ chỉ đơn giản mang một con số sai.

Chọn khóa **lạc quan** chứ không bi quan (`SELECT … FOR UPDATE` giữ suốt lúc
người dùng đang gõ): một bản ghi bị khóa từ lúc mở form tới lúc bấm Lưu là bản
ghi có thể bị khóa cả giờ đồng hồ vì ai đó đi ăn trưa với form đang mở. Khóa lạc
quan chỉ va chạm **lúc ghi**, và va chạm thì hiếm.

Cơ chế nằm ở **DB**, không ở tầng ứng dụng: SQLAlchemy đưa `row_version` vào mệnh
đề `WHERE` của mỗi `UPDATE` và đếm số dòng bị ảnh hưởng. Không dòng nào khớp
nghĩa là bản ghi đã đổi từ lúc đọc → `StaleDataError` → `409` (xem
`api/middleware/problem_details.py`). Một lần kiểm "if row_version == ..." ở
Python sẽ có một khe hở đúng bằng khoảng giữa lúc đọc và lúc ghi.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ket.kernel.errors import RowVersionConflictError

ROW_VERSION_COLUMN: Final[str] = "row_version"
"""Tên cột, lộ ra để `auditing/listener.py` loại nó khỏi nhật ký.

Số phiên bản là **cơ chế nội bộ**, không phải dữ liệu nghiệp vụ: một dòng nhật
ký "row_version: 3 → 4" không nói gì mà chính diff của các cột khác chưa nói."""


class RowVersioned:
    """Mixin thêm `row_version` và bật kiểm phiên bản của SQLAlchemy.

    Áp cho bảng mà **con người sửa qua nhiều request** (danh mục, chứng từ, tùy
    chọn). Không áp cho bảng do cơ chế nội bộ đổi (`jobs`, `number_sequences`,
    `idempotency_keys`): ở đó tranh chấp đã được giải bằng khóa dòng hoặc bằng
    ràng buộc duy nhất, và thêm một tầng nữa chỉ tạo ra lỗi `409` mà không ai
    biết phải làm gì với nó.

    Bảng nào có mixin này thì **mọi** đường ghi qua ORM đều được canh — kể cả
    đường mà phase sau viết và quên nghĩ tới tranh chấp. Đó là lý do nó là mixin
    chứ không phải một lời gọi hàm mà service phải nhớ.
    """

    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    """Tăng 1 sau mỗi `UPDATE` thành công. Client gửi lại giá trị đã đọc; lệch
    nghĩa là có người khác đã ghi trong lúc form đang mở."""

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:  # noqa: N805 - hợp đồng của declared_attr
        """Khai cột phiên bản cho mapper của **từng lớp** kế thừa mixin.

        `declared_attr.directive` chứ không phải một `dict` hằng dùng chung: mỗi
        lớp có `Column` riêng (mixin sao chép cột cho từng bảng), nên một `dict`
        chung sẽ trỏ mọi mapper vào cột của lớp được ánh xạ đầu tiên.
        """
        return {"version_id_col": cls.row_version}


def require_row_version(
    *,
    current: int,
    expected: int,
    entity: str,
    latest: dict[str, Any] | None = None,
) -> None:
    """Chặn sớm khi client gửi lên một phiên bản đã cũ.

    **Không thay thế** cơ chế của SQLAlchemy, mà đứng trước nó. Hai lớp vì
    chúng bắt hai tình huống khác nhau:

    * Lớp này bắt ca thường gặp — form mở từ mười phút trước, người khác đã lưu
      trong lúc đó. Bắt tại đây thì trả được **bản mới nhất** kèm theo, vì
      transaction còn sạch và bản ghi vừa đọc xong đang trong tay.
    * `version_id_col` bắt ca hiếm — hai request cùng đọc một phiên bản rồi
      cùng ghi. Ở đó `UPDATE … WHERE row_version = ?` của PostgreSQL là thứ duy
      nhất phân xử được, và nó ném `StaleDataError` (xem handler ở
      `api/middleware/problem_details.py`).

    Bỏ lớp này thì vẫn đúng nhưng thông điệp nghèo đi; bỏ lớp kia thì có khe hở
    thật.
    """
    if current != expected:
        raise RowVersionConflictError(
            "Bản ghi đã được người khác sửa — hãy xem lại thay đổi mới nhất rồi lưu lại",
            latest=latest,
            entity=entity,
            expected=expected,
            current=current,
        )
