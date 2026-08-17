"""Chiều phân tích **mở rộng** (LD-08, FR-SYS-051, rủi ro SRS 19 §9 #5).

Hệ thống có hai loại chiều, và ranh giới giữa chúng là một quyết định kiến trúc
đã chốt (LD-08), không phải một chi tiết cài đặt:

* **Sáu chiều lõi** — chi nhánh, đối tượng THCP, công trình, đơn hàng, hợp đồng,
  khoản mục chi phí — là **cột cố định** trên dòng phát sinh (phase 4). Chúng
  xuất hiện trên hầu hết báo cáo và cần chỉ mục riêng; một mô hình EAV cho chúng
  sẽ chậm ở đúng chỗ hay dùng nhất.
* **Chiều mở rộng** — khai lúc chạy, giá trị nối vào dòng phát sinh qua bảng
  trung gian ở phase 4. Đặc thù ngành thì hiếm và ít bản ghi, nên EAV chấp nhận
  được. Đổi lại, thêm một chiều **không** phải sửa mã và không phải migrate.

"Mã thống kê" (FR-SYS-051) là một chiều mở rộng **dựng sẵn**, không phải một cột
riêng. Nó được gieo lúc cấp dữ liệu kế toán (xem `seed.py`) — đó cũng chính là
bằng chứng cho tiêu chí "khai bằng cấu hình, không sửa code": nếu chiều dựng sẵn
duy nhất mà cũng chỉ là một dòng dữ liệu, thì chiều thứ hai cũng vậy.

**Phạm vi v1 (RT-20):** bảng, dịch vụ và phần gieo là MUST. Hoãn tới v1.1: (a)
màn hình cho người dùng cuối tự khai chiều, (b) ép `applies_to_accounts`. Cột
`applies_to_accounts` **vẫn khai ở đây** và vẫn ghi được — hoãn là hoãn phần
*ép*, không phải phần *lưu*: bật một validator sau thì rẻ, còn thêm một cột vào
bảng đã có dữ liệu thật thì không.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.master_data.tree_path import PATH_PATTERN, ROOT_LEVEL
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.persistence.versioning import RowVersioned

ANALYSIS_DIMENSION_TABLE_NAME = "analysis_dimensions"
ANALYSIS_DIMENSION_VALUE_TABLE_NAME = "analysis_dimension_values"

CODE_MAX_LENGTH = 50
NAME_MAX_LENGTH = 255
PATH_MAX_LENGTH = 255
ACCOUNT_PREFIX_MAX_LENGTH = 20


class DimensionValueSource(StrEnum):
    """Giá trị của một chiều lấy từ đâu.

    `plan.md` phác nguồn dưới dạng một chuỗi ghép (`'master:partners'`). Ở đây
    tách thành **hai cột** (`value_source` + `master_slug`) vì một chuỗi ghép
    buộc mọi nơi đọc phải tự tách lấy phần sau dấu hai chấm, và chỗ thứ ba quên
    tách sẽ đi tìm một danh mục tên `"master:partners"`. Với hai cột, ràng buộc
    `CHECK` nói được điều mà một chuỗi không nói được: có `master` thì phải có
    slug, không có `master` thì không được có slug.
    """

    LIST = "list"
    """Giá trị là danh sách riêng của chiều — bảng `analysis_dimension_values`."""

    MASTER = "master"
    """Giá trị lấy từ một danh mục đã đăng ký; `master_slug` nói là danh mục nào."""


VALUE_SOURCE_MAX_LENGTH = 20

_VALUE_SOURCE_TYPE = Enum(
    DimensionValueSource,
    # `VARCHAR` chứ không kiểu `ENUM` của PostgreSQL: thêm một giá trị vào kiểu
    # native đòi `ALTER TYPE`, một lệnh không chạy được trong cùng transaction
    # với phần còn lại của migration ở nhiều phiên bản PostgreSQL.
    native_enum=False,
    length=VALUE_SOURCE_MAX_LENGTH,
    # Không sinh `CHECK`: bất biến thật của cặp cột đã nằm ở
    # `master_slug_iff_master_source`, và một `CHECK` liệt kê giá trị nữa sẽ là
    # chỗ thứ hai phải sửa mỗi lần thêm một nguồn giá trị.
    create_constraint=False,
    # Lưu **giá trị** (`"list"`) chứ không tên thành viên (`"LIST"`). Mặc định
    # của SQLAlchemy là tên, và nó sẽ lệch với `server_default` cũng như với mọi
    # câu SQL mà gói cấu hình phase 5 viết tay.
    values_callable=lambda enum_class: [member.value for member in enum_class],
)
"""Kiểu cột cho `value_source`.

