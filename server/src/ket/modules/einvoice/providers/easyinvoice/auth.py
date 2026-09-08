"""Header xác thực EasyInvoice (SoftDreams).

Định dạng riêng của nhà cung cấp, không phải một chuẩn nào:

    Authorization: <chữ ký>:<nonce>:<timestamp>:<user>:<mật khẩu>:<mã số thuế>
    chữ ký = Base64(MD5("POST" + timestamp + nonce))

**MD5 ở đây không phải lựa chọn của ta.** Nó là thứ máy chủ nhà cung cấp kiểm,
nên đổi sang hàm băm khác nghĩa là mọi yêu cầu bị từ chối. Nó cũng không bảo vệ
bí mật nào: `nonce` và `timestamp` đều đi kèm trong chính header, nên "chữ ký"
này chỉ chứng minh yêu cầu được dựng đúng khuôn — mật khẩu thật nằm ngay cạnh nó
dưới dạng rõ. Điều đó dẫn tới hai hệ quả bắt buộc, ghi ở đây vì không chỗ nào
khác nói được:

* **luôn dùng HTTPS** — `base_url` dạng `http://` để lộ mật khẩu trên mạng LAN;
* **không bao giờ ghi header này vào log**. `client.py` ghi đường dẫn và mã lỗi,
  không ghi header.
"""

from __future__ import annotations

import base64
import hashlib
import time
import uuid


def build_header(*, username: str, password: str, tax_code: str) -> str:
    """Giá trị header `Authorization` cho một yêu cầu.

    `nonce` và `timestamp` sinh mới mỗi lượt gọi: máy chủ dùng cặp ấy để chống
    phát lại, nên tái dùng một giá trị đã gửi là tự đánh hỏng yêu cầu của mình.
    """
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time()))
    digest = hashlib.md5(f"POST{timestamp}{nonce}".encode(), usedforsecurity=False).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return f"{signature}:{nonce}:{timestamp}:{username}:{password}:{tax_code}"
