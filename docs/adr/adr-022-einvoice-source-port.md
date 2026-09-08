---
adr: 022
title: "Cửa đọc chứng từ gốc của hóa đơn điện tử — Protocol EInvoiceSource"
status: accepted
date: 2026-09-08
supersedes: []
related: [ADR-004, ADR-013, ADR-015, ADR-020, ADR-021]
---

# ADR-022: Cửa đọc chứng từ gốc của hóa đơn điện tử — Protocol `EInvoiceSource`

## Context

Lát 7E-2 dựng adapter nhà cung cấp hóa đơn điện tử thật (EasyInvoice). Việc của
adapter là dựng bản XML theo NĐ123/TT78 rồi gửi đi, và bản XML ấy cần **toàn bộ
nội dung tờ hóa đơn**: người mua (mã, tên, mã số thuế, địa chỉ), từng dòng hàng
(tên, đơn vị tính, số lượng, đơn giá, chiết khấu, thuế suất, tiền thuế, thành
tiền), tổng trước thuế / thuế / tổng thanh toán, tiền tệ và tỷ giá.

Đối tác, vật tư và đơn vị tính nằm ở `kernel.master_data` — `modules.einvoice`
đọc thẳng được. Nhưng **dòng hóa đơn và các con số tổng nằm ở
`modules.sales`** (`sales_invoices`, `sales_invoice_lines`), và hợp đồng
`C3 — không module nghiệp vụ nào import module nghiệp vụ khác`
(`server/importlinter.ini`) cấm `einvoice` đọc chúng.

`modules.einvoice` cũng **không** thể suy nội dung ấy từ tầng dưới:
`posting.gl_postings` giữ số tiền và tài khoản, nhưng không giữ tên hàng, đơn vị
tính hay số lượng — đó là ba trường bắt buộc của mọi dòng trên tờ hóa đơn giá
trị gia tăng.

Lát 7D đã chốt `einvoices` **không mang một con số tiền nào**: tổng đọc thẳng từ
chứng từ gốc, nên BR-EIV-07 ("tổng tiền trên hóa đơn khớp chứng từ bán hàng
gốc") đúng theo cấu trúc thay vì theo một phép kiểm ai đó phải nhớ chạy. Quyết
định ấy để lại một câu hỏi chưa ai phải trả lời cho tới lát này: **đọc bằng
đường nào.**

## Decision

Thêm Protocol thứ bảy vào `kernel/protocols.py`: `EInvoiceSource`, kèm hai kiểu
dữ liệu `EInvoiceSourceDocument` và `EInvoiceSourceLine`. Module `sales` cài;
`modules.einvoice` gọi. Đăng ký **một bản cài duy nhất**, cùng luật với
`ArApSubledger` và `InventoryPosting`.

Đây là lần **thứ hai** mở API kernel đã đóng băng (ADR-020), sau ADR-021.

## Rationale

Bốn đường đã cân nhắc.

1. **Protocol trong kernel (chọn).** Đúng luật phụ thuộc #2 của plan, đúng khuôn
   sáu Protocol có sẵn, và giữ nguyên doctrine 7D: một nguồn sự thật duy nhất
   cho các con số tiền của tờ hóa đơn.

2. **Dựng XML ở tầng `api` rồi cất ảnh chụp.** Tầng `api` được phép import cả
   `sales` lẫn `einvoice`, nên không cần đụng kernel — và phác thảo plan có sẵn
   hai cột `payload JSONB` / `signed_xml BYTEA` cho đúng hình dạng này. Bỏ vì
   hai lẽ. Thứ nhất, `api/routers/setup.py` đã ghi doctrine BFF của dự án:
   *"Cái BFF này **không** làm: nó không ghi."* Dựng một chứng từ thuế là logic
   nghiệp vụ, không phải phép nối dữ liệu cho một màn hình. Thứ hai, ảnh chụp
   nhân đôi các con số tiền — đúng thứ 7D loại khỏi `einvoices` — và lập luận
   duy nhất bênh vực nó (dữ liệu có thể trôi giữa lúc cấp số và lúc gửi) **đã
   sai** kể từ 7D: `FR-EIV-035` khóa chứng từ gốc ngay khi hóa đơn phát hành, ba
   cửa sửa/xóa/bỏ ghi sổ đều bị chặn. Không có gì trôi được.

3. **Dời bảng hóa đơn bán sang `einvoice`.** Một chủ, hết vướng C3. Bỏ vì cùng
   lý do ADR-021 đã bỏ đường tương ứng: nó gộp hai phân hệ SRS khác nhau vào một
   module chỉ để né một luật phụ thuộc, và hóa đơn bán còn là nguồn của công nợ,
   kho, giá vốn — ba thứ chẳng liên quan gì tới hóa đơn điện tử.

4. **Đọc thẳng bảng của `sales` qua SQL thô.** Lách được `import-linter` vì
   không có câu `import` nào. Bỏ thẳng: nó biến một luật kiểm được bằng máy
   thành một luật chỉ người review mới thấy, và cái giá phải trả đúng vào lúc
   `sales` đổi cột.

Vì sao lượt khai trước ở phase 6 (RT-18) không có Protocol này: RT-18 liệt kê
theo **quan hệ ghi sổ** giữa các phân hệ — công nợ, kho, cam kết giao hàng, sổ
quỹ. Hóa đơn điện tử không ghi sổ gì cả (xem `modules/einvoice/__init__.py`: nó
không đăng ký loại chứng từ posting nào), nên nó không lọt vào lưới ấy. Đây là
một lỗ của lượt khai trước, nhìn thấy được ngay khi bắt tay dựng bản XML thật —
không phải một thay đổi ý định.

## Consequences

* `kernel/protocols.py` mở ra sửa lần thứ hai. Hai lần trong hai lát liên tiếp
  là một tín hiệu: lượt khai trước của RT-18 phủ **quan hệ ghi sổ**, còn quan hệ
  **đọc để dựng chứng từ ra ngoài** thì chưa. Phase 8/9 nên soi lại trục ấy
  trước khi bắt đầu (bản in, tờ khai thuế, bảng lương đều đọc chéo phân hệ).
* `sales` mang thêm một bản cài Protocol, cạnh `ReceivableProvider`,
  `SettlementTargetSource` và `CommitmentProvider` nó đã cài.
* `einvoice` **không** phụ thuộc `sales` ở tầng import: bản cài vắng mặt là
  trạng thái hợp lệ — hóa đơn điện tử lập từ một loại chứng từ mà không phân hệ
  nào cài nguồn thì bị từ chối **rõ ràng**, chứ không dựng ra một bản XML thiếu
  dòng nào.
* Chữ ký của Protocol này chốt theo người dùng thật đầu tiên của nó (bộ dựng XML
  EasyInvoice ở chính lát 7E-2), đúng cách sáu Protocol trước đã chốt.
