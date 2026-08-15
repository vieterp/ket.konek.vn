#!/usr/bin/env python3
"""Năm nơi khai phiên bản phải khớp nhau — nếu không, bản phát hành nói dối.

Konek Két phát hành **một** bản cho cả server lẫn client (client và server bắt
tay theo phiên bản schema — LD-05), nên "phiên bản phần mềm" là một số duy nhất
xuất hiện ở năm chỗ:

    server/pyproject.toml            → tên wheel, metadata gói
    server/src/ket/__init__.py       → `/health`, log, tiêu đề OpenAPI
    client/package.json              → metadata gói web
    client/src-tauri/Cargo.toml      → phiên bản crate của shell
    client/src-tauri/tauri.conf.json → **tên bộ cài + phiên bản trong latest.json**

Lệch nhau thì hỏng theo kiểu khó truy: `.github/workflows/release.yml` lấy số từ
`tauri.conf.json`, nên một bản `0.3.0` có thể được phát hành trong khi
`/health` vẫn báo `0.2.0` — người hỗ trợ khách hàng đọc số sai, và bản vá đi
nhầm máy.

Cố tình **không** chọn một tệp làm "nguồn chuẩn" rồi sinh ra bốn tệp kia: sinh
mã tự động cần thêm một bước dựng mà ai cũng có thể quên chạy. Kiểm tính nhất
quán rẻ hơn, và nó hỏng ngay trong PR chứ không phải lúc phát hành.

Chạy: `make version-check` (hoặc `python3 .github/scripts/check_version_consistency.py`).
Đặt trong `.github/` vì `scripts/` nằm trong `.gitignore` — một tệp mà CI bắt buộc
phải có thì không thể sống trong thư mục không được theo dõi.
Thoát 0 = khớp, 1 = lệch, 2 = không đọc được một tệp.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _from_toml(relative: str, *keys: str) -> str:
    data = tomllib.loads((REPO_ROOT / relative).read_text("utf-8"))
    for key in keys:
        data = data[key]
    if not isinstance(data, str):
        raise TypeError(f"{relative}: {'.'.join(keys)} không phải chuỗi")
    return data


def _from_json(relative: str, *keys: str) -> str:
    data = json.loads((REPO_ROOT / relative).read_text("utf-8"))
    for key in keys:
        data = data[key]
    if not isinstance(data, str):
        raise TypeError(f"{relative}: {'.'.join(keys)} không phải chuỗi")
    return data


def _from_python_dunder(relative: str) -> str:
    """Đọc `__version__` bằng regex thay vì `import`.

    Import gói `ket` sẽ kéo theo toàn bộ phụ thuộc runtime; script này chạy
    trong job CI nhẹ nhất, không có venv của server.
    """
    text = (REPO_ROOT / relative).read_text("utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise ValueError(f"{relative}: không tìm thấy __version__")
    return match.group(1)


def collect_versions() -> dict[str, str]:
    return {
        "server/pyproject.toml": _from_toml("server/pyproject.toml", "project", "version"),
        "server/src/ket/__init__.py": _from_python_dunder("server/src/ket/__init__.py"),
        "client/package.json": _from_json("client/package.json", "version"),
        "client/src-tauri/Cargo.toml": _from_toml(
            "client/src-tauri/Cargo.toml", "package", "version"
        ),
        "client/src-tauri/tauri.conf.json": _from_json(
            "client/src-tauri/tauri.conf.json", "version"
        ),
    }


def main() -> int:
    try:
        versions = collect_versions()
    except (OSError, KeyError, ValueError, TypeError) as error:
        print(f"Không đọc được phiên bản: {error}", file=sys.stderr)
        return 2

    distinct = set(versions.values())
    for path, version in versions.items():
        print(f"  {version:<12} {path}")

    if len(distinct) != 1:
        print(
            f"\nLỆCH PHIÊN BẢN: tìm thấy {sorted(distinct)}. "
            "Sửa cả năm tệp về cùng một số trước khi merge.",
            file=sys.stderr,
        )
        return 1

    print(f"\nPhiên bản nhất quán: {distinct.pop()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
