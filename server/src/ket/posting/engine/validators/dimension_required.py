"""Chiều bắt buộc theo tài khoản (FR-SYS-021, BR-SYS-04 — validator #5 và #6).

Hai nguồn luật, một phép kiểm:

* `chart_of_accounts.detail_tracking` — TK bật theo dõi chi tiết thì dòng phải
  điền chiều tương ứng (đúng **loại** đối tác, xem `PostingDimensions`).
* `analysis_dimensions` có `is_required` — chiều mở rộng bắt buộc khi số hiệu
  TK khớp một **tiền tố** trong `applies_to_accounts` (`'641'` phủ `6411`);
  `NULL` nghĩa là mọi TK. Việc *ép* này phase 3 đã hoãn về đúng chỗ này
  (docstring `dimensions/models.py`): giờ có dòng hạch toán thật để ép.

Giá trị chiều mở rộng nguồn `list` được kiểm tồn tại (một bảng, một truy vấn).
Giá trị nguồn `master` chỉ kiểm **có điền** — đích của nó là một trong hai mươi
bảng danh mục tùy `master_slug`, và mở registry danh mục từ trong posting engine
là kéo cả tầng master-data vào đường ghi sổ; tầng API của module (nơi đã biết
danh mục nào) là chỗ kiểm tồn tại trước khi dựng request.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.accounts_models import ChartOfAccount
from ket.kernel.dimensions.models import (
    AnalysisDimension,
    AnalysisDimensionValue,
    DimensionValueSource,
)
from ket.kernel.errors import PostingViolation
from ket.posting.engine.prepared import PreparedLine


def check_required_dimensions(
    session: Session,
    *,
    lines: list[PreparedLine],
    accounts: dict[int, ChartOfAccount],
) -> list[PostingViolation]:
    violations: list[PostingViolation] = []

    required_dimensions = (
        session.execute(
            select(AnalysisDimension)
            .where(AnalysisDimension.is_required.is_(True))
            .where(AnalysisDimension.is_active.is_(True))
        )
        .scalars()
        .all()
    )

    for line in lines:
        account = accounts.get(line.source.account_id)
        if account is None:
            # Dòng trỏ TK không tồn tại đã có vi phạm riêng ở `account.py`;
            # hỏi tiếp "thiếu chiều nào" trên một TK không có là thêm nhiễu.
            continue

        for tracking in account.detail_tracking or []:
            if not line.source.dimensions.has_tracking(tracking):
                violations.append(
                    PostingViolation(
                        "dimension.missing",
                        "Tài khoản bật theo dõi chi tiết — dòng phải điền chiều này",
                        ledger=line.ledger,
                        line_no=line.line_no,
                        account=account.code,
                        tracking=tracking,
                    )
                )

        provided = {value.dimension_id for value in line.source.dimensions.extended}
        for dimension in required_dimensions:
            if not _applies_to(dimension, account.code):
                continue
            if dimension.id not in provided:
                violations.append(
                    PostingViolation(
                        "dimension.extended_missing",
                        "Chiều phân tích bắt buộc với tài khoản này chưa được điền",
                        ledger=line.ledger,
                        line_no=line.line_no,
                        account=account.code,
                        dimension=dimension.code,
                    )
                )

    violations.extend(_check_list_values_exist(session, lines))
    return violations


def _applies_to(dimension: AnalysisDimension, account_code: str) -> bool:
    prefixes = dimension.applies_to_accounts
    if prefixes is None:
        return True
    return any(account_code.startswith(prefix) for prefix in prefixes)


def _check_list_values_exist(session: Session, lines: list[PreparedLine]) -> list[PostingViolation]:
    """Giá trị chiều nguồn `list` phải là một dòng thật của đúng chiều đó.

    Một truy vấn cho cả chứng từ: gom mọi cặp `(dimension_id, value_id)` rồi
    đối chiếu một lượt — không truy vấn theo từng dòng định khoản.
    """
    pairs = {
        (value.dimension_id, value.value_id)
        for line in lines
        for value in line.source.dimensions.extended
    }
    if not pairs:
        return []

    list_dimension_ids = set(
        session.execute(
            select(AnalysisDimension.id)
            .where(AnalysisDimension.id.in_({dimension_id for dimension_id, _ in pairs}))
            # Nguồn `master` kiểm ở tầng API của module — xem docstring đầu tệp.
            .where(AnalysisDimension.value_source == DimensionValueSource.LIST)
        )
        .scalars()
        .all()
    )
    checkable = {pair for pair in pairs if pair[0] in list_dimension_ids}
    if not checkable:
        return []

    existing = set(
        session.execute(
            select(AnalysisDimensionValue.dimension_id, AnalysisDimensionValue.id).where(
                AnalysisDimensionValue.dimension_id.in_(
                    {dimension_id for dimension_id, _ in checkable}
                )
            )
        ).all()
    )

    violations: list[PostingViolation] = []
    for line in lines:
        for value in line.source.dimensions.extended:
            pair = (value.dimension_id, value.value_id)
            if pair in checkable and pair not in existing:
                violations.append(
                    PostingViolation(
                        "dimension.extended_value_unknown",
                        "Giá trị chiều phân tích không tồn tại",
                        ledger=line.ledger,
                        line_no=line.line_no,
                        dimension_id=value.dimension_id,
                        value_id=value.value_id,
                    )
                )
    return violations
