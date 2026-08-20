"""Schema Pydantic của `/api/v1/reports` (lát 5C)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ReportSummaryResponse(BaseModel):
    """Một dòng trong danh mục báo cáo (màn *Sổ sách & Thuế*)."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    name_en: str | None
    category: str
    module: str
    ledger_scope: Literal["both", "financial", "management"]


class ReportListResponse(BaseModel):
    reports: list[ReportSummaryResponse]


class ReportParamFieldResponse(BaseModel):
    """Một ô nhập NGOÀI bộ chuẩn — client dựng form từ danh sách này.

    Bộ chuẩn (`from_date`, `to_date`, `branch_ids`, `ledger`) là hợp đồng cố
    định FR-RPT-002, client dựng sẵn, không lặp lại ở đây.
    """

    name: str
    kind: Literal["date", "int", "text", "bool", "decimal"]
    label: str
    label_en: str | None
    required: bool


class ReportParamsResponse(BaseModel):
    code: str
    name: str
    ledger_scope: Literal["both", "financial", "management"]
    params: list[ReportParamFieldResponse]


class ReportRenderRequest(BaseModel):
    """Thân `POST /reports/{code}/render`.

    `params` là JSON tự do Ở RANH GIỚI — engine kiểm bằng model Pydantic sinh
    động từ `param_set.spec` (FR-RPT-002) rồi mới cho đi tiếp dưới dạng có kiểu
    (`BoundParams`), đúng tinh thần LD-13: dict thô không đi QUA ranh giới
    module, nó dừng ở bộ kiểm.
    """

    format: Literal["pdf", "xlsx"]
    params: dict[str, JsonValue] = Field(default_factory=dict)


class ReportRenderAcceptedResponse(BaseModel):
    """Thân `202` của `POST /reports/{code}/render` — báo cáo vượt ngưỡng
    chuyển-job (bước 19), đã xếp vào hàng đợi thay vì render trong request.

    Client theo dõi qua `GET /api/v1/jobs/{job_id}` (tiến độ + nút Hủy) rồi tải
    tệp ở `GET /reports/render-jobs/{job_id}/file` khi job `done`.
    """

    job_id: UUID
    estimated_rows: int


class ReportPreviewRequest(BaseModel):
    """Thân `POST /reports/{code}/preview` — cùng hợp đồng `params` với render."""

    params: dict[str, JsonValue] = Field(default_factory=dict)


class PreviewColumnResponse(BaseModel):
    """Một cột của lưới xem trước — đủ để client dựng header + căn lề."""

    key: str
    label: str
    label_en: str | None
    type: Literal["text", "date", "money", "quantity"]
    align: Literal["left", "center", "right"] | None
    width: int | None


class PreviewCellResponse(BaseModel):
    text: str
    css: str
    """Lớp CSS trình bày (`cell-money cell-right`…) — cùng bộ lớp với bản in
    PDF, để lưới xem trước và tờ giấy căn lề giống nhau."""


class PreviewRowResponse(BaseModel):
    """Một dòng trình bày: dữ liệu, tiêu đề nhóm, tổng nhóm hay tổng cộng."""

    kind: Literal["data", "group_header", "group_footer", "grand_total"]
    heading: str | None = None
    label_span: int | None = None
    cells: list[PreviewCellResponse] | None = None


class ReportPreviewResponse(BaseModel):
    """Bản xem trước dạng lưới (bước 14 phase-05) — ô đã định dạng sẵn phía
    server bằng CHÍNH pha chữ của bản in (BR-RPT-02 ở tầng trình bày); client
    chỉ vẽ, không tính."""

    code: str
    name: str
    param_lines: list[str]
    columns: list[PreviewColumnResponse]
    rows: list[PreviewRowResponse]
    truncated: bool
    """`true` = lưới bị cắt ở trần xem trước — xuất XLSX/PDF để lấy đủ."""
