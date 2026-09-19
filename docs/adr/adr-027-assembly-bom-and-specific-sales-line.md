---
adr: 027
title: "Lắp ráp / tháo dỡ như một phiếu kho, định mức NVL, và lần nhập đích danh trên dòng bán"
status: accepted
date: 2026-09-19
supersedes: []
related: [ADR-010, ADR-014, ADR-020, ADR-024, ADR-025, ADR-026]
---

# ADR-027: Lắp ráp / tháo dỡ như một phiếu kho, định mức NVL, và lần nhập đích danh trên dòng bán

## Context

Lát 8C-2 mang ba việc, một trong đó chạm bề mặt đóng băng ADR-020:

1. **FR-STK-005/016** — lắp ráp / tháo dỡ theo định mức NVL: "xuất NVL theo định
   mức → nhập thành phẩm, giá thành phẩm = tổng giá trị NVL xuất; tháo dỡ: xuất
   thành phẩm → nhập linh kiện theo tỷ lệ phân bổ giá trị" (SRS 09 §2.3); và
   "lắp ráp nhiều vòng chỉ cần tính lại giá một lần, khác tháng thì tính lại cả
   hai tháng" (FR-STK-005). Engine 8B đã có cơ chế **vòng tới điểm bất động**
   cho chuyển kho chéo khóa (`transfer_in_legs.sql`) và trả lại hàng bán
   (`return_in_legs.sql`, ADR-026).
2. **FR-SYS-044** — định mức NVL trên danh mục VTHH: `items` chưa có bảng con
   nào cho việc này; bốn bảng con hiện có cùng một khuôn (`item_units`, …).
3. **Đích danh trên dòng hóa đơn bán** — 8B để lại: phiếu xuất sinh từ hóa đơn
   bán năm đích danh chờ giá vĩnh viễn vì dòng bán không có chỗ chỉ lần nhập.
   `sales` không import `inventory` (C3), nên dòng bán chỉ có thể *mang* một
   id movement để module kho kiểm khi sinh phiếu.

`InventoryMovementLine` trong `kernel.protocols` mở lần thứ sáu.

## Decision

1. **Lắp ráp / tháo dỡ = hai `kind` mới của `inventory_vouchers`** (`ASSEMBLY=3`
   "LR", `DISASSEMBLY=4` "TD"), cùng bảng dòng: một phiếu = **một** dòng thành
   phẩm (`inventory_voucher_lines.is_product`, chỉ mục duy nhất một phần) + N
   dòng linh kiện (`allocation_ratio` bắt buộc ở phiếu tháo dỡ). Chiều movement
   của từng dòng do `models.line_issues_stock(kind, is_product)` quyết — một chỗ
   cho service, guard tồn và `_legs`. **Không** tách thành cặp XK + NK nối bằng
   khóa: gấp đôi chứng từ, thêm bảng liên kết và luật sửa/xóa theo cặp, mà chữ
   "hai tháng" của FR-STK-005 vẫn được cơ chế vòng phủ (xem 2).
2. **Giá vế nhập suy từ vế xuất, mỗi vòng của engine.** `assembly_in_legs.sql`:
   thành phẩm nhận Σ `amount` các vế xuất cùng phiếu (mọi vế phải có giá, không
   thì PENDING), `unit_cost = Σ / số lượng`. `disassembly_in_legs.sql`: mỗi
   linh kiện nhận `round(total × rᵢ / Σr)`, dòng có tỷ lệ **lớn nhất** nhận
   phần dư để Σ = total tới từng đồng (dồn dòng cuối có thể âm khi tỷ lệ dòng
   cuối tí hon — review 8C-2 L-1). Cả hai ghi `STALE` → khóa vế nhập vào tập `keys` của vòng sau,
   nên chuỗi lắp ráp nhiều vòng (A lắp từ linh kiện, B lắp từ A) hội tụ trong
   **một** lượt job; khác tháng cùng năm nằm trong horizon; thành phẩm dùng ở
   năm sau do `mark_next_year.sql` để dấu — không cần câu đánh dấu riêng vì mọi
   vế của một phiếu cùng ngày, **với điều kiện** câu ấy chạy **trước**
   `finalize.sql`: khóa thành phẩm chỉ vào lượt qua vế `STALE`, hạ STALE trước
   là mất dấu (review 8C-2 H-1 — lỗ có sẵn từ 8B với vế đến chuyển kho, nay
   sửa chung). LR/TD vào tập chứng từ `repost` (ADR-025). Năm đích danh: vế nhập
   LR/TD chỉ có giá sau lượt engine, nên phiếu xuất đích danh trỏ nó trước đó bị
   từ chối "lần nhập chưa có giá" — quy trình là lắp → tính giá → xuất.
