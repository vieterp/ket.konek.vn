---
adr: 020
title: "Đóng băng API công khai của ket.kernel và ket.posting"
status: accepted
date: 2026-08-31
supersedes: []
related: [ADR-004, ADR-015]
---

# ADR-020: Đóng băng API công khai của `ket.kernel` và `ket.posting`

## Context

Phase 7 (Mua – Bán – Công nợ) và phase 8 (Kho – CCDC – TSCĐ) **chạy song song**
sau phase 6 (plan.md §Phase Roadmap). Chúng khác module, khác file, nên đường
va chạm duy nhất là hai gói dùng chung: `ket.kernel` và `ket.posting`.

RT-18 đã xử một nửa vấn đề: **mọi Protocol liên-module phải khai ở phase 6**,
để phase 7/8 chỉ *cài* chứ không *sửa* kernel. Nửa còn lại chưa có gì canh —
không ai ngăn một lượt sửa chữ ký `PostingService.post(...)` hay
`SettlementTargetSource.mark_paid(...)` ở nhánh phase 7. Lượt sửa ấy **không
làm gãy bản dựng của người sửa**; nó làm gãy nhánh phase 8, muộn, lúc merge, và
lỗi hiện ra ở một tệp mà người đọc log không có lý do gì để nghi ngờ.

Phase-06 bước 23 gọi việc này là "đóng băng API công khai... ghi vào
`docs/system-architecture.md`. Thay đổi sau đó cần ADR bổ sung." Câu ấy mô tả
một quy ước; quy ước không có cổng thì trượt.

Ba cách canh được cân nhắc (quyết định của user, 2026-08-31):

1. **Chỉ tài liệu + ADR** — rẻ nhất, dựa hoàn toàn vào review người. Chính là
   thứ vừa trượt ở đoạn trên.
2. **Luật ranh giới import** — `import-linter` đã canh (C1–C5) và vẫn canh:
   nó bắt "ai import ai", không bắt "chữ ký đổi".
3. **Ảnh chụp chữ ký gác CI** — chọn cách này.

## Decision

1. **Bề mặt đóng băng** là hai tệp KHAI, không phải toàn bộ hai gói:
   - `ket.kernel.protocols` — Protocol liên-module + `CrossModuleProviders`.
   - `ket.posting.contracts` — ranh giới công khai của `ket.posting`.

   Cả hai khai `__all__` tường minh. Tệp con bên trong hai gói **không** đóng
   băng: đóng băng cách cài đặt sẽ làm mọi lượt tái cấu trúc lành mạnh hóa đỏ,
   và một cổng kêu vì lý do không ai quan tâm là một cổng người ta học cách bỏ
   qua.

2. **Cổng**: `server/tests/test_frozen_kernel_api.py` kết xuất chữ ký của hai
   tệp trên thành văn bản và so với `server/tests/frozen_kernel_api.txt` đã
   commit. Chữ ký đổi ⇒ CI đỏ.

3. **Đường đổi hợp lệ** — ba bước, không bỏ bước nào:
   - viết một ADR bổ sung nói vì sao chữ ký phải đổi và ai chịu ảnh hưởng;
   - `KET_UPDATE_FROZEN_API=1 uv run pytest tests/test_frozen_kernel_api.py`;
   - commit ảnh chụp mới **cùng** ADR đó.

4. **Protocol không có người gọi thì không đóng băng — xóa.** Lát 6G-2 xóa
   `DepositMovementSource` + `BankDepositMovementSource` vì bản sửa H-2 của
   6G-1 (carry-forward đọc thẳng `gl_postings.bank_account_id`) đã lấy đi người
   gọi cuối cùng. Đóng băng một Protocol không ai gọi là ghim vĩnh viễn một
   phỏng đoán về nhu cầu tương lai vào chỗ đắt nhất để sửa.

## Consequences

**Được:** phase 7 và 8 có một hợp đồng đọc được và một cổng biết kêu. Ảnh chụp
cũng là tài liệu chính xác nhất về bề mặt liên-module — nó sinh ra từ mã, không
từ trí nhớ.

**Mất:** thêm một bước cho mỗi lần đổi chữ ký thật. Đó là chi phí có chủ đích:
việc đổi bề mặt dùng chung giữa hai nhánh song song **nên** tốn hơn việc sửa
trong nhà.

**Rủi ro còn lại:** ảnh chụp chỉ canh *chữ ký*, không canh *ngữ nghĩa*. Đổi
nghĩa của một tham số mà giữ nguyên kiểu vẫn lọt — vẫn là việc của review
người, như luật thứ 6 ("cấm `dict[str, Any]` qua ranh giới module") của
§7 `system-architecture.md`.
