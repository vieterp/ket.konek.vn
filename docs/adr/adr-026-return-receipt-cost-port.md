---
adr: 026
title: "Hàng bán trả lại lấy giá lần xuất, và cổng chi tiết của số dư ban đầu"
status: accepted
date: 2026-09-19
supersedes: []
related: [ADR-014, ADR-020, ADR-024, ADR-025]
---

# ADR-026: Hàng bán trả lại lấy giá lần xuất, và cổng chi tiết của số dư ban đầu

## Context

Lát 8C-1 mang hai việc chạm bề mặt đóng băng ADR-020:

1. **FR-STK-004** — "tính lại giá phải cập nhật luôn đơn giá nhập kho của phiếu
   nhập hàng bán trả lại khi phiếu đó chọn *lấy từ giá xuất kho*". Chứng từ trả
   lại (`sales` kind `RETURN`) đối trừ hóa đơn gốc ở mức header (7C-2) — không
   có gì nói dòng nào trả về từ dòng bán nào, và phiếu nhập sinh từ nó "không
   giá" (8A). Module kho không được import `sales` (C3) để tự tra.
2. **RT-24 / FR-OPB-004** — tồn đầu kỳ theo từng lần nhập. Lớp đầu kỳ chỉ có
   nghĩa khi nằm trong **sổ kho** (`inventory_movements`) để engine 8B, guard
   tồn, lưới tồn và báo cáo đọc một nguồn; bảng ấy và bảng lô thuộc `inventory`,
   còn số dư ban đầu thuộc `posting` — và `posting` không được import module
   (C4). Đây là cùng thế của `LOCK_CHECKS` (8A) và `REFERENCE_GUARDS`.

`InventoryMovementLine` trong `kernel.protocols` mở lần thứ năm;
`ket.posting.contracts` mở lần thứ hai (lần đầu ADR-025).

## Decision

1. **`InventoryMovementLine.cost_from_line_id: UUID | None`** (cộng thêm) — dòng
   chứng từ **bán gốc** mà dòng nhập này trả về. `sales` điền từ cột mới
   `sales_invoice_lines.returned_line_id` (chỉ kind `RETURN`; dòng gốc thuộc hóa
   đơn bán hàng hóa/đại lý đã ghi sổ, cùng khách, cùng mã hàng). `inventory`
   tra phiếu XK sinh từ dòng gốc (`inventory_voucher_lines.source_line_id`) →
   movement xuất → `source_movement_id` của dòng nhập sinh: cột 8B "movement mà
   giá của dòng này lấy theo" nay có nghĩa ở **cả hai chiều** (xuất: lần nhập
   đích danh; nhập: lần xuất hàng quay về). Dòng gốc không kiêm xuất kho → dòng
   nhập chờ giá như 8B, không lỗi.
2. **Giá chép, không gõ.** Dòng nhập có nguồn xuất không nhận giá
   (`inventory.receipt_cost_conflicts_source`); nguồn phải là movement xuất
   cùng mã hàng, kho khác được (`inventory.return_source_mismatch`). Lúc ghi sổ,
   nguồn đã có giá → movement `COSTED` ngay và chứng từ được **`repost`**
   (ADR-025) trong cùng hook `after_post` để bút toán Nợ 156 / Có 632 có từ lượt
   ghi sổ; về sau `return_in_legs.sql` chạy mỗi vòng của engine (khuôn
   `transfer_in_legs.sql`): chép giá xuất sang giá nhập, `STALE` kéo khóa nhập
   vào vòng sau → tính lại giá bán **tự** cập nhật giá nhập trả lại, kể cả khác
   kho; khác năm thì `mark_return_next_year.sql` để dấu bẩn tại ngày nhập.
   NK vào tập chứng từ `repost` của engine (chỉ NK có dòng đổi giá mới vào
   `RETURNING`, NK gõ giá không bao giờ bị ghi lại oan).
3. **`OPENING_DETAIL_PORTS`** (`posting.opening_balances.ports`, xuất qua
   `posting.contracts`): registry `kind → OpeningDetailPort` với bốn việc
   `lot_id_for` / `clear` / `materialize` / `annotate_carried`. `posting` gọi
   `clear` **trước** khi xóa dòng cha (movement giữ FK `RESTRICT` về lớp),
   `materialize` sau khi chèn cha + lớp, `annotate_carried` sau lượt chuyển năm.
   `inventory` cài cho nhóm 5: mỗi lớp → một movement nhập đã có giá, ngày = ngày
   đầu năm − 1, kỳ = kỳ đầu năm, thứ tự trong ngày theo ngày nhập;
   `inventory_movements.voucher_id`/`line_id` NULL được, `opening_layer_id` +
   hai `CHECK` loại trừ. Bất biến: nhập tay nhóm 5 chỉ khi chi nhánh chưa có
   movement trước ngày đầu năm — năm sau `carry_forward` ghi dòng nhóm 5 (giá trị
   từ sổ cái, số lượng qua cổng) mà **không** sinh lớp/movement, vì engine đọc
   cả lịch sử (8B #7).
4. Ảnh chụp `frozen_kernel_api.txt` cập nhật cùng ADR này theo ba bước của ADR-020.

## Consequences

**Được:** FR-STK-004 đúng chữ "tự cập nhật" bằng chính cơ chế vòng/điểm bất động
của 8B — không job riêng, không cột giá gõ tay để lệch; tồn đầu kỳ đi qua đúng
một bảng sự thật nên không SQL nào của engine/guard/báo cáo phải `UNION` một
nguồn thứ hai; `posting` vẫn không biết module nào tồn tại.

**Mất:** lớp đầu kỳ dùng ngày "ngày đầu năm − 1", có thể rơi vào kỳ 12 của năm
trước nếu năm trước có kỳ mà chưa có sổ kho — báo cáo theo ngày của năm trước
sẽ thấy tồn đầu năm sau (bất biến "chưa có movement trước đầu năm" loại ca năm
trước có sổ kho). Chuyển năm gộp nhóm 5 theo `(TK, kho, mã hàng)` — mất lô ở dòng
sổ cái vì `gl_postings` không có cột lô; chi tiết lô vẫn ở sổ kho.

**Rủi ro còn lại:** `repost` trong `after_post` là DELETE + INSERT của chính
chứng từ vừa ghi sổ — hợp lệ vì trạng thái đã là `DA_GHI_SO` và hook này không
có tác dụng phụ nào khác cần chạy lại; ai thêm hook `after_post` cho phiếu kho
phải giữ nó idempotent với lượt ghi lại này.
