"""Đo mốc hiệu năng nền của app server (bước 24 của phase 2).

Đây **không** phải benchmark của FR-NFR-040..044 — những ngưỡng đó nói về báo
cáo, khóa sổ và tính giá trên dữ liệu một năm, mà phase 2 chưa có dữ liệu nào để
tính. Cái đo ở đây là chi phí **nền**: một tiến trình mất bao lâu để sẵn sàng, và
một request tối giản tốn bao nhiêu trước khi có nghiệp vụ nào chạy.

Vì sao đo ngay bây giờ chứ không đợi phase 11: con số của phase 11 chỉ trả lời
được "có đạt ngưỡng không". Muốn trả lời "cái gì làm nó chậm đi" thì phải có mốc
từ lúc hệ còn rỗng — và mốc ấy không dựng lại được về sau, vì lúc đó nền đã mang
theo mọi thứ của mười phase.

Năm phép đo, và ranh giới của chúng nói thẳng ra:

* **nhập mô-đun** — `import ket.main` trong một tiến trình **mới**: nạp FastAPI,
  SQLAlchemy, Pydantic và toàn bộ cây model. Đây là phần lớn nhất của "bấm mở
  phần mềm tới lúc dùng được", và nó **không** nằm trong `create_app` — đo trong
  cùng tiến trình thì mọi mô-đun đã nạp sẵn, và con số 0,3 ms bên dưới sẽ nói
  dối về trải nghiệm khởi động.
* **dựng ứng dụng** — `create_app()`: nạp router, model, middleware. Không chạm DB.
* **khởi động** — `lifespan`: dựng pool, kiểm phiên bản cụm và phiên bản schema
  của **từng** dataset. Đây là phần người dùng chờ khi bấm mở phần mềm ở chế độ
  một máy.
* **`/health`** — đường request ngắn nhất có thật: middleware đầy đủ, không DB.
* **`/api/v1/system/handshake`** — endpoint client gọi ở màn hình đầu tiên.

Số đo lấy **trong tiến trình** (`TestClient`), nên nó không gồm mạng LAN và
không gồm uvicorn. Đó là sàn chứ không phải số người dùng cảm nhận — và đúng thứ
cần cho việc so sánh giữa hai lần đo.

Chạy:

    uv run python scripts/measure_baseline.py --requests 200
    uv run python scripts/measure_baseline.py --json          # để dán vào docs
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

# Chạy như script (không phải `-m`), nên `src/` chưa có trong `sys.path`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from ket import __version__
from ket.api.middleware.schema_version_gate import CLIENT_VERSION_HEADER
from ket.main import create_app
from ket.settings import Settings, get_settings

WARMUP_REQUESTS = 20
"""Lượt chạy nóng máy, bỏ khỏi thống kê.

