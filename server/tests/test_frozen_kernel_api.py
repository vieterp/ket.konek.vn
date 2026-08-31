"""Ảnh chụp chữ ký API công khai của `ket.kernel` và `ket.posting` — bước 23.

**Đây là cổng của lượt "đóng băng kernel"** (phase-06 bước 23, RT-18). Phase 7
và 8 chạy SONG SONG sau phase 6, và ranh giới chia sẻ duy nhất giữa chúng là
hai gói này. Một chữ ký đổi lặng lẽ ở đây không làm gãy bản dựng của người đổi
— nó làm gãy nhánh kia, muộn, và ở chỗ không ai đang nhìn.

Cách nó chặn: kết xuất chữ ký thành văn bản rồi so với tệp
`frozen_kernel_api.txt` đã commit. Đổi chữ ký ⇒ CI đỏ ⇒ người đổi phải cập nhật
ảnh chụp **có chủ đích**, và (theo phase-06 bước 23) kèm một ADR bổ sung nói vì
sao. Cập nhật ảnh chụp:

    KET_UPDATE_FROZEN_API=1 uv run pytest tests/test_frozen_kernel_api.py

Phạm vi đóng băng là **bề mặt liên-module đã KHAI**, không phải mọi thứ import
được:

* `ket.kernel.protocols` — Protocol liên-module + registry của chúng. Đúng thứ
  RT-18 nói tới: phase 7/8 chỉ được *cài*, không được *sửa*.
* `ket.posting.contracts` — ranh giới công khai của `ket.posting`, tệp mà chính
  docstring của nó gọi là "mọi thứ module khác được phép chạm".

Tệp con bên trong hai gói KHÔNG bị đóng băng: đóng băng chúng là đóng băng cả
cách cài đặt, và lượt tái cấu trúc lành mạnh nào cũng sẽ đỏ. Luật ranh giới
(module chỉ đi qua hai tệp trên) do `import-linter` C1–C5 canh, không phải tệp
này.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from ket import model_registry as _model_registry  # noqa: F401  (nạp mọi bản cài)
from ket.kernel import protocols as kernel_protocols
from ket.posting import contracts as posting_contracts

SNAPSHOT_PATH: Final[Path] = Path(__file__).with_name("frozen_kernel_api.txt")

_FROZEN_MODULES: Final[tuple[ModuleType, ...]] = (kernel_protocols, posting_contracts)


def _render_callable(qualified: str, member: object) -> str:
    try:
        signature = inspect.signature(member)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - builtin không có chữ ký
        return f"{qualified}(...)"
    return f"{qualified}{signature}"


def _public_members(owner: object) -> list[tuple[str, object]]:
    return sorted(
        (name, member) for name, member in vars(owner).items() if not name.startswith("_")
    )


def _render_class(module_name: str, name: str, cls: type) -> list[str]:
    lines = [f"class {module_name}.{name}"]
    for member_name, member in _public_members(cls):
        if inspect.isfunction(member):
            lines.append(f"  {_render_callable(member_name, member)}")
        elif isinstance(member, property):
            lines.append(f"  property {member_name}")
    annotations = getattr(cls, "__annotations__", {})
    for field, annotation in sorted(annotations.items()):
        if not field.startswith("_"):
            lines.append(f"  field {field}: {annotation!s}")
    return lines


def _render_module(module: ModuleType) -> list[str]:
    """Chỉ tên khai trong `__all__` — tên import lại từ nơi khác là chi tiết cài
    đặt, và gộp chúng vào ảnh chụp biến mỗi lượt nâng pydantic thành một lượt
    "đổi API"."""
    exported = getattr(module, "__all__", None)
    assert exported is not None, (
        f"{module.__name__} phải khai `__all__` — ảnh chụp đóng băng chỉ chụp bề mặt "
        "ĐÃ KHAI; thiếu nó thì tên import lại (BaseModel, Session…) lọt vào API."
    )
    names = sorted(exported)
    lines = [f"# module {module.__name__}"]
    for name in names:
        member = getattr(module, name)
        if inspect.isclass(member):
            lines.extend(_render_class(module.__name__, name, member))
        elif inspect.isfunction(member):
            lines.append(_render_callable(f"{module.__name__}.{name}", member))
        else:
            lines.append(f"{module.__name__}.{name}: {type(member).__name__}")
    return lines


def render_frozen_api() -> str:
    blocks = ["\n".join(_render_module(module)) for module in _FROZEN_MODULES]
    return "\n\n".join(blocks) + "\n"


def test_the_public_kernel_and_posting_api_matches_the_frozen_snapshot() -> None:
    rendered = render_frozen_api()
    if os.environ.get("KET_UPDATE_FROZEN_API") == "1":
        SNAPSHOT_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip("đã ghi lại ảnh chụp API đóng băng")
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert rendered == expected, (
        "Chữ ký công khai của ket.kernel/ket.posting đã đổi so với ảnh chụp đóng băng "
        "(phase-06 bước 23). Nếu đây là thay đổi CÓ CHỦ ĐÍCH: viết ADR bổ sung, rồi "
        "chạy `KET_UPDATE_FROZEN_API=1 uv run pytest tests/test_frozen_kernel_api.py` "
        "và commit ảnh chụp mới cùng ADR."
    )


def test_the_snapshot_covers_the_protocols_phase_7_and_8_will_implement() -> None:
    """Bản neo cho bài trên: ảnh chụp phải THẬT SỰ chứa bề mặt RT-18 nói tới.

    Không có bài này thì một lượt `--snapshot-update` trên một ảnh chụp rỗng
    (hoặc một `_FROZEN_MODULES` bị cắt còn một phần tử) vẫn xanh mãi mãi.
    """
    rendered = render_frozen_api()
    for name in (
        "ReceivableProvider",
        "PayableProvider",
        "SettlementTargetSource",
        "InventoryPosting",
        "CommitmentProvider",
        "TreasurerCashBook",
        "TreasurerVoucherSource",
    ):
        assert f"class ket.kernel.protocols.{name}" in rendered, name
    for name in ("PostingService", "PostingRequest", "PostingDocumentType", "VoucherService"):
        assert f"class ket.posting.contracts.{name}" in rendered, name
