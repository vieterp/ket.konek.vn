---
adr: 023
title: "Khoản nợ nói được nó đến từ chứng từ nào — OpenInvoice.document_id"
status: accepted
date: 2026-09-11
supersedes: []
related: [ADR-020, ADR-021, ADR-022]
---

# ADR-023: Khoản nợ nói được nó đến từ chứng từ nào — `OpenInvoice.document_id`

## Context

Lát 7F-2a dựng đường thi hành cho nghiệp vụ **điều chỉnh** hóa đơn điện tử
(FR-EIV-033). Hóa đơn điện tử cố ý không mang cột tiền — nó đọc tổng từ chứng từ
gốc, và đó chính là cách BR-EIV-07 đúng theo **cấu trúc** (lát 7D) — nên một tờ
hóa đơn điều chỉnh, thứ chỉ khai **phần chênh**, cần một chứng từ bán của riêng
nó. Phân hệ bán hàng vì thế nhận hai loại chứng từ mới, `ADJUSTMENT_INCREASE` và
`ADJUSTMENT_DECREASE`, kèm cột `sales_invoices.adjusts_voucher_id` trỏ về chứng
từ được điều chỉnh.

`ADJUSTMENT_DECREASE` là loại chứng từ bán **đầu tiên mang hai đường trỏ về cùng
một "hóa đơn gốc"**:

1. `adjusts_voucher_id` trên thân chứng từ — thứ tờ hóa đơn điện tử đọc để biết
   nó điều chỉnh hóa đơn nào;
2. dòng đối trừ trong `sales_settlements` — thứ ghi giảm nợ trên sổ phụ công nợ.

Trả lại hàng và giảm giá hàng bán (`REVERSING_KINDS` từ 7C-2) chỉ có đường thứ
hai, nên câu hỏi này chưa từng đặt ra.

Bộ kiểm dùng chung `ket.posting.settlements.price_settlements` kiểm đối tác, chi
nhánh, đồng tiền và tài khoản công nợ của đích — **không** kiểm đích thuộc chứng
từ nào, và đúng như thế cho mọi người gọi có trước: một phiếu thu tất toán nhiều
hóa đơn một lượt là hình dạng bình thường.

Hệ quả với chứng từ điều chỉnh giảm, phát hiện ở vòng review pre-landing của lát:

```
D: kind = ADJUSTMENT_DECREASE, adjusts_voucher_id = V1,
   settlements = [ đích là dòng sổ phụ của V9 ]
```

lọt qua schema, qua ràng buộc bảng, và qua cả sáu điều kiện của
`einvoice.error_flow._verify_delta_voucher`. Kết quả: tờ hóa đơn điều chỉnh khai
với **cơ quan thuế** rằng hóa đơn phát hành trên V1 giảm 30 triệu, trong khi
**sổ công nợ** giảm dư nợ của V9. Sổ sách và lời khai thuế nói hai điều khác
nhau, và **không phép kiểm toàn vẹn nào thấy**: cả hai vế đều cân.

Ca sắc hơn: đối trừ vào một đích `OPENING_BALANCE`. Lúc ấy hai đường **chắc chắn**
lệch, vì `adjusts_voucher_id` bị khóa ngoại buộc vào `sales_invoices`.

Vấn đề là **hỏi câu ấy không được**. Hình dạng chung `OpenInvoice` — thứ mọi đích
đối trừ quy về — mang `target_id` (định danh **dòng sổ phụ**) nhưng không mang
định danh **chứng từ** đã sinh ra nó. Ba đường đã cân nhắc:

1. **`sales` đọc thẳng `ar_ap_ledger.document_id`.** Đỏ CI: `receivables` là chủ
   bảng ấy, và C3 cấm module nghiệp vụ import module nghiệp vụ khác.
