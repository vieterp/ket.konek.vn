"""Xuất đặc tả OpenAPI của app server ra tệp JSON (bước 14 của phase 2).

`client/packages/api-types` được sinh **từ tệp này** bằng `openapi-typescript`,
nên hợp đồng client↔server có đúng một nguồn: mã server (LD-03).

Chạy: `uv run python scripts/export_openapi.py <đường-dẫn.json>`
Hoặc, đủ cả hai bước (JSON → type TypeScript): `make api-types` ở gốc repo.

Không cần DB: `create_app` chỉ dựng router và model, còn cổng kiểm phiên bản
schema chạy trong `lifespan` — thứ chỉ khởi động khi có request thật. Đó là lý
do đường ống sinh type chạy được trong job CI không có PostgreSQL.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Chạy như script (không phải `-m`), nên `src/` chưa có trong `sys.path`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ket.main import create_app
from ket.settings import Settings


def export(destination: Path) -> dict[str, object]:
    """Ghi đặc tả OpenAPI ra `destination`, trả lại chính đặc tả đó."""
    settings = Settings(
        # Không đọc `.env` hay biến môi trường của máy đang chạy: đặc tả phải
        # giống hệt nhau trên máy lập trình và trên CI, nếu không cổng "type đã
        # sinh lại chưa" sẽ đỏ vì lý do không liên quan tới hợp đồng API.
        _env_file=None,
        verify_schema_on_startup=False,
        verify_postgres_version_on_startup=False,
    )
    spec = create_app(settings).openapi()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # `sort_keys` + xuống dòng cuối: tệp này được so sánh bằng `git diff` trong
    # CI, nên thứ tự khóa phải ổn định giữa hai lần chạy.
    destination.write_text(
        json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return spec


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        sys.stderr.write("Cách dùng: python scripts/export_openapi.py <đường-dẫn.json>\n")
        return 2
    destination = Path(args[0])
    export(destination)
    sys.stdout.write(f"Đã ghi đặc tả OpenAPI: {destination}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
