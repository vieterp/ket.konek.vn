---
adr: 013
title: "Đánh số chứng từ và khóa Idempotency"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-012, ADR-017]
---

# ADR-013: Đánh số chứng từ và khóa Idempotency

## Context

Hệ thống phải cấp số **liên tục** cho hóa đơn (yêu cầu pháp lý TT200/TT133), nhưng cũng cần **số nội bộ** (phiếu, bút toán) mà không bắt buộc liên tục. Khi mạng chập, client retry POST → phải **ngăn chặn bản ghi trùng lặp**.

**RT-10** (Critical): Pipeline phát hành HĐĐT thiếu định nghĩa — cấp số phải trong cùng transaction với phát hành.

**RT-12** (High): Idempotency key phải ghi trong cùng transaction, hết hạn fail-closed.

## Decision

1. **Đánh số liên tục (hóa đơn)**: bảng `numbering_counters` giữ counter per loại chứng từ + dataset.
   - Lấy số dùng `SELECT counter FOR UPDATE` → increment → return.
   - Đảm bảo không nhảy số kể cả concurrent request.

2. **Đánh số nội bộ**: counter lạc quan (SERIAL/IDENTITY).
   - Cho phiếu, bút toán, chi tiết (không yêu cầu pháp lý liên tục).
   - Tốc độ cao, không lock.

3. **Idempotency key** (`idempotency_keys` table):
   - Insert vào **CÙNG transaction** với business write.
   - Unique constraint trên `(route, key)`. **Không** có cột `dataset_id`: bảng
     này nằm trong chính schema dataset (ADR-017), nên dataset đã được phân tách
     bằng schema rồi.
   - TTL: 24 giờ (mặc định); **hết hạn → fail-closed** cho POST chứng từ (trả 409).
   - Lưu **tham chiếu kết quả** (`result_id`), KHÔNG raw response body (RT-12).
   - Scope: tất cả endpoint thay đổi trạng thái trừ `/reports/*/render` và `/jobs/*` (miễn trừ).

4. **Cấp số HĐĐT**: số cấp trong **cùng transaction**, ghi `in_flight` flag.
   - Nếu thất bại sau khi cấp số → giữ số + biên bản hủy (RT-10).
   - Cấm cấp số rồi mới ghi DB; phải atomic.

## Consequences

### Tích cực

- Số liên tục đảm bảo: `FOR UPDATE` + atomic transaction.
- Retry an toàn: idempotency key + unique constraint.
- Hết hạn tự động: cleanup job xóa key > 24h cũ.
- Áp dụng toàn cầu: không cần logic riêng per endpoint.

### Tiêu cực / Đánh đổi

- Lock `numbering_counters` khi cấp số → throughput tỉ lệ với số concurrent người dùng.
- Idempotency table phình tới nếu không cleanup → phải have monitor + reaper job.
- Cấm cấp số ở transaction không-business → phức tạp HĐĐT workflow.

## Reversal cost

- Bỏ idempotency: phải detect + remove bản ghi trùng (tốn chi phí data audit).
- Đổi sang server-side token thay vì key: client không biết result → không retry được.
- Xóa table `numbering_counters`: quay lại SERIAL → không kiểm soát được số.

## Related FR

- **FR-NFR-004/006**: Số chứng từ đúng, liên tục, không trùng.
- **FR-EIV-014**: Cấp số HĐĐT phải atomic (RT-10).
- **RT-12**: Idempotency key scope, TTL.
- **ADR-017**: Số counter per dataset (schema-per-dataset).

---

**Implementasi trong phase 2/3**: Tạo migration `numbering_counters`, middleware idempotency, fastapi dependency cho scope route.
