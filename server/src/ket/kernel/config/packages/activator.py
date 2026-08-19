"""Kích hoạt một gói cấu hình (FR-SYS-004, RT-09).

Kích hoạt = ghi `activated_at`/`activated_by` — một ghi ORM bình thường, có vết
tự động (`ConfigPackage` là `Audited`). Phần khó là **cổng chặn**: một khi năm
tài chính đã có chứng từ, chế độ kế toán của năm đó đã chốt (nguyên tắc U14,
cùng bất biến với `PeriodService.ensure_scheme_changeable`) — kích hoạt một gói
khác `scheme` cho **chế độ đang chốt đó** là mở lại đúng cánh cửa mà
`ensure_scheme_changeable` sinh ra để đóng, chỉ đi vòng qua màn hình gói cấu
hình thay vì màn hình năm tài chính.

**Vì sao `has_vouchers` do người gọi truyền vào:** kernel không được biết bảng
`vouchers` tồn tại (luật phụ thuộc C1 — `ket.kernel` không import `ket.posting`).
Cùng lý do và cùng khuôn với `PeriodService.ensure_scheme_changeable(fiscal_year_id,
*, has_documents: bool)` (xem `kernel/periods/service.py` và
`api/routers/setup.py`): tầng gọi (API, nơi được phép biết cả hai tầng) tính
tập năm tài chính đã có chứng từ rồi truyền vào đây bằng `id`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ConfigPackage
from ket.kernel.errors import AccountingSchemeLockedError, ConfigPackageIdUnknownError
from ket.kernel.periods.models import FiscalYear


def _require_package(session: Session, package_id: int) -> ConfigPackage:
    package = session.get(ConfigPackage, package_id)
    if package is None:
        raise ConfigPackageIdUnknownError("Không có gói cấu hình với id này", package_id=package_id)
    return package


def _reject_if_scheme_conflicts(
    session: Session, *, package: ConfigPackage, fiscal_year_ids_with_vouchers: Sequence[int]
) -> None:
    if not fiscal_year_ids_with_vouchers:
        return
    # `FOR SHARE` — cùng house style RT-09 với `PostingService`/`PeriodService`:
    # khóa đọc trên đúng những dòng năm tài chính đang xét, giữ tới hết
    # transaction kích hoạt. Một request khóa kỳ đầu tiên của năm khác `scheme`
    # đang chạy song song sẽ chờ ở đây thay vì cả hai cùng đọc "chưa có chứng
    # từ xung đột" rồi cùng thành công (write-skew).
    conflicting = (
        session.execute(
            select(FiscalYear.id, FiscalYear.code)
            .where(FiscalYear.id.in_(set(fiscal_year_ids_with_vouchers)))
            .where(FiscalYear.accounting_scheme != package.scheme)
            .with_for_update(read=True)
        )
        .tuples()
        .all()
    )
    if conflicting:
        first_id, first_code = conflicting[0]
        raise AccountingSchemeLockedError(
            "Có năm tài chính đã phát sinh chứng từ theo chế độ kế toán khác — "
            "không kích hoạt được gói cấu hình của chế độ khác cho dữ liệu này",
            package_id=package.id,
            package_scheme=package.scheme,
            fiscal_year_id=first_id,
            fiscal_year_code=first_code,
        )


def activate(
    session: Session,
    *,
    package_id: int,
    actor: int,
    fiscal_year_ids_with_vouchers: Sequence[int] = (),
) -> ConfigPackage:
    """Kích hoạt một gói cấu hình. Kích hoạt gói mới hơn **cùng** `scheme` luôn
    được phép — chỉ chặn khi đổi sang `scheme` khác trên dữ liệu đã có chứng từ.

    `fiscal_year_ids_with_vouchers` là **mọi** năm tài chính của dataset đã có
    ít nhất một chứng từ (không riêng năm khớp `scheme` — hàm tự lọc), do tầng
    gọi tính trước khi mở transaction ghi (xem docstring đầu tệp).
    """
    package = _require_package(session, package_id)
    _reject_if_scheme_conflicts(
        session, package=package, fiscal_year_ids_with_vouchers=fiscal_year_ids_with_vouchers
    )
    package.activated_at = datetime.now(UTC)
    package.activated_by = actor
    session.flush()
    return package
