"""Hình dạng request/response của nhập liệu danh mục.

Phản hồi của cả hai bước là **một job**, không phải kết quả: 10.000 dòng của
FR-NFR-043 không chạy xong trong một request HTTP, và FR-NFR-044 nói giao diện
không được khóa. Client theo dõi bằng `GET /api/v1/jobs/{id}` — cùng đường mà
mọi tác vụ nền khác đã dùng, nên màn hình nhập liệu không cần cơ chế riêng nào.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ket.kernel.excel.report import ImportMode


class ImportValidateResponse(BaseModel):
    """Lượt kiểm đã được xếp hàng.

    Trả `content_hash` về cho client dù nó không phải nhập lại ở bước ghi: màn
    hình cần nó để nhận ra "tệp này tôi vừa tải lên xong" khi người dùng bấm tải
    lên cùng một tệp hai lần, và để hiện ra ở phần lịch sử.
    """

    job_id: UUID
    catalog: str
    file_name: str
    content_hash: str
    mode: ImportMode


class ImportCommitRequest(BaseModel):
    """Bước ghi: trỏ vào **lượt kiểm** đã thành công, không trỏ vào tệp (H78)."""

    model_config = ConfigDict(extra="forbid")

    validation_job_id: UUID = Field(
        description="`job_id` mà bước kiểm dữ liệu trả về, phải đã chạy xong và không còn dòng lỗi"
    )


class ImportCommitResponse(BaseModel):
    """Lượt ghi đã được xếp hàng."""

    job_id: UUID
    catalog: str
