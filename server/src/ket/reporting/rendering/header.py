"""Đầu báo cáo: đơn vị (tên, địa chỉ, MST) + tiêu đề + dòng tham số (BR-RPT-03).

Thông tin đơn vị lấy từ **gốc cây chi nhánh** — chi nhánh cấp cao nhất là pháp
nhân đứng tên bộ sổ. Logo + cấu hình font (FR-RPT-010) đến ở 5D cùng
`print_templates`; hợp đồng `UnitInfo` giữ chỗ sẵn.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.kernel.security.models import Branch


@dataclass(frozen=True)
class UnitInfo:
    name: str
    tax_code: str | None
    address: str | None


def load_unit_info(session: Session) -> UnitInfo:
    """Đơn vị đứng đầu báo cáo = chi nhánh gốc (path ngắn nhất người gọi thấy).

    Dùng `path` chứ không `parent_id IS NULL`: RLS có thể che chi nhánh gốc với
    người dùng bị giới hạn — khi đó đơn vị đứng tên bản in là nút cao nhất
    TRONG PHẠM VI của họ, không phải một dòng rỗng.
    """
    branch = session.execute(
        select(Branch).order_by(func.length(Branch.path), Branch.id).limit(1)
    ).scalar_one_or_none()
    if branch is None:
        return UnitInfo(name="", tax_code=None, address=None)
    return UnitInfo(name=branch.name, tax_code=branch.tax_code, address=branch.address)


def signature_date_line(on_date: date) -> str:
    return f"Ngày {on_date.day:02d} tháng {on_date.month:02d} năm {on_date.year}"
