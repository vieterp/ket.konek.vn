---
adr: 011
title: "Tính lại giá xuất kho có kiểm soát"
status: proposed
date: 2026-08-15
supersedes: []
related: [ADR-014, ADR-017]
---

# ADR-011: Tính lại giá xuất kho có kiểm soát

## Context

Khi nhập chứng từ xuất kho với ngày lùi trước kỳ đã khóa, giá vốn dùng bình quân tức thời (BQTT) hoặc FIFO có thể thay đổi, khiến cần tính lại lại toàn bộ phát sinh sau đó. Nếu tính lại **ngầm** (khi người dùng mở báo cáo), chi phí tính toán cao; nếu **không** tính lại, số liệu sai lệch.

**SRS 19 §9 rủi ro #6:** Tính giá xuất kho theo BQ tức thời/FIFO khi chèn chứng từ lùi ngày → sai giá vốn hàng loạt, khó phát hiện.

**RT-11:** Chính sách **cắt qua kỳ đã khóa** còn chờ chốt kế toán trưởng. Mặc định an toàn: chặn.

## Decision

Tính lại giá xuất kho có **đánh dấu + hàng đợi có kiểm soát**, **cấm tính lại ngầm**:

1. **Đánh dấu** (dirty flag) snapshot giá vốn tồn kho khi phát hiện horizon tính lại cần thiết.
2. **Hàng đợi tính lại** trong DB (`recalc_inventory_queue`) ghi rõ phạm vi ảnh hưởng (từ ngày X, tới ngày Y).
3. **Không tự động** tính lại khi người dùng đọc báo cáo; thay vào đó, **hiển thị trên preview** (FR-STK-003) rằng "Cần tính lại giá vốn từ ngày …".
4. **Người dùng chạy task** (bất đồng bộ qua worker, RT-14) hoặc **admin chốt chính sách** rồi tính lại toàn bộ.

## Consequences

### Tích cực

- Hiệu năng báo cáo: không phải tính lại hot-path khi mở sổ.
- Rõ ràng: người dùng thấy rõ khi dữ liệu cần cập nhật.
- Kiểm soát: admin có toàn quyền quyết định khi tính lại (quan trọng nếu cắt kỳ khóa).

### Tiêu cực / Đánh đổi

- **Chính sách khi cắt qua kỳ đã khóa chưa chốt** (RT-11, Open question #11): Mặc định **chặn + hiện preview**. Nếu lật sang "bút toán đảo chênh lệch giá vốn" thì phải:
  - Thêm luồng **tự động** tạo bút toán đảo vào kỳ mở sớm nhất (phải có thêm test).
  - Phức tạp: tính chênh lệch, audit, khóa sổ bị mở lại.
- Thêm giao diện + hàng đợi: người dùng phải hiểu workflow tính lại.

## Reversal cost

- Nếu lật sang tính lại ngầm: xóa dirty flag + hàng đợi, thêm view tính toán động vào báo cáo → sửa `reporting` layer.
- Nếu lật sang bút toán đảo: thêm module `costing.reversal_entries` + schema bảng reversals → migration + business logic.
- Bảng `recalc_inventory_queue` (hàng đợi tính lại) phải sửa/xóa; cần thêm vào §11 danh sách bảng lõi ở phase 8.

## Related FR

- **FR-STK-003**: Tính lại cục bộ trên màn inventory (phải có preview cảnh báo cần tính lại).
- **FR-NFR-041/042/044**: Hiệu năng — cấm tính lại ngầm trong query tính toán nặng.
- Liên quan **RT-11** (chính sách cắt kỳ khóa).
- Phụ thuộc **ADR-014** (worker tính lại bất đồng bộ).

---

**Trạng thái**: `proposed` — có điều kiện chuyển `accepted`:
1. Kế toán trưởng chốt chính sách khi horizon tính lại cắt qua kỳ đã khóa.
2. Test thường kỳ (không cắt) và cắt kỳ khóa đều pass.
3. Preview hiện chính xác cảnh báo tới người dùng.
