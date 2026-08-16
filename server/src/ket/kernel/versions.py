"""So sánh phiên bản phát hành (`MAJOR.MINOR.PATCH`).

Konek Két phát hành **một** số phiên bản cho cả server lẫn client — cùng con số
mà `.github/scripts/check_version_consistency.py` canh ở năm tệp. Cổng bắt tay
(bước 19, LD-05) phải trả lời được đúng một câu hỏi từ con số đó: *bản client
đang gọi có cũ hơn bản tối thiểu mà server này chấp nhận không?*

Vì sao tự viết thay vì dùng `packaging.version`: so sánh ở đây chạy trên **mọi**
request ghi và đầu vào của nó là một header do bên ngoài gửi. Luật phải hẹp và
đọc hết được trong một màn hình — ba số nguyên, không hậu tố, không epoch,
không so sánh bản pre-release. Một chuỗi không khớp đúng khuôn đó là chuỗi
**không đánh giá được**, và cổng xử lý nó như bản không hợp lệ (H2) chứ không cố
đoán ý.

`float` không xuất hiện ở đây, cũng như mọi nơi khác trong `kernel` (ADR-015):
`"0.10.0"` lớn hơn `"0.9.0"`, còn `0.10 < 0.9`.
"""

from __future__ import annotations

import re
from typing import Final

VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$", re.ASCII)
r"""Đúng ba nhóm số, không tiền tố `v`, không hậu tố `-beta`/`+build`.

Trần bốn chữ số cho mỗi nhóm để một header dài bất thường không biến thành một
phép chuyển đổi số nguyên tùy ý — đầu vào ở đây đến từ ngoài.

`re.ASCII` vì `\d` của Python mặc định là **Unicode**: không có cờ này thì
`٠.٦.٠` (chữ số Ả Rập-Ấn) được nhận, trong khi bản TypeScript ở
`client/src/lib/app-version.ts` chỉ nhận ASCII. Hai bên phải kết luận giống nhau
— đó là toàn bộ lý do hai bản cùng tồn tại.
"""

Version = tuple[int, int, int]


def parse_version(raw: str) -> Version | None:
    """`"1.2.3"` → `(1, 2, 3)`. Chuỗi không đúng khuôn → `None`.

    Trả `None` chứ không ném: chỗ gọi duy nhất là cổng phiên bản, và ở đó "không
    đọc được" và "quá cũ" dẫn tới **cùng một** phản hồi. Một ngoại lệ ở giữa
    middleware chỉ thêm một nhánh phải bắt mà không thêm thông tin nào.
    """
    match = VERSION_PATTERN.match(raw.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def parse_required_version(raw: str) -> Version:
    """Như trên, nhưng cho giá trị **của chính bản cài** — sai thì hỏng ngay.

    Dùng cho `Settings.minimum_client_version`: đó là cấu hình do người triển
    khai đặt, và một giá trị gõ sai ở đó phải chặn tiến trình khởi động. Hướng
    hỏng ngược lại — im lặng coi như "không giới hạn" — biến cổng phiên bản
    thành một dòng cấu hình không làm gì cả.
    """
    parsed = parse_version(raw)
    if parsed is None:
        raise ValueError(f"Phiên bản {raw!r} không đúng khuôn MAJOR.MINOR.PATCH (ví dụ: 0.6.0)")
    return parsed
