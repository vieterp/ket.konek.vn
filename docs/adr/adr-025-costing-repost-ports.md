---
adr: 025
title: "Ghi lại dòng phát sinh của chứng từ đang ghi sổ, và lời báo ngược giá vốn"
status: accepted
date: 2026-09-19
supersedes: []
related: [ADR-011, ADR-014, ADR-020, ADR-024]
---

# ADR-025: Ghi lại dòng phát sinh của chứng từ đang ghi sổ, và lời báo ngược giá vốn

## Context

Lát 8B dựng engine tính giá xuất kho (SRS 09 §3, bốn phương pháp). Thiết kế
"giá vốn tính sau" của 8A/ADR-024 cho phép phiếu xuất ghi sổ với **0 dòng sổ
cái**; engine tính xong thì bút toán giá vốn (Nợ 632/621… / Có 156/152…) phải
được **ghi thêm** vào chính chứng từ ấy — và ghi **lại** mỗi khi giá đổi (chèn
chứng từ lùi ngày, sắp xếp lại trong ngày). Hai đường có sẵn đều sai:

1. `PostingService.unpost` rồi `post` — chạy hook `after_unpost` của phiếu kho,
   hook ấy xóa `inventory_movements` vừa được tính giá và đánh dấu bẩn lại: engine
   tự phá kết quả của mình.
2. `UPDATE gl_postings` thẳng từ module kho — phá bất biến phase 4 (dòng sổ cái
   chỉ được chèn và xóa qua `PostingService`, luật phụ thuộc #3), và vì thế cũng
   phá lời hứa của `balance_recalc_queue` (dấu bẩn ghi cùng transaction ghi sổ).

Cùng lát, `sales_invoices.cogs_posted` (7C: "giá vốn đã ghi sổ chưa", BR-SAL-01
doanh thu – giá vốn cùng kỳ) cần được lật khi engine ghi xong — mà `inventory`
không được import `sales` (C3), và SQL chạm bảng của module khác chỉ là cách lách
`import-linter`.

`PostingService` nằm trong `ket.posting.contracts`, `InventoryLineSource` trong
`ket.kernel.protocols` — cả hai là bề mặt đóng băng ADR-020. Đây là lần mở đầu
tiên của `ket.posting.contracts` và lần thứ tư của `kernel.protocols`.

## Decision

1. **`PostingService.repost(request, *, user_id) -> Voucher`** — ghi lại dòng
   phát sinh của chứng từ **đang ở `DA_GHI_SO`**: kỳ phải mở (`FOR SHARE` như
   `post`), **cùng bộ validator** với `post` (cân, chiều phân tích, TK thuộc gói),
   DELETE toàn bộ `gl_postings` của chứng từ rồi INSERT bộ mới, đánh dấu số dư bẩn
   cho sổ cũ ∪ sổ mới. **Không** máy trạng thái, **không** hook, **không** guard
   (guard là câu hỏi cho người ghi sổ; một lượt ghi lại giá vốn không có ai để
   hỏi), `posted_at`/`posted_by` giữ nguyên. Chứng từ chưa ghi sổ → từ chối
   (`posting.repost_requires_posted`).
2. **Mapper là một**: `inventory.posting_mapper.build_posting_request` dùng cho
   cả lượt `post` đầu lẫn mọi lượt `repost` — dòng có cặp TK lấy số tiền từ
   `amount_fc` (nhập) hoặc từ movement **đã tính giá** (xuất, vế đi chuyển kho),
   cặp giá vốn phát bằng **đồng tiền hạch toán của năm, tỷ giá 1**: giá vốn là số
   sổ cái, không có nguyên tệ ở sổ kho; phiếu xuất USD sinh từ hóa đơn bán vẫn cân
   vì `check_balanced` cân theo từng `(sổ, tiền tệ)`.
3. **`InventoryLineSource.sync_cost_posted(session, voucher_id, *, posted: bool)
   -> None`** — lời báo ngược: engine gọi cho chứng từ nguồn của phiếu xuất sinh
   sau mỗi lượt ghi lại giá vốn, `posted=True` khi phiếu không còn dòng chờ giá,
   `False` khi giá vừa bị rút (gỡ lớp nhập duy nhất → dòng xuất về chờ, lượt
   `repost` rỗng xóa bút toán 632 — review 8B M-4). `sales` đồng bộ
   `cogs_posted`; `purchase` không làm gì (bút toán trả lại nằm trên hóa đơn).
   `sales.clear_after_unpost` vẫn tự hạ cờ khi phiếu sinh mất theo nguồn.
4. Cả hai thay đổi **cộng thêm** (chữ ký cũ nguyên vẹn); ảnh chụp
   `frozen_kernel_api.txt` cập nhật cùng ADR này theo đúng ba bước của ADR-020.

## Consequences

**Được:** engine tính giá có một đường ghi giá vốn hợp lệ, dùng lại toàn bộ bộ
kiểm và dấu bẩn số dư; "không UPDATE `gl_postings`" vẫn đúng; module kho không
biết bảng của module bán. `repost` cũng là đường cho mọi bút toán suy ra sau ghi
sổ về sau (khấu hao chạy lại 8E, phân bổ 9).

**Mất:** `repost` là DELETE + INSERT trọn chứng từ, kể cả khi chỉ một dòng đổi —
đơn giản và đúng bất biến, nhưng với hàng chục nghìn chứng từ mỗi lượt tính lại là
phần tốn nhất của job (spike 8B đo). Nếu trượt mốc, bước tiếp là `repost_many`
set-based trong cùng ADR, không phải mở đường UPDATE.

**Rủi ro còn lại:** ảnh chụp chỉ canh chữ ký — `repost` bỏ hook có chủ đích, và
ai gọi nó cho một loại chứng từ có hook `after_post` mang tác dụng phụ (sổ phụ
công nợ) phải tự bảo đảm tác dụng phụ ấy không cần chạy lại. Ở 8B chỉ NK/XK/CK gọi.
