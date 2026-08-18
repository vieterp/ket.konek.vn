"""Báo cáo của một lượt kiểm dữ liệu (FR-SYS-081).

Ba con số và một danh sách. Ba con số trả lời câu người dùng hỏi trước khi bấm
ghi — *bao nhiêu dòng, tạo mới bao nhiêu, sửa bao nhiêu* — và danh sách trả lời
câu họ hỏi sau đó: *sai ở đâu*.

Báo cáo này đi thẳng vào `jobs.result` (JSONB) theo H78, nên nó phải là thứ
`model_dump(mode="json")` được và phải có **trần**: một tệp dán nhầm cả bảng
tính khác sinh ra 10.000 dòng lỗi giống hệt nhau, và nhét cả 10.000 vào một ô
JSONB là cách một lần bấm nút làm phình bảng `jobs` thêm vài megabyte.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field

MAX_REPORTED_ERRORS: Final[int] = 500
"""Số dòng lỗi giữ lại trong báo cáo. Phần còn lại chỉ còn là một con số.

Năm trăm là mức "sửa được": quá số đó thì vấn đề không phải từng dòng nữa mà là
cả tệp sai cột hoặc sai nguồn dữ liệu, và thứ giúp được người dùng lúc ấy là
mười dòng đầu chứ không phải mười nghìn dòng."""


class ImportMode(StrEnum):
    """Lượt nhập được phép làm gì với mã đã tồn tại (H80).

    Mặc định là `CREATE_ONLY` ở mọi nơi khai giá trị này. Đó là quyết định có
    chủ đích: `CREATE_AND_UPDATE` biến một bảng tính dán nhầm thành một lần sửa
    hàng loạt không ai duyệt, nên nó phải là thứ người dùng **chọn**, và chọn
    sau khi đọc con số "sẽ cập nhật 9.960 dòng" trong báo cáo.
    """

    CREATE_ONLY = "create_only"
    CREATE_AND_UPDATE = "create_and_update"


class MissingReferenceMode(StrEnum):
    """Làm gì với một ô tra cứu mang mã chưa có trong danh mục đích (FR-NFR-062).

    Mặc định `ERROR` ở mọi nơi khai giá trị này, cùng lý do với `ImportMode`:
    `CREATE` ghi vào một danh mục **khác** danh mục người dùng đang nhập, nên nó
    phải là thứ họ chọn — và chọn sau khi đọc con số "sẽ tạo thêm 12 đơn vị tính"
    trong báo cáo của lượt kiểm.
    """

    ERROR = "error"
    CREATE = "create"
    """Tạo bản ghi tối thiểu (mã + tên bằng chính mã ấy) ở danh mục đích.

    Chỉ áp cho danh mục **tự tạo được** — xem `CatalogSpec.auto_creatable`. Tên
    đặt bằng chính cái mã để bản ghi sinh ra nhìn là biết cần đặt lại tên: một
    bản ghi tên "Chưa đặt tên" trông như dữ liệu thật, còn một bản ghi tên `DVT01`
    thì không.

    **Không** áp cho cột mã nhóm cha: nhóm cha trỏ về chính danh mục đang nhập, và
    tệp được phép khai cả cây trong một lượt (xem `staging._declared_in_file`).
    Tự tạo ở đó sẽ biến một mã nhóm cha gõ sai thành một nút rác thay vì một lỗi.
    """


class RowError(BaseModel):
    """Một lỗi, gắn với đúng một ô.

    Có **cả** `row` lẫn `column`: FR-SYS-081 nói "hiển thị nguyên nhân lỗi theo
    từng dòng", nhưng một dòng mười cột mà chỉ báo số dòng thì người sửa vẫn
    phải dò. `column` là tiêu đề như trên tệp (kèm dấu `*` nếu bắt buộc), không
    phải tên trường nội bộ — người đọc đang nhìn Excel, không nhìn model ORM.
    """

    row: int
    column: str | None
    """`None` cho lỗi thuộc về cả dòng (ví dụ mã trùng với một dòng khác trong
    cùng tệp), khi không có một ô nào đáng bị chỉ đích danh hơn ô khác."""

    code: str
    """Mã lỗi ổn định để client dựng câu tiếng Việt — cùng hợp đồng với
    `DomainError.error_code`."""

    message: str
    """Câu tiếng Việt sẵn dùng, cho đường xuất báo cáo lỗi ra tệp."""

    value: str | None = None
    """Nguyên văn ô người dùng đã gõ, cắt ngắn. Có nó thì câu báo lỗi nói được
    "`abc` không phải số" thay vì "sai kiểu dữ liệu"."""


class ImportReport(BaseModel):
    """Kết quả một lượt kiểm — và, sau khi ghi, kết quả của lượt ghi.

    Một model cho cả hai bước chứ không hai: lượt ghi chạy **lại** đúng phép
    kiểm ấy (H85), nên nó có cùng thứ để kể. Hai model sẽ là hai chỗ để cùng một
    con số được đặt tên khác nhau.
    """

    catalog: str
    """`slug` của danh mục — nó đi vào `jobs.result`, nơi không còn ngữ cảnh nào
    khác cho biết lượt nhập này thuộc danh mục nào."""

    mode: ImportMode
    missing_reference: MissingReferenceMode = MissingReferenceMode.ERROR
    file_name: str
    content_hash: str
    """SHA-256 của tệp đã kiểm. Đây là mấu chốt của H78: bước ghi so lại giá trị
    này nên "tệp đã kiểm" và "tệp sẽ ghi" là **cùng một** chuỗi byte, chứ không
    phải cùng một cái tên."""

    total_rows: int
    create_rows: int = 0
    update_rows: int = 0
    """Với `CREATE_ONLY` thì luôn 0 — mã đã tồn tại là dòng lỗi, không phải dòng
    sẽ cập nhật."""

    created_references: dict[str, int] = Field(default_factory=dict)
    """`{slug danh mục đích: số bản ghi sẽ tạo thêm}` (FR-NFR-062).

    Có mặt cả ở lượt **kiểm** — nơi nó là dự báo, và là con số người dùng phải
    đọc trước khi bật chế độ tự tạo — lẫn ở lượt **ghi**, nơi nó là kết quả. Rỗng
    khi `missing_reference` là `ERROR`.

    Khóa là `slug` chứ không nhãn tiếng Việt: client dựng câu từ registry mà nó
    đã có, và một nhãn đi vào `jobs.result` sẽ đóng băng bản dịch của ngày hôm nay.
    """

    error_rows: int = 0
    """Số dòng **có lỗi**, không phải số lỗi: một dòng sai ba ô vẫn là một dòng
    người dùng phải sửa, và đó là đơn vị họ đếm."""

    errors: list[RowError] = Field(default_factory=list)
    truncated_errors: bool = False
    """Danh sách trên đã bị cắt ở `MAX_REPORTED_ERRORS`. Khai tường minh thay vì
    để người đọc tự so `len(errors)` với `error_rows`: một danh sách bị cắt im
    lặng trông y hệt một danh sách đầy đủ."""

    committed: bool = False
    """Lượt này đã **ghi** hay chỉ mới kiểm. Phân biệt ở đây chứ không bằng loại
    job, vì `jobs.result` là chỗ duy nhất người đọc lịch sử nhìn thấy."""

    @property
    def is_valid(self) -> bool:
        """Không dòng nào lỗi — điều kiện bắt buộc để được ghi (FR-SYS-081)."""
        return self.error_rows == 0

    def add(self, error: RowError) -> None:
        """Thêm một lỗi, tôn trọng trần báo cáo."""
        if len(self.errors) >= MAX_REPORTED_ERRORS:
            self.truncated_errors = True
            return
        self.errors.append(error)
