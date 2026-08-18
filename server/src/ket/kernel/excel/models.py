"""Bảng đệm của một lượt nhập liệu — dòng thô, chưa phân giải gì (H82, H83).

`COPY` đổ nguyên văn từng ô vào đây rồi mọi phép kiểm chạy bằng SQL tập hợp trên
chính bảng này: bắt buộc, độ dài, ép kiểu, và **tra mã thành khóa ngoại bằng
`JOIN`**. Đó là đọc đúng LD-14 ("tính khối lượng lớn phải set-based SQL trong
PostgreSQL; Python chỉ điều phối") ở đúng chỗ nó có ý nghĩa nhất — 10.000 dòng
của FR-NFR-043.

**Vì sao không phải `CREATE TEMP TABLE` (H83).** `security/roles.sql` thu hồi có
chủ đích quyền `TEMPORARY` của mọi vai trò runtime, và lý do được ghi ngay tại
đó: `pg_temp` đứng trước mọi schema khác trong đường tra, nên một
`CREATE TEMP TABLE audit_log (…)` khiến mọi dòng nhật ký tiếp theo rơi vào bảng
tạm rồi biến mất lúc phiên đóng — nhật ký bất biến bị vô hiệu hóa mà không sửa
hay xóa dòng nào (RT-02). Cấp lại quyền ấy cho tiện nhập liệu là bán đúng bất
biến đó. Một bảng **có tên** trong schema dataset thì không che được bảng nào.

**`UNLOGGED`** vì nội dung dựng lại được từ tệp đã lưu trong kho định địa chỉ
theo nội dung: nó không đáng nằm trong WAL, không đáng đi vào bản sao lưu, và
mất sạch sau một lần máy chủ đổ là kết quả đúng. Khai ở migration
(`postgresql_unlogged`), không ở đây, vì đó là thuộc tính của bảng vật lý.

Dòng ở đây **không** có `branch_id`: một lượt nhập thuộc về đúng một job, và job
đã mang chi nhánh của người bấm nút. Thêm cột chi nhánh trên từng dòng là mở lại
đúng câu hỏi mà `_COMMON_COLUMNS` đã đóng ở `descriptors.py`.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.types import AuditValues


class ImportStagingRow(DatasetBase):
    """Một dòng thô của tệp nhập liệu, giữ nguyên văn như người dùng gõ.

    Cố ý **không** `Audited` và **không** `RowVersioned`: đây là dữ liệu trung
    gian sống trong một lượt chạy job, không phải một bản ghi ai đó sửa. Ghi vết
    nó sẽ đúng bằng 10.000 dòng nhật ký cho mỗi lần bấm "Kiểm tra dữ liệu" —
    tiếng ồn che mất đúng thứ H84 muốn giữ lại.
    """

    __tablename__ = "import_staging_rows"
    __table_args__ = (
        # Mọi truy vấn của một lượt nhập lọc theo `job_id` rồi sắp theo số dòng
        # (báo cáo lỗi phải theo thứ tự người dùng thấy trong Excel). Một index
        # gộp phục vụ cả hai; `row_number` đứng sau vì nó không bao giờ là điều
        # kiện lọc đứng một mình.
        Index("ix_import_staging_rows_job", "job_id", "row_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    job_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    """`CASCADE` chứ không `RESTRICT`, khác quy ước của danh mục: dòng đệm không
    có giá trị gì khi job của nó biến mất, và một khóa ngoại `RESTRICT` ở đây sẽ
    biến việc dọn hàng đợi thành việc phải dọn hai bảng theo đúng thứ tự."""

    uid: Mapped[UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    """UUIDv7 cấp sẵn cho bản ghi mà dòng này sẽ tạo ra (RT-19).

    Sinh ở tầng Python lúc nạp, không bằng một hàm SQL trong câu `INSERT` của
    bước ghi: PostgreSQL 16 chỉ có `gen_random_uuid()`, và nó cho ra **v4** ngẫu
    nhiên đều — chạy trót lọt, không lỗi, và mất đúng tính tăng dần theo thời
    gian mà cột `uid` của danh mục tồn tại vì nó. Cấp sẵn ở đây cũng khiến giá
    trị ổn định qua một lần chạy lại: job hỏng rồi reaper cho chạy lại thì dòng
    đệm vẫn mang đúng `uid` cũ."""

    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    """Số dòng **trong tệp Excel như người dùng nhìn thấy** (dòng tiêu đề là 1).

    Không phải chỉ số 0-based của vòng lặp: FR-SYS-081 đòi "hiển thị nguyên nhân
    lỗi theo từng dòng", và một con số lệch 1 so với thanh dòng của Excel biến
    mỗi lần sửa lỗi thành một lần đếm tay."""

    cells: Mapped[AuditValues] = mapped_column(JSONB, nullable=False)
    """Ô của dòng, khóa theo `ColumnDescriptor.field`, giá trị là **chuỗi thô**.

    Tên `cells` chứ không `values`: `values` trùng tên với phương thức
    `ColumnCollection.values()` của SQLAlchemy, nên `table.c.values` ở tầng Core
    trả về **phương thức** chứ không phải cột — một lỗi chỉ lộ ra ở truy vấn nào
    dùng bí danh bảng, và lộ ra dưới dạng một thông báo kiểu khó lần.

    JSONB chứ không một bảng có cột thật cho từng trường: hai mươi danh mục có
    hai mươi bộ cột khác nhau, và một bảng đệm cho mỗi danh mục là hai mươi
    migration cho một việc trung gian. Ép kiểu diễn ra ở câu SQL kiểm dữ liệu,
    nơi lỗi ép kiểu **là** thứ cần báo cho người dùng chứ không phải thứ cần
    tránh.

    Chuỗi thô chứ không giá trị đã ép: người dùng gõ `1.234,50` hay `x` hay một
    ngày Excel tự đổi định dạng, và bản ghi lại nguyên văn là điều kiện để câu
    thông báo lỗi trích đúng thứ họ đã gõ."""
