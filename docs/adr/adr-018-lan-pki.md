---
adr: 018
title: "LAN PKI: CA nội bộ hoặc scope chế độ trình duyệt"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-001, ADR-016]
---

# ADR-018: LAN PKI: CA nội bộ hoặc scope chế độ trình duyệt

## Context

Chế độ trình duyệt LAN (máy trạm mở Chrome/Edge truy cập `https://host:5443` thay vì Tauri app) là **đường mở rộng rẻ v1.x** — không làm ở v1, nhưng **không được thiết kế thứ gì chặn nó**.

Bảo mật HTTPS LAN yêu cầu TLS certificate. Hai cách:
1. **CA nội bộ enroll vào máy trạm**: cài chứng chỉ CA vào OS trust store (tương tự Windows domain CA).
2. **Scope chế độ trình duyệt cần cert hợp lệ**: sử dụng certificate từ Let's Encrypt hoặc nhà cung cấp công khai (yêu cầu DNS cộng).

**RT-08** (Medium): Trình duyệt vs ghim chứng chỉ tự ký mâu thuẫn → **phải chốt một, không hoãn** (RT-25 không cho phép spike delay).

**LD-04/LD-16**: Gói cứng hóa bắt buộc; PKI là phần bảo mật.

## Decision

**Chốt cách 1: CA nội bộ enroll vào máy trạm**

1. **Setup CA nội bộ** (VD: OpenSSL `pem` format hoặc mô phỏng Windows CA):
   - CA root certificate lưu ở server (ít nhất một bản cài).
   - Khi cài đặt chế độ một-máy hoặc LAN → người quản trị chạy script enroll CA vào OS trust store.
   - macOS: `security add-trusted-cert ...`
   - Windows: PowerShell cert store hoặc `certmgr.msc`.

2. **Server certificate**:
   - Ký bởi CA nội bộ, CN = `host.local` hoặc IP LAN.
   - Load từ OS keystore nếu khả thi, hoặc file PEM encrypted (khóa ở OS keystore).

3. **Client (trình duyệt LAN v1.x)**:
   - Truy cập `https://host:5443` → browser thấy CA nội bộ đã cài → tin tưởng certificate.
   - Không cảnh báo "self-signed".

4. **Client (Tauri v2)**:
   - Webview của Tauri dùng trust store của hệ điều hành (macOS Keychain, Windows cert store).
   - App server cần certificate hợp lệ ký bởi CA nội bộ đã enroll → webview tự động tin tưởng.
   - TLS verify-full bình thường (không dùng cert pin).

## Trade-offs

| Aspek | CA nội bộ enroll | Cert công khai (Let's Encrypt) |
|-------|---|---|
| Chi phí | Free (OpenSSL) | Free (Let's Encrypt) nhưng cần DNS công khai |
| Hiệu năng enroll | 1 lần khi cài | Mỗi 90 ngày renew, cần internet |
| UX (trình duyệt LAN) | Đơn giản (CA đã cài) | Phức tạp (phải mở DNS LAN hoặc port forward) |
| UX (offline) | Hoạt động (no internet) | Cần renew trước khi offline lâu |
| Đa máy trạm | Phải enroll từng máy | Một cert cho mọi host (dễ) |
| Bảo mật | CA nội bộ (controlled) | Public CA (ngoài kiểm soát) |

**Chọn cách 1** vì:
- Chế độ LAN được thiết kế trong v1 (LD-01).
- Offline hoàn toàn là use case chính.
- Người quản trị cài chế độ một-máy sẵn sàng chạy script enroll.

## Consequences

### Tích cực

- **Chế độ trình duyệt LAN v1.x**: không bị chặn bởi PKI.
- **Offline**: không cần internet, không cần renew.
- **Kiểm soát**: CA nội bộ ở tay admin, không phụ thuộc nhà cung cấp.
- **Multi-dataset**: mỗi dataset có cert nếu cần (hoặc wildcard nội bộ).

### Tiêu cực / Đánh đổi

- **Phải maintain CA**: certificate hierarchy, renewal, rotation.
- **Enroll per-machine**: thêm bước setup khi cài trên máy trạm mới.
- **Không thích hợp internet**: nếu sau này muốn cloud/SaaS → phải switch sang public CA (sửa code).

## Reversal cost

- **Đổi sang public cert**: phải setup DNS LAN hoặc ngrok/tunnel, sửa serve config, test lại trình duyệt.
- **Bỏ PKI**: server chạy HTTP (không an toàn) hoặc self-signed không enroll (browser warning).

## Related FR

- **FR-NFR-014**: Kênh bảo mật (TLS).
- **LD-04/LD-16**: Gói cứng hóa bắt buộc.
- **RT-08**: Critical decision point (phải chốt).
- Chế độ trình duyệt LAN v1.x (đường mở, không làm v1).

---

**Phase 11** (deployment):
- Script enroll CA `setup-ca-trust.sh` (macOS) / `.ps1` (Windows).
- Generate server cert từ CA nội bộ.
- Load cert trong FastAPI `uvicorn --ssl-keyfile --ssl-certfile`.

**Ghi chú**: Nếu trong tương lai chuyển sang public cert, chuỗi mã vẫn xử lý cert từ keystore nên không cần sửa code logic, chỉ đổi config cert path.
