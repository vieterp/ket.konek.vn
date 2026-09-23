---
adr: 028
title: "Registry chứng từ của posting trả mã theo module, cho câu hỏi liên phân hệ"
status: accepted
date: 2026-09-23
supersedes: []
related: [ADR-004, ADR-020, ADR-024]
---

# ADR-028: Registry chứng từ của posting trả mã theo module

## Context

Lát 8D cài `CommitmentProvider` (cột **"Có thể bán"**, U7) trong module `sales`.
"Đã hứa giao" = hóa đơn bán **chưa rời kho**, và một trong hai vế của định nghĩa
ấy là *"hóa đơn đã ghi sổ mà **chưa có phiếu kho nào sinh ra từ nó**"* — đúng
tập nhóm `chua-xuat-kho` mà BFF việc-còn-thiếu đã dùng từ 8A.

Câu hỏi "đã có phiếu kho sinh ra chưa" cần biết **mã loại chứng từ của phân hệ
kho** (`NK`/`XK`/`CK`/`LR`/`TD`). Ba đường đi tới nó, hai đường sai:

* `sales` import `ket.modules.inventory.models.INVENTORY_DOCUMENT_TYPES` — vi
  phạm thẳng luật phụ thuộc #1 (`import-linter` C3). Đây chính là thứ Protocol
  sinh ra để tránh.
* Chép danh sách mã vào `sales` — một bản sao sẽ lệch, và nó lệch **im lặng**:
  8C-2 thêm `LR`/`TD` mà không ai nhớ sửa bản sao thì cam kết chỉ sai đi một ít.
* Bỏ vế thứ hai của định nghĩa, chỉ đếm hóa đơn **đã cất chưa ghi sổ** — mất
  đúng những hóa đơn mà cầu mua/bán không dựng được phiếu kho, tức đúng những
  hóa đơn đáng lo nhất.

`POSTING_DOCUMENT_REGISTRY` (tầng `posting`, ADR-020) **đã** giữ đúng dữ liệu
ấy: mỗi `PostingDocumentType` khai `permission_module` của module làm chủ nó, và
`ket.modules.*` được phép import `ket.posting` (C2). Nó chỉ chưa có đường đọc
theo module.

## Decision

Thêm **một** phương thức đọc, cộng thêm, vào `PostingDocumentRegistry`:

```python
def codes_of_module(self, permission_module: str) -> tuple[str, ...]
```

Trả mã loại chứng từ mà một module nghiệp vụ làm chủ, sắp xếp ổn định; module
chưa được nạp trong tiến trình thì trả rỗng — đúng nghĩa "bản cài này không có
phân hệ ấy", và người gọi (`sales.commitment`) xử lý rỗng như "chưa có phiếu kho
nào tồn tại được", tức mọi hóa đơn đều còn là cam kết.

Bề mặt đóng băng của `ket.posting` (ADR-020) vì thế **rộng thêm một phương
thức**; không chữ ký nào đổi, không hành vi nào của đường ghi sổ đổi.

## Consequences

* `sales` hỏi được "phân hệ kho làm chủ những loại chứng từ nào" mà không biết
  `ket.modules.inventory` tồn tại; `import-linter` C3 giữ nguyên 5/5.
* Registry trở thành nguồn sự thật cho câu hỏi liên phân hệ dạng "chứng từ của
  module X" — phase sau thêm loại chứng từ kho là tự động đúng, không có bản sao
  nào phải nhớ sửa.
* Giá phải trả: một câu hỏi liên phân hệ nữa đi qua registry thay vì qua một
  Protocol có tên nghiệp vụ. Ranh giới: registry chỉ trả **siêu dữ liệu đăng
  ký** (mã, module, tiêu đề). Dữ liệu nghiệp vụ vẫn phải đi qua Protocol trong
  `kernel` — `codes_of_module` không mở đường cho `sales` đọc bảng của
  `inventory`.
* `permission_module` nay có hai vai: cấp mã quyền và nhận diện chủ sở hữu loại
  chứng từ. Hai vai trùng nhau trên mọi loại hiện có, và một loại chứng từ có
  chủ khác với module cấp quyền của nó sẽ là một bất thường đáng dừng lại xem —
  không phải một lý do tách cột.
