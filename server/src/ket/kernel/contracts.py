"""Ranh giới công khai của `ket.kernel`.

Mọi thứ module khác được phép chạm phải khai ở đây (Protocol / Pydantic model
/ dataclass có kiểu). Cấm `dict[str, Any]` qua ranh giới module — ADR-015.
Đây cũng là đích neo cho `import-linter` (ADR-004).

Phase 1: cố ý rỗng. Lát 5C mang `Ledger` về đây: hai hệ thống sổ là từ vựng
chung của LD-07 — `posting` ghi theo nó, `reporting` đọc theo nó, mà C5 cấm
reporting import posting; một hằng số chép tay ở mỗi bên là chỗ chúng lệch
nhau. `posting.engine.models` re-export nên mọi import cũ giữ nguyên.

Lát 6A mang `PartnerKind` về đây với cùng lập luận: loại đối tác là từ vựng
của **mọi** bên chạm công nợ — dòng hạch toán (`posting`), Protocol công nợ
liên-module (`kernel.protocols`, RT-18), và các module tiền/mua/bán — mà
kernel không được import posting (luật phụ thuộc #5). `posting.engine.dimensions`
re-export nên mọi import cũ giữ nguyên.

Các Protocol liên-module (RT-18) nằm ở tệp cạnh đây, `kernel/protocols.py` —
tách tệp vì bên đó còn mang registry đăng-ký-lúc-khởi-động, không chỉ từ vựng.
"""

from __future__ import annotations

from enum import IntEnum


class Ledger(IntEnum):
    """Hai hệ thống sổ hoạt động song song (N3, LD-07, FR-NFR-031)."""

    FINANCIAL = 0
    MANAGEMENT = 1


class PartnerKind(IntEnum):
    """Loại đối tác trên dòng hạch toán và trong công nợ — `0 customer 1 vendor 2 employee`.

    Một cột `partner_id` + một cột loại, chứ không ba cột khóa riêng: TK 131
    theo dõi khách, 331 theo dõi nhà cung cấp, 141 theo dõi nhân viên — mỗi
    dòng chỉ có **một** đối tượng công nợ, và ba cột sẽ cho phép điền hai.
    """

    CUSTOMER = 0
    VENDOR = 1
    EMPLOYEE = 2
