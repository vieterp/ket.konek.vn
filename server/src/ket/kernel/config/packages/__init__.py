"""Máy móc quanh gói cấu hình pháp lý (lát 5A): nạp, gieo, kích hoạt, nhập gói ký số.

`accounts_models.ConfigPackage`/`ChartOfAccount` (từ lát 4A, migration `0009`)
là hình dạng bảng; gói này là mọi thứ vận hành quanh nó — `loader` đọc + kiểm
hợp đồng 4 tệp, `seed` gieo gói dựng sẵn lúc cấp dữ liệu kế toán, `activator`
kích hoạt có kiểm soát (FR-SYS-004), `signature_verifier`/`importer` nhập gói
`.zip` đã ký của bên thứ ba (RT-07).
"""

from __future__ import annotations
