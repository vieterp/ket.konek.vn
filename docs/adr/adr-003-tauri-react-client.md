---
adr: 003
title: "Client: Tauri desktop shell + web UI TypeScript/React"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-02, LD-03, FR-NFR-054, FR-NFR-050]
---

# ADR-003: Tauri + React desktop client

## Context

Phần mềm kế toán cần chạy **Windows + macOS từ cùng một codebase**. Avalonia (runner-up) không tái dùng design system web-first của Konek. Tauri 2 đã chứng minh qua esign.konek.vn: chạy stable trên macOS 12+ và Windows 10+, bundle size nhỏ, tài liệu tốt.

Client phải hỗ trợ: auto-update (OTA), in ấn (silent + UI), chọn/lưu tệp, giữ phiên. Luồng **nghiệp vụ (ghi sổ, tính tiền, kiểm quyền) là lỗi thiết kế nếu ở client** — mọi thứ ở app server (ADR-001).

## Decision

**Tauri 2 desktop shell bọc web UI TypeScript/React**. Client build là bundle web chạy qua `webview2` (Win) / `WebKit` (macOS). Các Tauri command: updater, dialog file, print (in ấn), shell (open file explorer). Luồng nghiệp vụ gọi REST API server, **không dùng Tauri API**.

Thiết kế tạo đường mở lên **chế độ trình duyệt LAN ở v1.x** (không làm, nhưng không được chặn): app server phục vụ chính bundle web tại `/app`, người dùng vãng lai mở bằng Chrome, chỉ mất tính năng Tauri (token, in im lặng, chọn thư mục).

## Consequences

### Tích cực
- Web UI + React design system Konek → tái dùng khi mở web/SaaS sau
- Auto-update Tauri out-of-box; không phải maintain installer riêng
- Bundle size nhỏ, load nhanh (macOS 60MB, Win 150MB)
- IME tiếng Việt support (đã chứng minh ở esign); gõ không trễ trên grid 500 dòng (spike S3 phase 2 đánh giá)

### Tiêu cực
- Phụ thuộc Tauri updates; nếu lỗi bảo mật ở Tauri phải patch khẩn
- Gõ không trễ là rủi ro: nếu grid lag → phải scale back Tauri hoặc dùng AG Grid Enterprise (có phí)

## Reversal cost

Đảo từ Tauri sang native (Avalonia / WinForms / Cocoa):
- Viết lại `client/src-tauri/src/main.rs` (Rust shell) → Avalonia C# hoặc native Cocoa Swift
- Viết lại `client/src` (React → Avalonia XAML hoặc native UI)
- Mất design system web-first → tuỳ chỉnh desktop-only UI
- Installer riêng Windows + macOS + auto-update flow mỗi nền
- **Hợp đồng REST API server không thay** → server tương thích được
- Spike S3 (grid 500 dòng) phải chạy lại với stack mới

## Related FR

- **FR-NFR-054** (Auto-update): Tauri updater — **dựng ở phase 11** cùng
  installer, vì cần cặp khóa ký bản cập nhật và endpoint phát hành thật. Phase 2
  chỉ chốt handshake schema-version client↔server (LD-05).
- **FR-NFR-050** (Thông báo lỗi): client dựng message từ `error_code` server gửi
- **LD-02** (Windows + macOS)
- **LD-03** (Stack chọn)
