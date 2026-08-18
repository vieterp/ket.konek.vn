"""Luật liên-trường của một danh mục, ở dạng bước nhập liệu đánh giá được (H3).

**Vấn đề nó giải.** Ràng buộc `CHECK` liên-trường sống dưới DB — đó là chỗ đúng,
và H76/H90 đã chốt rằng nó phải ở đó vì không phải đường ghi nào cũng đi qua
Pydantic. Nhưng bước **kiểm** của lượt nhập không biết chúng: nó kiểm bắt buộc,
độ dài, kiểu, tra mã — rồi báo "hợp lệ". Bước ghi sau đó đổ với một thông báo
`IntegrityError` thô nêu tên một ràng buộc nội bộ. Người dùng đọc "tệp hợp lệ"
rồi nhận "ck_items_stock_item_needs_base_unit". Đó là nợ **H3** mà lát 3C-1 ghi
lại và để mở.

**Cách giải.** Danh mục khai luật của mình ở đây, cạnh chính model mang ràng
buộc `CHECK` tương ứng, và bước kiểm dựng luật ấy thành một `WHERE` trên bảng
đệm — cùng một luật, hai chỗ ép, mỗi chỗ làm điều nó làm tốt nhất: `CHECK` là
**chỗ bảo đảm** (mọi đường ghi đều qua), bước kiểm là **chỗ nói** (câu tiếng
Việt nêu đúng ô và đúng dòng). Cùng khuôn H76 đã đặt cho vật tư hàng hóa, chỉ
mở rộng sang đường thứ ba.

**Vì sao là một hàm nhận `StagedRow` chứ không một chuỗi SQL.** Chuỗi SQL ở
tầng này sẽ phải nói tên cột của bảng đệm và tự ghép — đúng thứ
`excel/sql.py` giải thích là **hàng rào bảo mật chính** chứ không phải sở
thích. `StagedRow` là một khung nhìn có kiểu: luật viết bằng biểu thức Core, và
mypy kiểm nó.

**Vì sao Protocol ở đây mà không import `excel`.** `excel/sql.py` import registry
danh mục (nó cần tra bảng đích của một mã), nên chiều ngược lại sẽ là một vòng
import. Protocol cắt vòng ấy: `master_data` khai **hình dạng** của khung nhìn,
`excel/staging.py` là bên dựng ra nó.

**Giá trị "hiệu lực", không phải ô thô.** `StagedRow` trả về giá trị mà dòng ấy
**sẽ mang sau khi ghi** — nghĩa là ô trống trên một dòng cập nhật cho ra giá trị
đang có trong DB (H87/H91), còn trên một dòng tạo mới thì cho ra ngầm định của
cột. Đây là điểm dễ sai nhất: đánh giá luật trên ô thô sẽ báo lỗi cho một dòng
chỉ sửa tên của một mã hàng vốn hoàn toàn hợp lệ, chỉ vì tệp không nhắc lại đơn
vị tính của nó.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import ColumnElement


class StagedRow(Protocol):
    """Khung nhìn có kiểu lên **giá trị hiệu lực** của một dòng đang được kiểm.

    Mỗi phương thức nhận tên trường **như trên tệp mẫu** (`ColumnDescriptor.field`)
    — `base_unit_code`, không phải `base_unit_id` — vì đó là thứ người viết luật
    nhìn thấy khi mở tệp mẫu ra, và là thứ câu báo lỗi sẽ trỏ tới.
    """

    def value(self, field: str) -> ColumnElement[Any]:
        """Giá trị hiệu lực của một cột: số, chuỗi, hoặc `id` đã tra ra từ mã."""
        ...

    def flag(self, field: str) -> ColumnElement[bool]:
        """Giá trị hiệu lực của một cột boolean."""
        ...


@dataclass(frozen=True)
class RowRule:
    """Một luật liên-trường, phản chiếu đúng một `CHECK` dưới DB."""

    constraint: str
    """Tên ràng buộc `CHECK` tương ứng, **không** tiền tố `ck_<bảng>_`.

    Không dùng để chạy — dùng để **đối chiếu**: `tests/test_import_row_rules.py`
    đọc `pg_constraint` của mọi bảng danh mục và đòi mỗi ràng buộc liên-trường
    phải có một luật mang đúng tên nó. Nhờ vậy một `CHECK` thêm ở phase 5 làm bộ
    test đỏ thay vì lặng lẽ quay lại đúng triệu chứng H3.
    """

    field: str
    """Cột mà câu báo lỗi trỏ tới — ô người dùng phải sửa."""

    message: str
    """Câu tiếng Việt hoàn chỉnh. Nêu **điều kiện đúng**, không nêu tên ràng buộc."""

    violated: Callable[[StagedRow], ColumnElement[bool]]
    """Biểu thức đúng khi dòng **vi phạm** luật.

    Chiều "vi phạm" chứ không chiều "hợp lệ", vì nó đi thẳng vào `WHERE` của
    truy vấn tìm dòng lỗi. Phủ định một biểu thức có `NULL` ở giữa là chỗ ba
    trạng thái của SQL cắn người viết, nên tránh hẳn một phép phủ định.
    """

    code: str = "import.row_rule_violated"
    """Mã lỗi ổn định cho client. Mặc định dùng chung vì client dựng câu từ
    `message` — mã riêng chỉ cần khi có màn hình xử lý riêng cho một luật."""
