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
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    create_model,
)

from ket.kernel.config.reports.models import LedgerScope
from ket.kernel.config.reports.spec import (
    PARAM_KIND_TYPES,
    ParamSetSpec,
    ParamSpec,
    coerce_param_value,
)
from ket.kernel.contracts import Ledger
from ket.kernel.errors import ReportParamsInvalidError

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


class _BaseReportParams(BaseModel):
    """Bộ tham số chuẩn — lớp nền tĩnh để mypy thấy kiểu từng trường; các
    tham số NGOÀI bộ chuẩn được `create_model` ghép thêm ở lớp con động."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_date: date
    to_date: date
    ledger: int = Field(
        default=int(Ledger.FINANCIAL), ge=int(Ledger.FINANCIAL), le=int(Ledger.MANAGEMENT)
    )
    branch_ids: tuple[int, ...] | None = None


def _extra_field(param: ParamSpec) -> tuple[Any, Any]:
    base = PARAM_KIND_TYPES[param.kind]
    if param.required:
        return (base, Field())
    return (base | None, Field(default=None))


def _same_value(sent: object, param: ParamSpec, pinned: object) -> bool:
    """Client có gửi ĐÚNG giá trị đã ghim không?

    Giá trị **sai kiểu** trả `False` chứ không `True` (review 6E-1 M-3): bản
    đầu trả `True` với lý do "để lượt kiểm chính báo lỗi kiểu", nhưng ngay sau
    đó `merged[name] = pinned` GHI ĐÈ giá trị rác nên lượt kiểm chính không bao
    giờ thấy nó — `direction=7` lặng lẽ thành `200`. Đó đúng là thứ doctrine
    "thắng TƯỜNG MINH" sinh ra để chặn: người gửi sai phải biết mình gửi sai.
    """
    try:
        return bool(TypeAdapter(PARAM_KIND_TYPES[param.kind]).validate_python(sent) == pinned)
    except ValidationError:
        return False


def _apply_fixed_params(
    raw: Mapping[str, object],
    *,
    spec: ParamSetSpec,
    fixed_params: Mapping[str, object],
) -> dict[str, object]:
    """Ghim giá trị tham số của definition vào đầu vào lượt render.

    Cùng doctrine "thắng TƯỜNG MINH" với `_apply_ledger_scope`: client gửi giá
    trị khác cho một tham số đã ghim là lỗi chứ không phải thứ bị lặng lẽ ghi
    đè — người dùng phải thấy được rằng thứ họ gửi không có tác dụng.
    """
    if not fixed_params:
        return dict(raw)
    declared = {param.name: param for param in spec.params}
    merged = dict(raw)
    for name, value in fixed_params.items():
        param = declared.get(name)
        if param is None:  # pragma: no cover - loader/seed đã chặn từ dữ liệu
            raise ReportParamsInvalidError(
                f"Báo cáo ghim tham số {name!r} không khai trong bộ tham số", param=name
            )
        pinned = coerce_param_value(value, param=param, where="Tham số ghim của báo cáo")
        if (
            name in merged
            and merged[name] is not None
            and not _same_value(merged[name], param, pinned)
        ):
            raise ReportParamsInvalidError(
                f"Báo cáo này ghim cố định {param.label!r} — bỏ tham số {name!r}", param=name
            )
        merged[name] = pinned
    return merged


def validate_params(
    raw: Mapping[str, object],
    *,
    spec: ParamSetSpec,
    ledger_scope: str,
    fixed_params: Mapping[str, object] = MappingProxyType({}),
) -> BoundParams:
    """Kiểm và ràng tham số một lượt render (FR-RPT-002, BR-RPT-04)."""
    raw = _apply_fixed_params(raw, spec=spec, fixed_params=fixed_params)
    fields: dict[str, Any] = {param.name: _extra_field(param) for param in spec.params}
    model_type = create_model("ReportParams", __base__=_BaseReportParams, **fields)
    try:
        validated = model_type.model_validate(dict(raw))
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "(gốc)"
        raise ReportParamsInvalidError(
            f"Tham số báo cáo không hợp lệ tại {location}: {first['msg']}",
            param=location,
        ) from exc

    from_date = validated.from_date
    to_date = validated.to_date
    ledger = validated.ledger
    branch_ids = validated.branch_ids

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
