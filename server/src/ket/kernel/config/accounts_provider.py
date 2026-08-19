"""Tra hệ thống tài khoản theo ngày hạch toán (phase-05 §Quy tắc đọc, dạng tối thiểu).

Quy tắc đọc của plan: *mọi* truy vấn hệ thống TK đi qua một chỗ chọn gói theo
`(scheme, ngày)` — không code nào truy vấn `chart_of_accounts` bằng `code` cứng
hay bằng gói đoán sẵn. Phase 4 cần đúng hai phép: chọn gói hiệu lực, và nạp
tài khoản theo `id` để validator kiểm. Phần còn lại (kích hoạt gói, tài khoản
ngầm định theo `purpose`, cặp kết chuyển) là phase 5 — dựng quanh đúng hai hàm
này chứ không thay chúng.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount, ConfigPackage
from ket.kernel.errors import ConfigPackageNotFoundError


def resolve_package(session: Session, *, scheme: str, on_date: date) -> ConfigPackage:
    """Gói cấu hình hiệu lực cho một chế độ kế toán tại một ngày.

    `effective_to IS NULL` nghĩa là "còn hiệu lực" — điều kiện nửa mở
    `effective_from <= ngày < effective_to` khớp mô tả phase-05. Nhiều gói cùng
    phủ một ngày (gói sửa đổi phát hành chồng lên gói cũ) thì lấy gói có
    `effective_from` muộn nhất: gói mới hơn thay gói cũ kể từ ngày nó hiệu lực.
    """
    package = (
        session.execute(
            select(ConfigPackage)
            .where(ConfigPackage.scheme == scheme)
            .where(ConfigPackage.effective_from <= on_date)
            .where((ConfigPackage.effective_to.is_(None)) | (ConfigPackage.effective_to > on_date))
            .order_by(ConfigPackage.effective_from.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if package is None:
        raise ConfigPackageNotFoundError(
            "Chưa có gói cấu hình nào hiệu lực cho chế độ kế toán này tại ngày hạch toán",
            scheme=scheme,
            on_date=on_date.isoformat(),
        )
    return package


def accounts_by_id(session: Session, account_ids: Sequence[int]) -> dict[int, ChartOfAccount]:
    """Nạp một lượt các tài khoản mà chứng từ chạm tới — một truy vấn cho cả chứng từ.

    Trả `dict` thiếu-là-vắng-mặt chứ không ném: validator ghi sổ cần báo **mọi**
    dòng trỏ vào tài khoản không tồn tại, không phải dừng ở dòng đầu tiên.
    """
    if not account_ids:
        return {}
    rows = (
        session.execute(select(ChartOfAccount).where(ChartOfAccount.id.in_(set(account_ids))))
        .scalars()
        .all()
    )
    return {row.id: row for row in rows}
