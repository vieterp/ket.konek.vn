"""Header của một lượt tải tệp về — dùng chung cho mọi cửa trả tệp.

Ở đây chứ không ở một router: lát 7E-3 dựng cửa tải bản thể hiện hóa đơn và
viết lại một bản `Content-Disposition` **yếu hơn** ngay cạnh bản đúng đã có ở
`routers/attachments.py`. Hậu quả đo được: tên tệp tiếng Việt do nhà cung cấp
đặt làm cả lượt tải đổ `500` (Starlette mã hóa header bằng latin-1), và vì tệp
đã cất xong trước khi dựng phản hồi nên nó đổ **mãi mãi** cho tờ hóa đơn ấy.

Một bản duy nhất, ở tầng `api` vì đây là tri thức về HTTP chứ không về nghiệp vụ.
"""

from __future__ import annotations

from urllib.parse import quote


def content_disposition(file_name: str) -> str:
    """Header buộc tải về, mang được tên tiếng Việt.

    Hai dạng tên trong cùng một header là cố ý: `filename=` ASCII cho client cũ,
    `filename*=UTF-8''…` cho tên có dấu. Bỏ dạng thứ hai thì "Hợp đồng số 12.pdf"
    tải về thành một chuỗi ký tự hỏng.
    """
    # Bỏ `"` và `\` khỏi dạng ASCII: cả hai đều là ký tự **cấu trúc** của giá
    # trị trong ngoặc kép, nên một tên tệp chứa chúng sẽ cắt header thành hai
    # tham số mà trình duyệt mỗi bên đọc một kiểu. Dạng `filename*` bên dưới
    # mang tên đầy đủ nên không mất thông tin gì.
    ascii_fallback = (
        file_name.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "")
        or "attachment"
    )
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"
