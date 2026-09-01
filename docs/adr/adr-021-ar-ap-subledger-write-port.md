---
adr: 021
title: "Cửa ghi sổ phụ công nợ — Protocol ArApSubledger"
status: accepted
date: 2026-09-01
supersedes: []
related: [ADR-004, ADR-015, ADR-020]
---

# ADR-021: Cửa ghi sổ phụ công nợ — Protocol `ArApSubledger`

## Context

Phase 7 dựng sổ phụ công nợ `ar_ap_ledger`. Kế hoạch (`phase-07` §Architecture,
RT-18) chốt **module `receivables` là chủ sở hữu** bảng ấy: nó là sổ phụ *được
ghi vào*, không phải hạ tầng đọc-only, nên không nhét vào kernel.

Chủ sở hữu là `receivables`, nhưng **người sinh ra dòng** là `purchase` và
`sales`: ghi sổ một chứng từ mua sinh một khoản phải trả, ghi sổ một chứng từ
bán sinh một khoản phải thu, bỏ ghi sổ thì gỡ. Ba module ấy nằm trong hợp đồng
`C3 — không module nghiệp vụ nào import module nghiệp vụ khác`
(`server/importlinter.ini`), nên `purchase.service` gọi thẳng
`receivables.ledger_service` là **đỏ CI**, đúng như thiết kế.

Phase 6 (RT-18) đã khai trước năm Protocol liên-module để phase 7/8 chỉ *cài*
chứ không *sửa* kernel. Ba trong số đó phủ **chiều đọc và chiều đối trừ** của
công nợ:

- `ReceivableProvider` / `PayableProvider` — "đối tác này còn nợ gì";
- `SettlementTargetSource` — "phiếu thu/chi vừa trả vào chứng từ nào, cộng số
  đã trả vào đó".

Không cái nào phủ **chiều ghi**: tạo và gỡ chính dòng sổ phụ lúc chứng từ gốc
ghi sổ / bỏ ghi sổ. Đây là một lỗ trong lượt khai trước của RT-18 — nhìn thấy
được ngay khi bắt tay lát 7A, không phải một thay đổi ý định.

Bốn đường đã cân nhắc:

1. **Dời `purchase_invoices`/`sales_invoices` sang `receivables`.** Một chủ, hết
   vướng C3. Nhưng nó gộp ba phân hệ SRS khác nhau vào một module chỉ để né một
   luật import, và đặt logic giá/chiết khấu bán hàng cạnh sổ phụ công nợ.
2. **`receivables` gắn hook vào loại chứng từ của module khác.**
   `PostingDocumentType` đăng ký **một lần bởi chủ loại** và trùng mã thì ném
   (`posting/documents/registry.py`), nên module khác không chen hook vào được.
   Muốn làm phải thêm một danh sách observer chạy-sau-mọi-lượt-ghi-sổ vào
   registry — cũng là sửa API đóng băng, mà lại mơ hồ hơn: đọc mã ghi sổ không
   thấy được ai sẽ chạy.
3. **`receivables` suy dòng sổ phụ từ `gl_postings`.** Không cần Protocol nào.
   Nhưng `due_date`, số hóa đơn của người bán và điều khoản thanh toán **không
   có trên `gl_postings`** — suy ra được thì đã không cần sổ phụ.
4. **Khai một Protocol cho chiều ghi** — chọn cách này.

## Decision

