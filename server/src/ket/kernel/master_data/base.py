"""Khung chung cho ~20 danh mục (`docs/srs/01` §3.1, FR-SYS-010..018).

Mọi danh mục kế toán trả lời cùng một bộ câu hỏi: mã là gì, tên tiếng Việt và
tiếng Anh là gì, nằm ở nhánh nào của cây, còn dùng nữa hay đã ngừng theo dõi,
dùng chung toàn công ty hay riêng một chi nhánh. Viết lại bộ đó hai mươi lần là
hai mươi cơ hội để một danh mục quên mất một luật — và luật hay bị quên nhất
chính là luật khó thấy nhất (mã duy nhất **theo phạm vi**, xem
`master_data_table_args`).

Vì sao là **một lớp gốc dùng chung** chứ không một bảng `master_data` gộp với
cột `type`: danh mục có ràng buộc khóa ngoại thật từ bảng chứng từ (`item_id`,
`partner_id`), và một bảng gộp biến mọi khóa ngoại đó thành "trỏ vào bảng chung,
tin rằng đúng loại". PostgreSQL kiểm được cái thứ nhất; cái thứ hai chỉ có
review kiểm.

Cột `branch_id` ở đây **không** bật RLS. Đó là điểm khác then chốt so với bảng
phát sinh: danh mục dùng chung mang `branch_id IS NULL` và phải nhìn thấy được
từ **mọi** chi nhánh, trong khi policy chi nhánh sẽ giấu đúng những dòng đó.
Ai được xem/sửa danh mục là câu hỏi của RBAC (`permissions`), không phải của cô
lập dòng — cùng lập luận đã áp cho `branches` ở migration `0001`.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column
from sqlalchemy.schema import SchemaItem

from ket.kernel.auditing.listener import Audited
from ket.kernel.identifiers import uuid7
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

CODE_MAX_LENGTH = 50
NAME_MAX_LENGTH = 255
PATH_MAX_LENGTH = 255
"""Đủ cho hơn 25 cấp id 9 chữ số — sâu hơn nhiều lần mức 5 cấp mà FR-SYS-011
đòi, và vẫn là `varchar` có trần để chỉ mục không phình vô hạn."""


class MasterDataRow(DatasetBase, Audited, RowVersioned):
    """Cột dùng chung của mọi danh mục — lớp gốc trừu tượng, không tạo bảng riêng.

    Kèm luôn `Audited` và `RowVersioned` chứ không để từng danh mục tự khai:
    "ai đổi mã khách hàng này" và "hai người cùng sửa một mã hàng" là câu hỏi
    đúng cho **mọi** danh mục, và thứ khai theo tùy chọn là thứ danh mục thứ hai
    mươi sẽ quên khai.

    Là **lớp gốc** (`__abstract__`) chứ không phải mixin thuần như phase-03 phác:
    `declared_attr` bên dưới đọc `cls.__tablename__`, và `MasterDataService` cần
    một kiểu để ràng buộc tham số kiểu vào. Một mixin không kế thừa
    `DeclarativeBase` thì cả hai chỗ đó chỉ còn `Any` — tức mypy strict (LD-13)
    không kiểm được gì trên đúng đoạn mã dùng chung cho hai mươi danh mục.
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    uid: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True, default=uuid7)
    """Định danh ổn định để nối nhiều bản cài về sau (RT-19) — xem
    `kernel/identifiers.py`. Sinh ở tầng Python chứ không `server_default`:
    PostgreSQL 16 không có sẵn hàm UUIDv7, và một `DEFAULT gen_random_uuid()`
    sẽ âm thầm cho ra v4 (ngẫu nhiên đều) — mất đúng tính chất tăng dần mà cột
    này được chọn vì nó."""

    code: Mapped[str] = mapped_column(String(CODE_MAX_LENGTH), nullable=False)
    """Mã người dùng đặt và đọc. Duy nhất **theo phạm vi**, không phải toàn
    bảng — xem `master_data_table_args`."""

    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)
    """FR-NFR-034: giao diện song ngữ đọc cột này, không dịch tên do người dùng nhập."""

    path: Mapped[str] = mapped_column(String(PATH_MAX_LENGTH), nullable=False)
    level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=ROOT_LEVEL, server_default=str(ROOT_LEVEL)
    )

    is_group: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Nút nhóm chỉ để gom, **không** được hạch toán vào. Cột riêng chứ không
    suy ra từ "có con hay không": một nhóm vừa tạo thì chưa có con nào, và suy
    kiểu đó sẽ cho hạch toán vào nó đúng trong khoảng thời gian nó còn rỗng."""

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    """FR-SYS-012 "Ngừng theo dõi" — thay cho xóa. Danh mục đã xuất hiện trên
    chứng từ của năm trước không được biến mất khỏi sổ năm đó, nên xóa thật chỉ
    dành cho bản ghi chưa ai dùng (xem `usage.py`)."""

    @declared_attr
    def parent_id(cls) -> Mapped[int | None]:  # noqa: N805 - hợp đồng của declared_attr
        """Cha trong cây; `NULL` = nút gốc.

        `RESTRICT` chứ không `CASCADE`: xóa một nhóm mà kéo theo cả nhánh con là
        cách mất một phần danh mục chỉ bằng một cú bấm nhầm. Muốn xóa nhóm thì
        phải chuyển con đi trước — và chính lúc chuyển đó người dùng nhìn thấy
        mình đang có bao nhiêu thứ trong tay.
        """
        return mapped_column(
            ForeignKey(f"{cls.__tablename__}.id", ondelete="RESTRICT"), nullable=True
        )

    @declared_attr
    def branch_id(cls) -> Mapped[int | None]:  # noqa: N805 - hợp đồng của declared_attr
        """`NULL` = dùng chung toàn công ty; có giá trị = riêng chi nhánh đó (FR-SYS-018)."""
        return mapped_column(
            ForeignKey("branches.id", ondelete="RESTRICT"), nullable=True, index=True
        )


def master_data_table_args(table_name: str) -> tuple[SchemaItem, ...]:
    """Ràng buộc và chỉ mục của một bảng danh mục.

    Là hàm chứ không `__table_args__` trên lớp gốc vì tên chỉ mục phải mang tên
    bảng — hai bảng cùng tên chỉ mục thì bảng thứ hai không tạo được.

    **Hai** chỉ mục duy nhất cho `code`, không phải một (BR-SYS-01). PostgreSQL
    coi mỗi `NULL` là khác nhau, nên `UNIQUE (branch_id, code)` đơn thuần cho
    phép tạo vô số danh mục dùng chung trùng mã — đúng thứ ràng buộc đó sinh ra
    để chặn. Tách đôi theo điều kiện:

    * `branch_id IS NULL` → mã duy nhất trong phạm vi "dùng chung".
    * `branch_id IS NOT NULL` → mã duy nhất trong phạm vi từng chi nhánh.

    Hệ quả có chủ đích: chi nhánh Hà Nội **được** đặt mã `VT001` riêng trong khi
    công ty cũng có `VT001` dùng chung. Đó là điều FR-SYS-018 mô tả; chỗ phân xử
    "dùng cái nào" là lúc tra cứu (`service.resolve_by_code`), không phải lúc ghi.
    """
    return (
        CheckConstraint(f"path ~ '{PATH_PATTERN}'", name="path_is_dotted_ids"),
        CheckConstraint(f"level >= {ROOT_LEVEL}", name="level_at_least_root"),
        CheckConstraint("code <> ''", name="code_not_blank"),
        Index(
            f"uq_{table_name}_shared_code",
            "code",
            unique=True,
            postgresql_where=text("branch_id IS NULL"),
        ),
        Index(
            f"uq_{table_name}_branch_code",
            "branch_id",
            "code",
            unique=True,
            postgresql_where=text("branch_id IS NOT NULL"),
        ),
        Index(f"ix_{table_name}_path", "path"),
        Index(f"ix_{table_name}_parent_id", "parent_id"),
    )
