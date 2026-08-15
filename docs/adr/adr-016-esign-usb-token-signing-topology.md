---
adr: 016
title: "Topology ký số USB token: dịch vụ esign riêng, app server không nhúng PKCS#11"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-002, ADR-019]
---

# ADR-016: Topology ký số USB token: dịch vụ esign riêng, app server không nhúng PKCS#11

## Context

Ký số USB token yêu cầu PKCS#11 driver (chạy kernel-mode), cấp PIN qua giao diện. Nhúng vào app server (Python, chạy headless, nhiều người dùng) là **rủi ro bảo mật lớn**:
- Bảo vệ PIN khó (không giao diện user).
- Thread-safety PKCS#11 driver uncertain.
- Load-balancing phức tạp (token vật lý chỉ ở một máy).

**Bằng chứng**: Repo `~/code/esign.konek.vn` (Tauri 2 + React 18 + Rust `cryptoki 0.7`, 96 test, README v0.1.0) đã chứng minh **macOS 12+ VÀ Windows 10+ đều ký được token VNPT-CA / Viettel-CA / FPT-CA** → **gỡ AD-1 Critical**.

**LD-04**: Ký USB token = dịch vụ esign riêng (client-side), app server KHÔNG nhúng PKCS#11. **Ký đồng bộ lúc phát hành**; outbox lưu XML đã ký, retry **truyền tải**.

## Decision

1. **Dịch vụ esign riêng** tại `~/code/esign.konek.vn`:
   - Tauri 2 shell + web UI (React 18).
   - Rust backend (`cryptoki 0.7`) tương tác USB token.
   - Chạy **trên máy người ký** (macOS hoặc Windows).
   - PIN zeroization, khóa không rời tầng Rust.
   - Hỗ trợ VNPT-CA, Viettel-CA, FPT-CA.
   - Ký định dạng: **PAdES-BES** (PDF), **RFC3161 TSA** (timestamp).

2. **App server (Konek) gọi esign**:
   - **Không nhúng PKCS#11** vào `server/src/konek`.
   - Gọi esign (sidecar local qua **IPC/HTTP**, hoặc tái dùng module `pkcs11`/`tsa`/`cert` từ esign).
   - Ký **đồng bộ** ngay khi phát hành chứng từ (trước enqueue outbox).
   - Nhận XML/PDF **đã ký** từ esign.

3. **Outbox**:
   - Lưu **XML đã ký** (chứ không phải chứng từ raw rồi ký lại lần 2).
   - Retry **truyền tải** tới HĐĐT provider (không ký lại).
   - Offline: hàng đợi xuống khi mất internet; khi có internet chỉ **truyền tải lại**, KHÔNG ký lại.

4. **XAdES XML (hóa đơn)**:
   - Bắt buộc ở **phase 7** (hóa đơn điện tử VN yêu cầu XML ký XAdES để phát hành).
   - Tái dùng module `pkcs11`/`tsa`/`cert` từ esign (hoặc gọi esign như sidecar).
   - **Spike S1** (cuối phase 2) chứng minh khả thi XAdES trên token USB; bản thân ký XAdES là việc mới dựng trên token access đã có của esign (không phụ thuộc PAdES-BES chạy trước).

5. **Ký số từ xa (NĐ 130/2018)**:
   - **Đường phụ tùy chọn**, không phải đường chính v1.
   - Nếu cần: gọi dịch vụ headless ký từ xa qua API.
   - Không làm ở v1, không làm ở scope app-server v1.

## Consequences

### Tích cực

- **Bảo mật**: PIN ở giao diện UI, khóa ở tầng Rust; app server không biết PIN.
- **Đầy đủ máy**: macOS + Windows đều ký được (AD-1 resolved).
- **Tách mối quan tâm**: app logic độc lập với crypto; esign là black box.
- **Tái dùng esign**: XAdES/RFC3161 mở rộng sử dụng cùng infra.

### Tiêu cực / Đánh đổi

- **Esign cần chạy**: token ký phải trên máy người dùng (không ký headless).
- **IPC/HTTP cục bộ**: thêm port, thêm bảo mật (localhost-only, CORS tight).
- **Phụ thuộc esign.konek.vn**: nếu esign có bug → ảnh hưởng toàn ký số (nhưng repo độc lập, có thể fix ngay).

## Reversal cost

- **Nhúng PKCS#11 vào app server**: phải viết driver management, thread-safety wrapper; test + maintain khó.
- **Bỏ esign, ký cloud**: mất khả năng offline, tùy thuộc network.
- **Ký asynchronous**: phát hành rồi ký sau → outbox chứa XML unsigned, phức tạp; trạng thái voucher unclear.

## Related FR

- **FR-EIV-042**: Ký hóa đơn điện tử (phase 7).
- **FR-EIV-001**: Adapter HĐĐT, outbox.
- **LD-04**: Ràng buộc thiết kế.
- **D3**: Quyết định của user (apply).
- **RT-10**: Pipeline phát hành atomic + ký đồng bộ.
- **ADR-019**: Key-management (credentials cho gọi esign).

---

**Spike S1** (cuối phase 2): Tích hợp esign.konek.vn ở phase-02; ký thử với token thật VNPT/Viettel/FPT trên macOS + Windows.

**Ghi chú**: Giả định cũ "macOS không ký được token" **ĐÃ BỊ BÁC BỎ** bằng bằng chứng esign.konek.vn (96 test, README v0.1.0 trên hệ thống thật).
