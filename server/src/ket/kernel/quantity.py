"""Số lượng và tỷ lệ quy đổi — hình dạng cột, chưa có phép làm tròn (H67).

Số lượng **không phải** số tiền, nên nó không dùng `money.MONEY_SCALE_*`: tiền
làm tròn tới đồng còn số lượng của một mã hàng bán theo mét hay theo lít cần lẻ
hơn thế nhiều, và tỷ lệ quy đổi thì được **nhân** vào trước khi làm tròn nên nó
phải rộng hơn cả hai — cùng lập luận đã áp cho `RATE_SCALE_DEFAULT` của tỷ giá.

Ở đây chỉ có hình dạng cột. Quy tắc làm tròn số lượng thuộc **phase 8**, nơi tính
giá xuất kho là phép làm tròn số lượng đầu tiên của cả hệ thống; khóa tùy chọn
`quantity.scale` (khuôn `money.scale`) khai ở đó, cạnh chỗ đọc nó. Khai bây giờ
là khai một tùy chọn không ai đọc, và một tùy chọn không ai đọc là một tùy chọn
không ai kiểm.

Lưu **rộng** rồi làm tròn lúc tính, đúng khuôn `money`: `NUMERIC(20,6)` chứa được
mọi cấu hình `quantity.scale` mà phase 8 có thể chọn, nên không cột nào phải
`ALTER` khi con số ấy được chốt — và `ALTER` một cột số lượng sau khi đã có tồn
kho là loại migration đau nhất.
"""

from __future__ import annotations

from typing import Final

QUANTITY_PRECISION: Final[int] = 20
"""Tổng số chữ số của một cột số lượng — 14 chữ số phần nguyên là đủ cho mọi đơn
vị đếm thực tế (nghìn tỷ đơn vị nhỏ nhất)."""

QUANTITY_SCALE: Final[int] = 6
"""Chữ số thập phân **lưu trữ** của số lượng và tỷ lệ quy đổi. Không phải chữ số
hiển thị, cũng không phải chữ số làm tròn khi ghi sổ (phase 8)."""
