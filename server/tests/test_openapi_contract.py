"""Đặc tả OpenAPI là **hợp đồng** với client, nên nó có cổng riêng (bước 14).

Client sinh type TypeScript từ tệp `client/packages/api-types/openapi.json` đã
commit. Hai thứ có thể trôi, và cả hai đều hỏng âm thầm:

* Tệp đã commit **cũ hơn** mã server → client build xanh với hợp đồng của tuần
  trước, rồi lỗi lúc chạy ở nơi cài đặt.
* Một endpoint mới không khai được hình dạng lỗi → client bắt lỗi bằng `any`,
  tức là phần hay xảy ra nhất của API lại là phần duy nhất không có kiểu.

Test này canh cả hai, và nó chạy **không cần PostgreSQL** — cùng lý do đường ống
sinh type chạy được trong job CI không có DB.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_SPEC = SERVER_ROOT.parent / "client" / "packages" / "api-types" / "openapi.json"

sys.path.insert(0, str(SERVER_ROOT / "scripts"))

from export_openapi import export  # noqa: E402 — sau khi nối sys.path


def _fresh_spec(tmp_path: Path) -> dict[str, Any]:
    return export(tmp_path / "openapi.json")


def test_the_committed_spec_matches_the_current_server(tmp_path: Path) -> None:
    """Tệp đặc tả đã commit phải là bản sinh từ mã đang có.

    Đỏ ở đây nghĩa là: đã đổi endpoint/model mà chưa chạy `make api-types`.
    Cách sửa nằm luôn trong thông điệp — người gặp nó lần đầu không phải đi tìm.
    """
    assert COMMITTED_SPEC.exists(), f"thiếu {COMMITTED_SPEC} — chạy `make api-types`"
    committed = json.loads(COMMITTED_SPEC.read_text(encoding="utf-8"))

    fresh = _fresh_spec(tmp_path)

    assert committed == fresh, (
        "Đặc tả OpenAPI đã commit không khớp mã server hiện tại. "
        "Chạy `make api-types` rồi commit thư mục client/packages/api-types."
    )


def test_every_operation_declares_the_rfc7807_error_shape(tmp_path: Path) -> None:
    """Mọi endpoint phải khai thân lỗi chung (FR-NFR-050).

    Khai ở tầng app (`create_app`) chứ không từng route, nên test này thực chất
    canh việc **không ai gỡ** khai báo đó — và bắt cả trường hợp một router mới
    được gắn bằng đường vòng nào đó bỏ qua cấu hình chung.
    """
    spec = _fresh_spec(tmp_path)
    methods = {"get", "post", "put", "patch", "delete"}

    missing: list[str] = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if method not in methods:
                continue
            responses = operation.get("responses", {})
            schema = (
                responses.get("default", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            if "ProblemDetails" not in json.dumps(schema):
                missing.append(f"{method.upper()} {path}")

    assert not missing, f"endpoint không khai hợp đồng lỗi RFC 7807: {missing}"


def test_the_job_queue_is_part_of_the_public_contract(tmp_path: Path) -> None:
    """Bốn đường của hàng đợi phải có mặt — client 2C dựng màn hình từ chúng."""
    spec = _fresh_spec(tmp_path)

    assert "post" in spec["paths"]["/api/v1/jobs"]
    assert "get" in spec["paths"]["/api/v1/jobs"]
    assert "get" in spec["paths"]["/api/v1/jobs/types"]
    assert "get" in spec["paths"]["/api/v1/jobs/{job_id}"]
    assert "post" in spec["paths"]["/api/v1/jobs/{job_id}/cancel"]
    assert "JobResponse" in spec["components"]["schemas"]


def test_enqueueing_a_job_is_202_in_the_contract_not_201(tmp_path: Path) -> None:
    """Mã trạng thái là một phần hợp đồng: `202` = đã nhận, chưa có kết quả.

    Client dựa vào đó để chuyển sang màn hình theo dõi tiến độ thay vì hiện
    "đã tạo xong".
    """
    spec = _fresh_spec(tmp_path)

    assert "202" in spec["paths"]["/api/v1/jobs"]["post"]["responses"]
    assert "201" not in spec["paths"]["/api/v1/jobs"]["post"]["responses"]