3. **Bút toán nằm trên dòng linh kiện, dòng thành phẩm không định khoản**: linh
   kiện có thể ở 152/153/156 khác nhau nên một cặp TK cho cả phiếu không diễn
   đạt được; lắp ráp: vế xuất linh kiện mang Nợ TK kho thành phẩm / Có TK kho
   linh kiện; tháo dỡ: vế **nhập** linh kiện mang Nợ TK linh kiện / Có TK thành
   phẩm — mapper mở thêm nhánh "IN của phiếu TD" bên cạnh "OUT" và "IN có
   `source_movement_id`". Hai nghiệp vụ `lap-rap` / `thao-do` vào gói cấu hình
   với purpose trống (như `chuyen-kho-noi-bo`), **không** bump `version` gói:
   `seed._ensure_auto_posting_backfilled` chèn theo `document_type` mới, còn
   bump version lại tắt backfill.
4. **`item_bom_lines`** (kernel master_data, khuôn `item_units`): linh kiện + số
   lượng cho **một đơn vị chính** thành phẩm + `allocation_ratio`; UNIQUE cặp,
   CHECK khác nhau, dịch vụ chặn **vòng** bằng recursive CTE `UNION` khử trùng
   (dừng sau ≤ số mã hàng bước kể cả dữ liệu đã có vòng) dưới một khóa advisory
   cho mọi lượt ghi định mức của dataset (đọc-rồi-ghi đồng thời A→B ‖ B→A mới
   lọt vòng — review 8C-2 M-1); vòng ở danh mục = hai phiếu lắp ráp chéo không
   hội tụ ở engine. **Một cấp**: `explode` không
   nổ đệ quy — lắp thành phẩm trung gian là một phiếu khác. Phiếu **liệt kê rõ
   mọi dòng** và được lệch định mức; định mức là gợi ý (báo cáo so thực xuất
   với định mức cần thấy lệch). Hook gộp mã hàng: từ chối khi một mã là linh
   kiện của mã kia; trùng linh kiện → bản đích thắng; sau chuyển kiểm vòng.
5. **`InventoryMovementLine.source_movement_id: int | None`** (cộng thêm) —
   lần nhập đích danh dòng **xuất** lấy hàng; `sales` điền từ cột mới
   `sales_invoice_lines.source_movement_id` (BIGINT, không FK — cùng lối
   `warehouse_id`), chỉ trên chứng từ kiêm phiếu xuất kho; `inventory` kiểm
   như phiếu xuất gõ tay (movement nhập đã có giá, cùng chi nhánh / kho / mã
   hàng / lô). Năm đích danh mà dòng không chỉ → phiếu sinh **vẫn chờ giá**
   (luật 8B) — không chặn ghi sổ hóa đơn khi UI chưa có chỗ chọn (8G). Năm
   không đích danh mà dòng vẫn chỉ nguồn: nguồn được kiểm và chép sang phiếu
   xuất như phiếu gõ tay (engine bỏ qua, nhưng lần nhập ấy bị guard tham chiếu
   giữ không cho bỏ ghi sổ) — cùng luật với phiếu tay, UI 8G chỉ hiện ô chọn ở
   năm đích danh.
6. Ảnh chụp `frozen_kernel_api.txt` cập nhật cùng ADR này theo ba bước của ADR-020.

## Consequences

**Được:** FR-STK-005 "chỉ tính lại một lần" là hệ quả cấu trúc của điểm bất động,
không job riêng; lắp ráp / tháo dỡ dùng lại trọn bộ phiếu kho (đánh số, guard tồn,
khóa sổ, sổ kho, repost); định mức đi theo khuôn bảng con đã có nên API/UI/gộp
đều quen; đích danh trên dòng bán đóng lỗ "chờ giá mãi" của 8B mà `sales` vẫn
không biết sổ kho.

**Mất:** hai câu vế mới chạy **mỗi vòng** trên mọi chi nhánh, kể cả không có
LR/TD — đi từ `inventory_vouchers` theo chỉ mục `kind` nên trả rỗng rẻ, đo ở
spike. Lắp ráp chéo cùng ngày gõ lệch định mức (A cần B, B cần A) vẫn lọt danh
mục và chỉ bị chặn ở engine sau `MAX_PASSES` như chuyển kho chéo. Dòng phiếu
tháo dỡ chép `allocation_ratio` của định mức lúc lập — sửa định mức sau không
đổi phiếu cũ (có chủ đích, cùng luật `factor`).

**Rủi ro còn lại:** `sales_invoice_lines.source_movement_id` không FK — movement
bị gỡ (bỏ ghi sổ lần nhập) khi hóa đơn còn Đã cất thì lúc ghi sổ hóa đơn phiếu
sinh nhận `specific_source_mismatch`; thông điệp nêu dòng, người dùng chọn lại.
