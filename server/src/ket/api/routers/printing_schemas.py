"""Schema Pydantic của in chứng từ (`/api/v1/print-templates`, `…/print`) — lát 5D."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PrintTemplateSummaryResponse(BaseModel):
    """Một mẫu in trong hộp chọn của nút In."""

    model_config = ConfigDict(from_attributes=True)

    document_type: str
    code: str
    name: str
    is_default: bool
    is_builtin: bool


class PrintTemplateListResponse(BaseModel):
    templates: list[PrintTemplateSummaryResponse]


class VoucherPrintRequest(BaseModel):
    """Thân `POST /vouchers/{id}/print` — bỏ trống `template_code` = mẫu mặc định."""

    template_code: str | None = Field(default=None, max_length=50)
