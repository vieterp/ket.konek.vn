"""Hình dạng phản hồi của endpoint BCTC (`/api/v1/statements`)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class StatementLayoutSummaryResponse(BaseModel):
    """Một mẫu BCTC trong gói cấu hình đang hiệu lực."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    name_en: str | None
    statement_kind: str


class StatementLayoutListResponse(BaseModel):
    package_code: str
    layouts: list[StatementLayoutSummaryResponse]


class StatementRowValueResponse(BaseModel):
    """Một chỉ tiêu đã tính — giá trị cột chính + cột so sánh."""

    model_config = ConfigDict(from_attributes=True)

    row_code: str
    label: str
    label_en: str | None
    note_ref: str | None
    indent_level: int
    is_bold: bool
    hide_when_zero: bool
    value: Decimal
    comparative: Decimal | None
    """`null` = cột so sánh chưa lập được (layout phát sinh — "Năm trước" cần
    loại bút toán kết chuyển, chờ 10a). Client hiện "—", KHÔNG hiện 0."""


class StatementPreviewResponse(BaseModel):
    """Một BCTC đã lập cho `(layout, kỳ, sổ[, chi nhánh])`.

    Cột so sánh (`comparative`) là "Số đầu năm" với báo cáo tình hình tài
    chính; với báo cáo phát sinh nó là `null` ở v1 — xem bất biến H1 trong
    `reporting/statements/builder.py`.
    """

    layout_code: str
    name: str
    name_en: str | None
    statement_kind: str
    package_code: str
    ledger: int
    period_id: int
    branch_id: int | None
    rows: list[StatementRowValueResponse]