1. Kernel khai thêm **`ArApSubledger`** trong `ket.kernel.protocols`, cùng khuôn
   với `InventoryPosting` ("cửa duy nhất để chứng từ mua/bán tạo phiếu kho —
   phase 7 gọi, phase 8 cài"). Ở đây: **phase 7 mua/bán gọi, `receivables` cài.**

   ```python
   class ArApSubledger(Protocol):
       def record(self, session, *, voucher_id, entries) -> None: ...
       def remove(self, session, *, voucher_id) -> None: ...
   ```

   Hình dạng dòng đi kèm là `SubledgerEntry` (Pydantic, `extra="forbid"`), khai
   cùng chỗ — ADR-015 cấm `dict[str, Any]` qua ranh giới module.

2. **Một bản cài duy nhất**, như `InventoryPosting` và `TreasurerCashBook`:
   `CrossModuleProviders.register_ar_ap_subledger` ném khi đăng ký trùng. Hai
   bản cài là hai nơi tranh nhau một bảng sổ phụ.

3. `SubledgerEntry` **không mang `branch_id`**: chi nhánh của khoản nợ là chi
   nhánh của chứng từ, và bản cài đọc nó từ `vouchers`. Cho người gọi truyền vào
   tạo ra một trạng thái biểu diễn được mà hệ thống không bao giờ đúng với nó —
   dòng sổ phụ chi nhánh B dưới chứng từ chi nhánh A. Nó không đối chiếu được
   với sổ cái (`gl_postings.branch_id` LUÔN lấy từ `vouchers.branch_id`; posting
   engine không có chiều chi nhánh theo dòng), và nó **vô hình với chính lượt bỏ
   ghi sổ của chứng từ đó**: guard chạy dưới RLS người gọi sẽ không thấy dòng B,
   im lặng cho qua, rồi `DELETE … WHERE document_id = …` cũng chỉ xóa được phần
   nhìn thấy — ghi sổ lại sinh bản thứ hai của dòng B. Bỏ trường đi thì trạng
   thái ấy không tồn tại để phải kiểm (review pre-landing 7A, C-2).

4. `record` **thay trọn theo `voucher_id`** (xóa dòng cũ rồi ghi dòng mới) chứ
   không cộng dồn. Ghi sổ → bỏ ghi sổ → sửa → ghi sổ lại là đường đi bình
   thường của mọi chứng từ trong hệ, và một `record` cộng-dồn sẽ nhân đôi công
   nợ ở lượt thứ hai — hỏng âm thầm, chỉ lộ ra ở số dư 131/331.

5. `remove` **từ chối** khi dòng sổ phụ đã bị đối trừ một phần
   (`settled_fc > 0`), thay vì xóa và bỏ lại phiếu thu trỏ vào hư không. Luật ấy
   cũng đăng ký vào `REFERENCE_GUARDS` để nó chạy ở **mọi lượt bỏ ghi sổ**, kể
   cả lượt mà mua/bán quên gọi `remove` — cùng khuôn với luật "đã khớp sao kê
   thì không bỏ ghi sổ" của 6G-2.

   **Bộ guard ấy KHÔNG chạy ở `VoucherService.delete`** — `posting/documents/
   registry.py` gỡ lời gọi đó có chủ đích ở review 6G-2 M-4, và chiều xóa do FK
   canh. Ở đây điều đó an toàn nhờ một bất biến, không nhờ may mắn: dòng sổ phụ
   chỉ sinh ra khi chứng từ **ghi sổ**, còn `delete` chỉ nhận chứng từ **đã cất**
   (nháp) — nên một chứng từ xóa được thì không có dòng nào để bỏ lại. Bất biến
   ấy là nghĩa vụ của 7B/7C: `after_post` gọi `record`, `after_unpost` gọi
   `remove`. Phá nó thì `ON DELETE CASCADE` quét dòng sổ phụ mà không guard nào
   nổ, vì RI trigger của cascade chạy với quyền chủ bảng, ngoài cả RLS.

6. Ảnh chụp `tests/frozen_kernel_api.txt` cập nhật **có chủ đích** trong lát 7A
   (`KET_UPDATE_FROZEN_API=1`), và ADR này là phần "kèm một ADR bổ sung nói vì
   sao" mà ADR-020 §Decision đòi.

## Consequences

**Được:**

- C3 đứng nguyên: `purchase`/`sales` biết Protocol, không biết `receivables` tồn
  tại; `receivables` biết sổ phụ, không biết hóa đơn mua/bán trông thế nào.
- Chiều ghi và chiều đọc của công nợ có cùng hình dạng kiến trúc — người đọc mã
  không phải học hai cơ chế cho một phân hệ.
- Phase 8 (kho) và các phase sau muốn sinh công nợ (vd bút toán phân bổ) dùng
  lại đúng cửa ấy, không cần thêm gì.

**Mất:**

- API đóng băng của phase 6 mở ra một lần, ở lát 7A. Ảnh chụp đổi ⇒ nhánh phase
  8 sẽ thấy CI đỏ khi merge và phải rebase — chi phí có thật, trả một lần, và
  chính là thứ cổng đóng băng sinh ra để bắt người ta nhìn thấy.
- `PROVIDERS.ar_ap_subledger()` trả `None` khi chưa có module `receivables`
  trong tiến trình. Nơi gọi phải từ chối rõ ràng ("chưa bật phân hệ công nợ"),
  **không** được ghi sổ chứng từ mua/bán mà lặng lẽ bỏ qua sổ phụ — đó đúng là
  hình dạng lệch sổ mà check toàn vẹn 131/331 sinh ra để bắt.

**Đo được — và chưa đo được.** Cổng cuối cùng cho quyết định này *đáng lẽ* là
check toàn vẹn "tổng `ar_ap_ledger` khớp số dư TK 131/331" (BR-GLE-05). Lát 7A
**không đăng ký được** nó: bút toán tổng hợp (GLE) gõ thẳng vào TK công nợ kèm
chiều đối tác là một thao tác hợp lệ, có thật, và **không có** dòng sổ phụ đối
ứng — nên đẳng thức ấy sai trên dữ liệu ĐÚNG. Ba nguồn báo-sai nữa: bên trái
tính của TK lưỡng tính không có hóa đơn chi tiết, hóa đơn rơi khi chuyển năm
(`invoices_dropped` đã đếm sẵn), và số dư đầu kỳ không nằm trong `gl_postings`
mà được nối vào lúc đọc. Bản thảo đầy đủ nằm ở
`posting/integrity/checks/arap_matches_control.sql`, **cố ý ngoài registry**,
với bốn điều kiện phải đóng trước khi đăng ký.

Thứ 7A đo được thay vào đó là `settlement_matches_subledger` (BR-QUY-02): số đã
đối trừ trên sổ phụ phải khớp tổng dòng đối trừ của chứng từ đã ghi sổ. Nó hẹp
hơn — không bắt được "quên ghi sổ phụ" — nhưng nó **đúng trên dữ liệu đúng**, và
một cổng đúng-hẹp đáng giá hơn một cổng rộng mà người ta học cách bỏ qua.

Câu hỏi sản phẩm mà việc này lộ ra, để ngỏ cho 7C/10a: **có nên cho bút toán GLE
gõ thẳng vào TK công nợ mà không sinh khoản phải thu/phải trả không?** Phần lớn
phần mềm kế toán hoặc cấm, hoặc tự sinh dòng sổ phụ. Giữ nguyên như hiện nay thì
những khoản ấy vô hình với màn thu nợ và với mọi lượt đối chiếu 131/331 sau này.