Không dùng `String(20)` trần: cột khai `Mapped[DimensionValueSource]` nhưng
`String` trả về `str` thuần khi đọc lại, nên `dimension.value_source is
DimensionValueSource.LIST` là **sai** cho một dòng vừa đọc từ DB — trong khi
`==` vẫn đúng (đây là `StrEnum`). Một annotation nói dối kiểu đó khiến mypy
đồng ý với mọi phép so sánh `is`, và phép so sánh ấy im lặng cho kết quả sai
tùy theo bản ghi tới từ bộ nhớ hay từ DB."""


class AnalysisDimension(DatasetBase, Audited, RowVersioned):
    """Định nghĩa một chiều phân tích mở rộng."""

    __tablename__ = ANALYSIS_DIMENSION_TABLE_NAME
    __table_args__ = (
        CheckConstraint("code <> ''", name="code_not_blank"),
        # Bất biến của `DimensionValueSource`, đặt ở DB chứ không chỉ ở Python:
        # gói cấu hình phase 5 ghi thẳng bằng SQL, và một chiều `master` không
        # có slug sẽ là một ô chọn rỗng mà không ai truy được nguyên nhân.
        CheckConstraint(
            "(value_source = 'master') = (master_slug IS NOT NULL)",
            name="master_slug_iff_master_source",
        ),
        UniqueConstraint("code", name="uq_analysis_dimensions_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    code: Mapped[str] = mapped_column(String(CODE_MAX_LENGTH), nullable=False)
    """Mã ổn định (`STAT`, `REGION`, `CHANNEL`) — khóa mà báo cáo tham chiếu.

    Duy nhất toàn dữ liệu kế toán, **không** theo chi nhánh: một chiều phân tích
    chỉ hữu ích nếu mọi chi nhánh hiểu nó giống nhau, còn hai chi nhánh định
    nghĩa `REGION` khác nhau thì báo cáo hợp nhất cộng nhầm.
    """

    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)

    value_source: Mapped[DimensionValueSource] = mapped_column(
        _VALUE_SOURCE_TYPE,
        nullable=False,
        default=DimensionValueSource.LIST,
        server_default=DimensionValueSource.LIST.value,
    )
    master_slug: Mapped[str | None] = mapped_column(String(CODE_MAX_LENGTH), nullable=True)
    """`slug` của danh mục cấp giá trị, khi `value_source = master`.

    Là **chuỗi** chứ không khóa ngoại: đích của nó là `CatalogSpec.slug` — một
    hằng trong mã nguồn, không phải một dòng trong bảng. Dịch vụ kiểm slug có
    thật lúc ghi (`service.py`); ở đây `CHECK` chỉ canh được cặp cột.
    """

    is_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    """Bắt buộc điền khi hạch toán vào tài khoản thuộc `applies_to_accounts`.

    Chưa được ép ở v1 (RT-20) — validator sống ở phase 4 cùng posting engine,
    nơi có dòng hạch toán thật để ép. Cột khai từ đây để gói cấu hình khai được
    ý định ngay, và để bật validator sau không phải migrate.
    """

    applies_to_accounts: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(ACCOUNT_PREFIX_MAX_LENGTH)), nullable=True
    )
    """Dải số hiệu tài khoản mà chiều này áp dụng; `NULL` = mọi tài khoản.

    Lưu **tiền tố** (`'641'` phủ `6411`, `64111`) chứ không liệt kê từng tài
    khoản: hệ thống tài khoản là cây mở (FR-SYS-020), nên một danh sách đầy đủ
    sẽ lỗi thời ngay lần đầu khách hàng thêm tài khoản chi tiết.
    """

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class AnalysisDimensionValue(DatasetBase, Audited, RowVersioned):
    """Một giá trị của chiều `list` — có cây riêng để cộng tổng theo nhánh."""

    __tablename__ = ANALYSIS_DIMENSION_VALUE_TABLE_NAME
    __table_args__ = (
        CheckConstraint("code <> ''", name="code_not_blank"),
        CheckConstraint(f"path ~ '{PATH_PATTERN}'", name="path_is_dotted_ids"),
        CheckConstraint(f"level >= {ROOT_LEVEL}", name="level_at_least_root"),
        # Mã duy nhất **trong một chiều**, không phải toàn bảng: `BAC` là miền
        # Bắc ở chiều `REGION` và là "bán buôn cấp 1" ở một chiều khác — hai
        # chiều không biết gì về nhau nên không có lý do để tranh mã.
        UniqueConstraint("dimension_id", "code", name="uq_analysis_dimension_values_code"),
        Index("ix_analysis_dimension_values_path", "dimension_id", "path"),
        Index("ix_analysis_dimension_values_parent_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    dimension_id: Mapped[int] = mapped_column(
        ForeignKey(f"{ANALYSIS_DIMENSION_TABLE_NAME}.id", ondelete="RESTRICT"), nullable=False
    )
    """`RESTRICT` chứ không `CASCADE`: xóa một chiều mà cuốn theo toàn bộ giá trị
    của nó là cách làm mọi dòng phát sinh đã gắn giá trị đó mất ý nghĩa trong một
    lệnh. Muốn bỏ một chiều thì tắt `is_active` — cùng lối FR-SYS-012 chỉ ra cho
    danh mục."""

    code: Mapped[str] = mapped_column(String(CODE_MAX_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(NAME_MAX_LENGTH), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(NAME_MAX_LENGTH), nullable=True)

    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey(f"{ANALYSIS_DIMENSION_VALUE_TABLE_NAME}.id", ondelete="RESTRICT"), nullable=True
    )
    path: Mapped[str] = mapped_column(String(PATH_MAX_LENGTH), nullable=False)
    level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=ROOT_LEVEL, server_default=str(ROOT_LEVEL)
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
