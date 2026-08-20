"""Kiểm tham số render: `param_set.spec` → model Pydantic động → `BoundParams`.

Ranh giới kiểu (LD-13): client gửi JSON tự do (`Mapping[str, object]`), nhưng
mọi thứ đi tiếp vào executor là `BoundParams` có kiểu — đã kiểm bắt buộc/kiểu/
tham số lạ (FR-RPT-002), đã áp `ledger_scope` của definition (BR-RPT-04), kèm
sẵn dòng thuật lại tham số cho tiêu đề bản in (BR-RPT-03).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from ket.kernel.config.reports.models import LedgerScope
from ket.kernel.config.reports.spec import ParamSetSpec, ParamSpec
from ket.kernel.contracts import Ledger
from ket.kernel.errors import ReportParamsInvalidError

_KIND_TYPES: dict[str, type] = {
    "date": date,
    "int": int,
    "text": str,
    "bool": bool,
    "decimal": Decimal,
}

_LEDGER_LABELS = {Ledger.FINANCIAL: "Sổ tài chính", Ledger.MANAGEMENT: "Sổ quản trị"}


@dataclass(frozen=True)
class BoundParams:
    """Tham số đã kiểm — thứ DUY NHẤT executor và header bản in được đọc."""

    from_date: date
    to_date: date
    ledger: int
    branch_ids: tuple[int, ...] | None
    extras: tuple[tuple[str, object], ...]
    """Tham số ngoài bộ chuẩn, theo thứ tự khai trong spec — giá trị đã đúng
    kiểu (`_KIND_TYPES`), `None` khi người dùng bỏ trống tham số tùy chọn."""

    echo_lines: tuple[str, ...]
    """Dòng thuật lại tham số in trên đầu báo cáo (BR-RPT-03)."""

    def sql_binds(self, allowed_params: tuple[str, ...] | list[str]) -> dict[str, object]:
        """Bind cho câu SQL đã bọc phạm vi: bộ chuẩn + extras trong `allowed_params`.

        `branch_ids`/`ledger` luôn có mặt — lớp bọc của `compose_scoped_query`
        cần chúng kể cả khi `sql_text` không nhắc tới.
        """
        binds: dict[str, object] = {
            "branch_ids": list(self.branch_ids) if self.branch_ids is not None else None,
            "ledger": self.ledger,
        }
        allowed = frozenset(allowed_params)
        if "from_date" in allowed:
            binds["from_date"] = self.from_date
        if "to_date" in allowed:
            binds["to_date"] = self.to_date
        for name, value in self.extras:
            if name in allowed:
                binds[name] = value
        return binds


def _base_fields() -> dict[str, tuple[type | object, object]]:
    return {
        "from_date": (date, Field()),
        "to_date": (date, Field()),
        "ledger": (
            int,
            Field(
                default=int(Ledger.FINANCIAL), ge=int(Ledger.FINANCIAL), le=int(Ledger.MANAGEMENT)
            ),
        ),
        "branch_ids": (tuple[int, ...] | None, Field(default=None)),
    }


def _extra_field(param: ParamSpec) -> tuple[type | object, object]:
    base = _KIND_TYPES[param.kind]
    if param.required:
        return (base, Field())
    return (base | None, Field(default=None))


def validate_params(
    raw: Mapping[str, object],
    *,
    spec: ParamSetSpec,
    ledger_scope: str,
) -> BoundParams:
    """Kiểm và ràng tham số một lượt render (FR-RPT-002, BR-RPT-04)."""
    fields = _base_fields()
    for param in spec.params:
        fields[param.name] = _extra_field(param)

    model_type: type[BaseModel] = create_model(  # type: ignore[call-overload]
        "ReportParams",
        __config__=ConfigDict(extra="forbid", frozen=True),
        **fields,
    )
    try:
        validated = model_type.model_validate(dict(raw))
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "(gốc)"
        raise ReportParamsInvalidError(
            f"Tham số báo cáo không hợp lệ tại {location}: {first['msg']}",
            param=location,
        ) from exc

    from_date: date = validated.from_date  # type: ignore[attr-defined]
    to_date: date = validated.to_date  # type: ignore[attr-defined]
    ledger: int = validated.ledger  # type: ignore[attr-defined]
    branch_ids: tuple[int, ...] | None = validated.branch_ids  # type: ignore[attr-defined]

    if from_date > to_date:
        raise ReportParamsInvalidError(
            "Khoảng thời gian ngược: `from_date` phải không muộn hơn `to_date`",
            param="from_date",
        )
    if branch_ids is not None and len(branch_ids) == 0:
        # Danh sách rỗng nghĩa là "không chi nhánh nào" — một báo cáo chắc chắn
        # trống. Không ai cố ý muốn thế; gần như luôn là client quên bỏ tham số.
        raise ReportParamsInvalidError(
            "`branch_ids` rỗng — bỏ hẳn tham số để xem mọi chi nhánh trong phạm vi",
            param="branch_ids",
        )

    ledger = _apply_ledger_scope(ledger, raw_had_ledger="ledger" in raw, scope=ledger_scope)

    extras = tuple((param.name, getattr(validated, param.name)) for param in spec.params)
    return BoundParams(
        from_date=from_date,
        to_date=to_date,
        ledger=ledger,
        branch_ids=branch_ids,
        extras=extras,
        echo_lines=_echo_lines(
            from_date=from_date,
            to_date=to_date,
            ledger=ledger,
            branch_ids=branch_ids,
            spec=spec,
            extras=extras,
        ),
    )


def _apply_ledger_scope(ledger: int, *, raw_had_ledger: bool, scope: str) -> int:
    """`ledger_scope` của definition thắng tham số (BR-RPT-04) — nhưng thắng
    TƯỜNG MINH: client gửi sổ mâu thuẫn với scope là lỗi, không phải thứ bị
    lặng lẽ ghi đè."""
    if scope == LedgerScope.BOTH:
        return ledger
    fixed = int(Ledger.FINANCIAL) if scope == LedgerScope.FINANCIAL else int(Ledger.MANAGEMENT)
    if raw_had_ledger and ledger != fixed:
        raise ReportParamsInvalidError(
            "Báo cáo này thuộc cố định một hệ thống sổ — bỏ tham số `ledger`",
            param="ledger",
        )
    return fixed


def _format_value(value: object) -> str:
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, bool):
        return "có" if value else "không"
    return str(value)


def _echo_lines(
    *,
    from_date: date,
    to_date: date,
    ledger: int,
    branch_ids: tuple[int, ...] | None,
    spec: ParamSetSpec,
    extras: tuple[tuple[str, object], ...],
) -> tuple[str, ...]:
    lines = [
        f"Từ ngày {from_date:%d/%m/%Y} đến ngày {to_date:%d/%m/%Y}",
        _LEDGER_LABELS[Ledger(ledger)],
    ]
    # Dòng "Chi nhánh: …" do engine thêm — nó có session để đổi id thành TÊN
    # chi nhánh (BR-RPT-03 in tham số cho người đọc, không phải khóa nội bộ).
    del branch_ids
    labels = {param.name: param.label for param in spec.params}
    for name, value in extras:
        if value is None:
            continue
        lines.append(f"{labels[name]}: {_format_value(value)}")
    return tuple(lines)


Format = Literal["pdf", "xlsx"]