2. **So theo số chứng từ** (`OpenInvoice.invoice_no` với `voucher_no` của chứng
   từ được điều chỉnh). Chạy được hôm nay: dãy `SAL` mang tiền tố `SAL{YY}-` và
   reset theo năm, nên số là duy nhất trong một chi nhánh qua các năm, mà cùng
   chi nhánh thì đã có điều kiện khác canh. Nhưng bảo đảm ấy tựa vào **dữ liệu
   người dùng sửa được**: đổi tiền tố dãy số là lặng lẽ làm yếu một bất biến kế
   toán, ở một chỗ không ai nghĩ là mình đang chạm tới.
3. **Khai thêm trường vào `OpenInvoice`.** Bề mặt ấy nằm trong ảnh chụp đóng băng
   `frozen_kernel_api.txt` (ADR-020), nên phải có ADR — chính văn bản này.

## Decision

**`OpenInvoice` khai thêm `document_id: UUID | None = None`** trong
`ket.kernel.protocols`, và ảnh chụp đóng băng cập nhật kèm ADR này.

1. **Trường, không phương thức.** `OpenInvoice` là hình dạng dữ liệu; câu hỏi
   "khoản nợ này đến từ chứng từ nào" là một thuộc tính của chính khoản nợ, mà
   mọi nguồn đều biết sẵn lúc dựng nó. Không nguồn nào phải tra thêm gì.

2. **`None` là câu trả lời ĐÚNG, không phải chỗ trống.** Số dư đầu kỳ khai tay
   (`OPENING_BALANCE`/`OPENING_ADVANCE`, `posting/opening_balances`) là khoản nợ
   có thật mà chứng từ sinh ra nó nằm ngoài sổ. Mặc định `None` cũng giữ cho
   delta của lượt mở đóng băng này đúng **một dòng**: không nguồn nào đang chạy
   phải sửa để tiếp tục biên dịch, và nguồn duy nhất có câu trả lời thật
   (`receivables`) điền nó bằng một dòng.

3. **Nơi gọi nào đòi trỏ đúng một chứng từ phải TỪ CHỐI `None`**, không bỏ qua.
   `price_settlements` nhận tham số tùy chọn `document_id`; đưa vào thì mọi đích
   phải khớp, và `document_id IS NULL` rơi vào nhánh từ chối — một khoản nợ
   không đến từ chứng từ nào thì chắc chắn không đến từ chứng từ bắt buộc. Mã vi
   phạm `settlement.document_mismatch`.

4. **Chỉ `ADJUSTMENT_DECREASE` truyền tham số ấy.** Mặc định `None` = không kiểm,
   đúng hình dạng của mọi lượt gọi có trước. Đây là chọn mặc định ở phía **không
   đổi hành vi cũ**, ngược với `settles_advance` của 7C-4 (chọn ở phía từ chối) —
   khác nhau vì ở đó một phân hệ quên khai sẽ ghi sổ phụ **sai chiều**, còn ở đây
   quên khai chỉ là không có thêm phép kiểm nào, đúng như trước lát này.

## Consequences

- Bề mặt đóng băng phase 6 mở lần thứ **hai** (lần thứ nhất: ADR-021). Cả hai
  lần đều là **thêm**, không đổi chữ ký nào sẵn có, và cả hai lần vì cùng một lý
  do: lượt khai trước RT-18 thiếu một câu hỏi mà chỉ lúc dùng thật mới lộ ra.
  C3 giữ nguyên; import-linter 5/5 xanh.
- `receivables` điền `document_id=row.document_id`; `posting/opening_balances`
  không điền và đó là câu trả lời đúng của nó.
- Trường này còn dùng được ngoài lát: đối chiếu hóa đơn ↔ doanh thu sổ cái và
  các báo cáo công nợ của 7G hỏi đúng câu hỏi ấy, và trước ADR này chúng sẽ phải
  tự nghĩ ra một đường vòng riêng.
- Vẫn còn một chỗ hở nhỏ đã biết: hai chứng từ điều chỉnh trỏ vòng vào nhau
  (`D1 → D2`, `D2 → D1`) biểu diễn được vì `does_not_adjust_itself` chỉ chặn
  vòng một bước. Vô hại hôm nay — không mã nào duyệt chuỗi ấy, phép kiểm là một
  phép so bằng — và chỉ đáng đóng khi có thứ đi dọc nó.
