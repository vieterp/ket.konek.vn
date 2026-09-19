---
adr: 024
title: "Ba cửa kho của kernel — chiều gỡ, chiều đọc và chứng từ không dòng sổ cái"
status: accepted
date: 2026-09-19
supersedes: []
related: [ADR-004, ADR-013, ADR-015, ADR-020, ADR-021, ADR-022]
---

# ADR-024: Ba cửa kho của kernel — chiều gỡ, chiều đọc và chứng từ không dòng sổ cái

## Context

Lát 8A là người dùng thật đầu tiên của hai Protocol kho mà RT-18 khai sẵn ở
phase 6: `InventoryPosting` (chứng từ mua/bán sinh phiếu kho mà không import
module kho) và `CommitmentProvider`. Bắt tay vào cài, ba thiếu sót lộ ra — cùng
loại với thứ 7A gặp ở `ArApSubledger` (ADR-021) và 7E-2 gặp ở `EInvoiceSource`
(ADR-022): lượt khai trước liệt kê theo **quan hệ ghi sổ** nên phủ chiều ghi mà
bỏ trống chiều gỡ và chiều đọc chéo.

1. **Không có chiều gỡ.** `create_movement` sinh phiếu khi hóa đơn ghi sổ,
   nhưng khi hóa đơn bỏ ghi sổ thì không có cửa nào để gỡ phiếu ấy — đúng lỗ
   ADR-021 phải vá cho sổ phụ công nợ.
2. **Guard tồn kho không có chỗ kêu.** Phiếu xuất sinh từ hóa đơn bán được ghi
   sổ **bên trong** hook `after_post(session, voucher_id, user_id)` của hóa đơn,
   và hook không mang `acknowledged_warnings`. Cảnh báo "xuất quá tồn"
   (FR-STK-040, ba mức FR-SYS-062) chạy trên phiếu sinh sẽ kêu ở chỗ người dùng
   không xác nhận được; mức "cảnh báo" vì thế chặn vĩnh viễn đường ghi sổ hóa
   đơn bán kiêm phiếu xuất.
3. **Giá vốn tính sau.** Phiếu xuất ghi sổ trước khi có giá (engine tính giá
   xuất là lát 8B; bình quân cuối kỳ về bản chất chỉ có giá ở cuối kỳ), phiếu
   nhập từ hóa đơn mua không có bút toán riêng (Nợ 156 / Có 331 đã ở hóa đơn),
   chuyển kho nội bộ không đổi tổng giá trị. Cả ba là chứng từ **đã ghi sổ mà
   không có dòng `gl_postings`** — hình dạng mà engine chưa từng gặp:
   `executemany` với danh sách rỗng không phải "không làm gì", nó chèn một dòng
   mặc định và đổ ở RLS.

## Decision

Mở kernel lần thứ ba, mọi thay đổi **cộng thêm** (ADR-020 §đường đổi hợp lệ),
ảnh chụp `frozen_kernel_api.txt` cập nhật cùng ADR này:

* `InventoryPosting.remove_movement(session, *, source_voucher_id, user_id)` —
  chiều gỡ; hook `after_unpost` của mua/bán gọi. `create_movement` nhận thêm
  bốn tham số tùy chọn (`operation_code`, `partner_id`, `partner_kind`,
  `description`) để phiếu sinh in được và lưới lọc được.
* `InventoryMovementLine` thêm `debit_account_id`/`credit_account_id` (cặp giá
  vốn 632/156 nằm trên dòng hóa đơn bán — kho không được import `sales`),
  `amount_fc` (thành tiền là **số chủ**: giá trị sổ kho bằng giá trị hóa đơn
  cộng chi phí mua phân bổ tới từng đồng, BR-STK-03) và `source_line_id`.
* Protocol **đọc** `InventoryLineSource.planned_movement(session, voucher_id)
  -> PlannedMovement | None` — `sales`/`purchase` cài, guard tồn kho của
  `inventory` gọi. Mỗi module có đúng **một** hàm dịch dòng hóa đơn → dòng
  phiếu kho, dùng cho cả hook ghi lẫn bản cài đọc, nên guard và phiếu sinh không
  bao giờ nhìn hai bộ dòng khác nhau. Guard kêu **trên hóa đơn** (băng "Vẫn
  ghi sổ?" của 7H-1); phiếu sinh sau đó ghi sổ với `acknowledged_warnings=True`
  — hợp lệ vì cùng dòng đã qua cùng guard, và mức "chặn" thì đã chặn từ hóa đơn.
* `posting.contracts.LOCK_CHECKS` — sổ đăng ký mục kiểm khóa sổ do module đóng
  góp (`(session, period, year) -> None`, ném `PeriodLockChecklistError`).
  `inventory` đăng ký "kỳ không khóa được khi giá xuất chưa chốt" (BR-STK-05);
  phase 10a nối danh mục U11 vào đây. Cùng lý do tồn tại với
  `REFERENCE_GUARDS`: thứ chặn khóa kỳ nằm ở module giữ dữ liệu, mà `posting`
  không được import module (C4).
* `PostingService._insert_postings` rẽ nhánh tường minh khi không có dòng:
  chứng từ 0 dòng sổ cái là hợp lệ, `check_balanced` tính `zero_amount` theo
  từng sổ nên tập rỗng không phải vi phạm.

Ngoài kernel, cùng lát: phiếu sinh từ chứng từ nguồn **đứng yên** chừng nào
nguồn còn ghi sổ (`EDIT_GUARDS` + `REFERENCE_GUARDS`, không trạng thái —
`PostingService.unpost` đổi trạng thái nguồn trước khi hook gọi `remove_movement`
nên phép kiểm tự cho qua đúng lúc).

## Consequences

* Ba lần mở kernel liên tiếp (ADR-021/022/024) đều cùng một hình: chiều ghi
  được khai trước, chiều gỡ và chiều đọc chéo lộ ra khi có người gọi thật.
  Phase 9 (thuế, lương, giá thành) nên soi hai trục ấy **trước** khi bắt tay —
  tờ khai thuế và bảng lương đều là "đọc chéo để dựng một chứng từ đi ra".
* Chứng từ đã ghi sổ mà không có dòng sổ cái nay là trạng thái bình thường.
  Mọi lượt đọc "chứng từ đã ghi sổ ⇒ có phát sinh" (báo cáo, check toàn vẹn,
  bản in) phải coi tập rỗng là hợp lệ; engine 8B là nơi lấp giá vốn bằng
  repost, không phải UPDATE.
* `LOCK_CHECKS` chạy dưới phạm vi mọi chi nhánh (khóa sổ đòi thế từ 6G-2) —
  bảng có RLS mà module đọc trong mục kiểm (`inventory_recalc_queue`) vì thế
  được soi trọn.
* Cửa đọc `InventoryLineSource` là **danh sách** (như `EInvoiceSource`): mỗi
  bản cài trả lời về chứng từ của chính nó, `None` cho phần còn lại. Phase 9
  (lệnh sản xuất sinh phiếu xuất NVL) đăng ký thêm mà không sửa kernel.
