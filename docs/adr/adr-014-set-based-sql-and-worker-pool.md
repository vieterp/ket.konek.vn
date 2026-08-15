---
adr: 014
title: "SQL tập hợp và worker pool cho tính toán nặng"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-015, ADR-017]
---

# ADR-014: SQL tập hợp và worker pool cho tính toán nặng

## Context

Tính toán trên hàng trăm nghìn chứng từ (tính giá xuất, khấu hao, giá thành, tổng hợp) nằm trên đường critical path kế toán. Python GIL + vòng lặp trên từng dòng sẽ **chắc chắn trượt FR-NFR-041/042** (hiệu năng 10–60s).

**LD-14**: Tính khối lượng lớn phải **set-based SQL** trong PostgreSQL; Python chỉ điều phối (dựng câu lệnh, chia lô, báo tiến độ, ghi audit).

**RT-13**: Worker chết mồ côi job; drain nâng cấp treo → cần lease/heartbeat/reaper.

## Decision

1. **Tính toán = set-based SQL trong PostgreSQL**, không vòng lặp Python.
   - Ví dụ: `INSERT INTO inventory_balances SELECT ... FROM gl_postings WHERE inventory_lot_id IS NOT NULL ...` (snapshot số dư tồn kho).
   - Dùng **window functions**, **CTE**, **JSON aggregation**, **CASE WHEN** logic phức tạp — tất cả ở SQL.
   - Python chỉ: (a) dựng mẫu SQL safe, (b) chia lô nếu >100k dòng, (c) gọi `EXECUTE`, (d) kiểm result.

2. **Worker pool** cho tác vụ async (giá xuất, khấu hao, giá thành, tổng hợp, recalc):
   - **Tiến trình riêng** (không nằm trong tiến trình FastAPI).
   - Mỗi worker độc lập, connect riêng tới DB, không chia sẻ session.
   - Queue trong DB (bảng `jobs`): `(job_id, job_type, params, status, lease_expires_at, heartbeat_at, processed_count)` cho tracking tiến độ.
   - Bảng `jobs` nằm **trong từng schema dataset** (ADR-017) nên không có cột
     `dataset_id`. Hệ quả phải xử ở phase 2: worker duyệt vòng các schema dataset
     đang hoạt động để nhận việc, thay vì đọc một hàng đợi chung.

3. **Lease + heartbeat + reaper**:
   - Worker `SELECT * FROM job_queue WHERE status='pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED` → lấy job, set `lease_expires_at = now() + 5m`.
   - Heartbeat mỗi 30s: update `heartbeat_at = now()` để chứng minh còn sống.
   - **Reaper job** (chạy mỗi 2m): nếu `lease_expires_at < now()` → move `status = 'requeue'` (retry, không phải fail).

4. **Resume semantics**: mỗi job khai rõ:
   - Idempotent? (có thể chạy 2 lần được không?)
   - Partial-success protocol? (nếu tính từ dòng 50k/100k được không?)
   - Rollback nếu fail, hay keep partial?

## Consequences

### Tích cực

- **Hiệu năng**: SQL tập hợp nhanh 100–1000x so với Python loop (dùng được ở 100k+ dòng).
- **Không khóa FastAPI**: request trả ngay, UI không chờ.
- **Tiến độ**: worker cập nhật `processed_count` tới bảng `jobs`, UI polling hiện.
- **Fault-tolerant**: worker chết → reaper requeue, thử lại tự động.

### Tiêu cực / Đánh đổi

- **SQL phức tạp**: developer Python phải viết T-SQL/PL-pgSQL tốt.
- **Debug khó**: lỗi SQL không dễ trace từ Python traceback.
- **Worker process** phải manage (start/stop, monitor, log), tên 1 thêm phức tạp infrastructure.

## Reversal cost

- Đảo lại Python loop: xóa worker, đưa tính toán vào handler → request treo, UI chặn.
- Phải viết lại tất cả query từ Python vòng lặp sang SQL set-based.
- Xóa `jobs` table → mất traceability tác vụ.

## Related FR

- **FR-NFR-041/042/044**: Hiệu năng (10–60s cho sổ cái/tổng hợp/dòng tiền).
- **LD-14**: Ràng buộc thiết kế.
- **RT-13**: Worker fault-tolerance.
- **ADR-015**: Mypy strict ép SQL driver code có kiểu (không `# type: ignore`).

---

**Spike S5** (trong phase 4): Benchmark set-based recalc giá xuất 100k dòng; dữ liệu test phải dày chiều (RT-22) để đo đúng.
