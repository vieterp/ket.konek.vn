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

from ket.kernel.config.accounts_models import (
    DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE,
    ChartOfAccount,
    ClosingAccountPair,
    ConfigPackage,
    DefaultAccount,
)
from ket.kernel.errors import ConfigPackageNotFoundError, DefaultAccountNotConfiguredError


def resolve_package(session: Session, *, scheme: str, on_date: date) -> ConfigPackage:
    """Gói cấu hình **đã kích hoạt** hiệu lực cho một chế độ kế toán tại một ngày.

    **Bất biến (sửa sau review, C1 CRITICAL):** chỉ gói có `activated_at IS NOT
    NULL` được xét. Trước sửa này, hàm chọn theo `effective_from` mà không lọc
    kích hoạt — một gói **nhập vào** (`importer.import_package`, `is_builtin=False`,
    `activated_at=NULL` cho tới khi ai bấm "kích hoạt") sẽ **chiếm quyền đọc TK
    ngay khi nhập**, đi vòng qua toàn bộ cổng chặn đổi chế độ kế toán của
    `activator.activate` (FR-SYS-004). `import` do đó **vô hại cho tới khi kích
    hoạt** — không cần thêm cổng scheme-lock riêng ở đường nhập, vì đường đọc
    duy nhất (hàm này) đã tự khóa theo kích hoạt.

    `effective_to IS NULL` nghĩa là "còn hiệu lực" — điều kiện nửa mở
    `effective_from <= ngày < effective_to` khớp mô tả phase-05. Nhiều gói cùng
    phủ một ngày (gói sửa đổi phát hành chồng lên gói cũ) thì lấy gói có
    `effective_from` muộn nhất: gói mới hơn thay gói cũ kể từ ngày nó hiệu lực.

    Hai tiêu chí phụ khi `effective_from` bằng nhau: `activated_at` muộn hơn
    thắng trước (gói kích hoạt **sau** là quyết định gần nhất của người quản
    trị — có ý nghĩa nghiệp vụ, không phải một mẹo phá hòa); `id` lớn hơn là
    lưới an toàn cuối khi cả `effective_from` lẫn `activated_at` đều bằng nhau
    (không có nó, `ORDER BY` không cam kết thứ tự và hai lần chạy có thể chọn
    khác gói nhau cho cùng một truy vấn).
    """
    package = (
        session.execute(
            select(ConfigPackage)
            .where(ConfigPackage.scheme == scheme)
            .where(ConfigPackage.activated_at.is_not(None))
            .where(ConfigPackage.effective_from <= on_date)
            .where((ConfigPackage.effective_to.is_(None)) | (ConfigPackage.effective_to > on_date))
            .order_by(
                ConfigPackage.effective_from.desc(),
                ConfigPackage.activated_at.desc(),
                ConfigPackage.id.desc(),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if package is None:
        raise ConfigPackageNotFoundError(
            "Chưa có gói cấu hình nào đã kích hoạt và hiệu lực cho chế độ kế toán này "
            "tại ngày hạch toán",
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


def default_account(
    session: Session, *, package_id: int, document_type: str, purpose: str
) -> DefaultAccount:
    """TK ngầm định của một mục đích, trong một gói (FR-SYS-024, ngoại lệ có kiểm soát).

    Tra theo `document_type` cụ thể trước; không có thì lùi về
    `DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE` ('*') — gói cấu hình khai một dòng
    dùng chung mọi loại chứng từ thay vì lặp lại cho từng loại. Không tìm thấy
    ở cả hai mức là một khoảng trống cấu hình thật (`DefaultAccountNotConfiguredError`),
    không phải lỗi gõ tay của người gọi.
    """
    specific = session.get(DefaultAccount, (package_id, document_type, purpose))
    if specific is not None:
        return specific
    fallback = session.get(
        DefaultAccount, (package_id, DEFAULT_ACCOUNT_WILDCARD_DOCUMENT_TYPE, purpose)
    )
    if fallback is not None:
        return fallback
    raise DefaultAccountNotConfiguredError(
        "Gói cấu hình chưa khai tài khoản ngầm định cho mục đích này",
        package_id=package_id,
        document_type=document_type,
        purpose=purpose,
    )


def closing_pairs(session: Session, *, package_id: int) -> Sequence[ClosingAccountPair]:
    """Cặp TK kết chuyển cuối kỳ của một gói, theo đúng thứ tự chạy (FR-SYS-023).

    Sắp theo `sequence` — bút toán kết chuyển doanh thu/chi phí phải chạy trước
    bút toán kết chuyển 911 sang 421, nếu không dòng sau đọc một số dư 911 chưa
    đầy đủ (xem docstring `ClosingAccountPair`).

    **Bất biến cho engine kết chuyển (chốt 2026-08-19):** `source_account`/
    `target_account` được phép là TK **tổng hợp** (642, 821, 421, …) — nghĩa là
    "kết chuyển cả nhánh con". Consumer PHẢI bung xuống các TK con hạch-toán-được
    (không `is_summary`) trước khi lập bút toán, vì validator ghi sổ từ chối
    dòng đâm thẳng vào TK tổng hợp (BR-SYS-03). Cấu hình đứng ở mức thông tư;
    việc bung cây là của engine, không phải của dữ liệu gói.
    """
    return (
        session.execute(
            select(ClosingAccountPair)
            .where(ClosingAccountPair.package_id == package_id)
            .order_by(ClosingAccountPair.sequence)
        )
        .scalars()
        .all()
    )