Lượt đầu tiên của mỗi endpoint gánh phần dựng cây dependency và biên dịch
regex của router. Tính nó vào p95 sẽ cho ra một con số nói về lần gọi đầu chứ
không nói về nhịp làm việc — và nó sẽ dao động theo số lượt đo."""


@dataclass(frozen=True)
class Timing:
    """Thống kê một phép đo, mili giây."""

    label: str
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    def as_row(self) -> str:
        return (
            f"{self.label:<42} {self.samples:>6} {self.p50_ms:>10.2f} "
            f"{self.p95_ms:>10.2f} {self.max_ms:>10.2f}"
        )


def _measure(label: str, action: Callable[[], None], *, repeats: int, warmup: int) -> Timing:
    for _ in range(warmup):
        action()

    durations: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        action()
        durations.append((time.perf_counter() - started) * 1000)

    ordered = sorted(durations)
    # Percentile "nearest-rank": không nội suy giữa hai mẫu. Với số mẫu nhỏ, nội
    # suy tạo ra một giá trị **chưa từng đo được**, và đây là con số sẽ bị so
    # sánh với ngưỡng ở phase 11.
    rank = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
    return Timing(
        label=label,
        samples=len(ordered),
        p50_ms=statistics.median(ordered),
        p95_ms=ordered[rank],
        max_ms=ordered[-1],
    )


def _run_python(code: str) -> None:
    """Chạy một đoạn Python trong tiến trình con. Đoạn mã là hằng số trong tệp này."""
    subprocess.run(  # noqa: S603 - lệnh và mã đều là hằng số, không có dữ liệu ngoài
        [sys.executable, "-c", code], check=True, capture_output=True
    )


def _measure_import(repeats: int) -> Timing:
    """Thời gian nhập `ket.main`, đã **trừ** phần khởi động của trình thông dịch.

    Trừ đi vì con số này sẽ được so với những lát sau: nếu để nguyên, nó gánh cả
    thời gian khởi động Python — thứ đổi theo máy và theo phiên bản, không đổi
    theo mã của dự án.

    Tiến trình con chứ không `importlib.reload`: reload dùng lại mọi mô-đun phụ
    thuộc đã nạp, nên nó đo lại đúng thứ rẻ nhất và bỏ qua thứ đắt nhất.
    """
    interpreter = _sample(lambda: _run_python(""), repeats=repeats)
    with_app = _sample(lambda: _run_python("import ket.main"), repeats=repeats)
    baseline = statistics.median(interpreter)

    ordered = sorted(max(0.0, duration - baseline) for duration in with_app)
    rank = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
    return Timing(
        label="nhập mô-đun (đã trừ khởi động Python)",
        samples=len(ordered),
        p50_ms=statistics.median(ordered),
        p95_ms=ordered[rank],
        max_ms=ordered[-1],
    )


def _sample(action: Callable[[], None], *, repeats: int) -> list[float]:
    """Đo `repeats` lượt, trả về mili giây thô (chưa thống kê)."""
    durations: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        action()
        durations.append((time.perf_counter() - started) * 1000)
    return durations


def measure(settings: Settings, *, repeats: int) -> list[Timing]:
    """Chạy cả năm phép đo. Hai phép đầu không cần PostgreSQL, ba phép sau thì cần."""
    results = [
        # Tiến trình con nên đắt: 10 lượt đã đủ ổn định cho một phép đo mà độ
        # phân tán đến từ bộ nhớ đệm hệ tệp, không từ mã.
        _measure_import(min(repeats, 10)),
        _measure(
            "dựng ứng dụng (create_app, không DB)",
            lambda: (create_app(settings), None)[1],
            # Dựng ứng dụng tốn hàng chục mili giây và không phụ thuộc tải, nên
            # vài chục lượt là đủ; chạy đủ `repeats` chỉ kéo dài lượt đo.
            repeats=min(repeats, 20),
            warmup=1,
        ),
    ]

    app = create_app(settings)

    def startup() -> None:
        with TestClient(app):
            pass

    results.append(
        _measure(
            "khởi động (lifespan: pool + kiểm schema)",
            startup,
            repeats=min(repeats, 20),
            warmup=1,
        )
    )

    with TestClient(app, headers={CLIENT_VERSION_HEADER: __version__}) as client:
        for label, path in (
            ("GET /health (không chạm DB)", "/health"),
            ("GET /api/v1/system/handshake", "/api/v1/system/handshake"),
        ):

            def call(path: str = path) -> None:
                response = client.get(path)
                if response.status_code != 200:
                    raise RuntimeError(f"{path} trả {response.status_code}: {response.text}")

            results.append(_measure(label, call, repeats=repeats, warmup=WARMUP_REQUESTS))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=200, help="Số lượt đo mỗi endpoint")
    parser.add_argument("--json", action="store_true", help="In JSON thay vì bảng")
    arguments = parser.parse_args()

    # Hạ mức log xuống `WARNING`: mỗi request đo được sẽ sinh một dòng log, và
    # vài trăm dòng ấy vừa lẫn vào bảng kết quả vừa **tự nó tốn thời gian** —
    # tức là làm sai chính con số đang đo.
    settings = get_settings().model_copy(update={"log_level": "WARNING"})
    results = measure(settings, repeats=arguments.requests)

    if arguments.json:
        payload = json.dumps([asdict(timing) for timing in results], ensure_ascii=False, indent=2)
        sys.stdout.write(payload + "\n")
        return 0

    lines = [
        f"Konek Két {__version__} — mốc hiệu năng nền (trong tiến trình)",
        f"{'phép đo':<42} {'mẫu':>6} {'p50 ms':>10} {'p95 ms':>10} {'max ms':>10}",
        *(timing.as_row() for timing in results),
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
