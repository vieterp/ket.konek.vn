"""Schema Pydantic của `/api/v1/reports` (lát 5C)."""

from __future__ import annotations

from typing import Literal

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
