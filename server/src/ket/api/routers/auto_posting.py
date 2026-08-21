"""Tra nghiệp vụ định khoản tự động qua HTTP (`/api/v1/auto-posting`) — lát 6A.

Đọc-only, cùng khuôn `/accounts` (lát 4E): nghiệp vụ là dữ liệu của gói cấu
hình, sửa đổi thuộc màn quản trị gói. Form phiếu thu/chi gọi endpoint này lúc
mở, người dùng chọn nghiệp vụ → form điền sẵn cặp Nợ/Có (FR-SYS-025).

Cùng mã quyền `master.account.view` với `/accounts`, có chủ đích: phản hồi ở
đây là **số hiệu TK ngầm định** — đúng lớp thông tin mà `/accounts` đã trả, và
người nhập chứng từ nào cũng cần cả hai cùng lúc. Một mã quyền riêng chỉ tạo
thêm một ô phân quyền mà bật cái này tắt cái kia không có nghĩa gì.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from ket.api.dependencies import SessionFactory
from ket.api.routers.accounts import AccountReader
from ket.api.routers.auto_posting_schemas import (
    AutoPostingOperationResponse,
    AutoPostingOperationsResponse,
)
from ket.kernel.config.auto_posting_provider import operations_for
from ket.kernel.errors import DomainError
from ket.kernel.periods.service import fiscal_year_covering
from ket.kernel.persistence.unit_of_work import unit_of_work

router = APIRouter(prefix="/api/v1/auto-posting", tags=["auto-posting"])

DOCUMENT_TYPE_MAX_QUERY_LENGTH = 20
"""Bằng `vouchers.document_type` — tham số lạ dài hơn cột là request hỏng."""


@router.get("/operations", response_model=AutoPostingOperationsResponse)
def list_operations(
    authorized: AccountReader,
    factory: SessionFactory,
    document_type: Annotated[str, Query(min_length=1, max_length=DOCUMENT_TYPE_MAX_QUERY_LENGTH)],
    on_date: Annotated[date, Query()],
) -> AutoPostingOperationsResponse:
    """Nghiệp vụ của một loại chứng từ, đã phân giải TK theo gói hiệu lực.

    `on_date` bắt buộc, cùng lý do với `/accounts`: gói cấu hình hiệu lực theo
    ngày, và form đã biết ngày hạch toán nên cứ gửi nó lên. Loại chứng từ chưa
    khai nghiệp vụ nào trả danh sách rỗng — không phải lỗi: gói nhập ngoài có
    thể không mang `auto_posting_rules.csv`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        year = fiscal_year_covering(session, on_date)
        if year is None:
            raise DomainError(
                "Chưa có năm tài chính phủ ngày này — tạo năm tài chính trước khi tra nghiệp vụ",
                on_date=on_date.isoformat(),
            )
        resolved = operations_for(
            session,
            document_type=document_type,
            scheme=year.accounting_scheme,
            on_date=on_date,
        )
        return AutoPostingOperationsResponse(
            package_id=resolved.package_id,
            items=[AutoPostingOperationResponse.model_validate(item) for item in resolved.items],
        )
