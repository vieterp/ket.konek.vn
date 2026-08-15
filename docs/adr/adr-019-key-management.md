---
adr: 019
title: "Quản lý khóa ứng dụng: OS keystore + chiến lược backup mã hóa"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-015, ADR-018]
---

# ADR-019: Quản lý khóa ứng dụng: OS keystore + chiến lược backup mã hóa

## Context

Ứng dụng kế toán giữ bí mật nhạy cảm:
- `totp_secret`: mã 2FA (RF-NFR-016).
- Token eSign: lưu PIN/token từ esign để retry ký (NĐ 130 tương lai).
- Creds DB: mật khẩu kết nối PostgreSQL.
- Backup files: lưu trữ lâu dài, chứa PII lương + bí mật đối tác.

**Yêu cầu pháp lý + bảo mật**: không lưu rõ trong DB, không sao lưu unencrypted.

**SRS 19 §9, RT-03/05**: Backup mã hóa bắt buộc; khóa ở OS keystore; không khôi phục được nếu thiếu khóa.

**LD-16**: Sao lưu **bắt buộc mã hóa** (khóa ở OS keystore); `totp_secret`/token eSign mã hóa; app→DB TLS + `scram-sha-256`.

## Decision

1. **Khóa app ở OS keystore**:
   - **macOS**: Keychain (`security find-generic-password` / `security add-generic-password`).
   - **Windows**: DPAPI-CNG (Data Protection API) / `dpapi-rs` crate Rust, hoặc Credential Manager.
   - **Linux (nếu chế độ LAN đơn-máy)**: SecretService dbus hoặc file `.env` encrypted (fallback).
   - Khóa = random `Fernet` key (từ `cryptography` lib Python).

2. **Mã hóa secrets ở runtime**:
   - Khi app khởi động: load `app_key` từ OS keystore.
   - `totp_secret` lưu DB dạng `Fernet.encrypt(secret_bytes)` (ciphertext + timestamp).
   - Token eSign (PIN nếu lưu): mã hóa tương tự.
   - Creds DB: password mã hóa, decrypt chỉ khi kết nối (không lưu plaintext).

3. **Backup bắt buộc mã hóa**:
   - `pg_dump`: dữ liệu sau khi encrypt secrets nên ở ciphertext (không plaintext).
   - Thêm lớp mã hóa backup file: GPG hoặc `AES-256-GCM` với khóa từ OS keystore.
   - Backup hết hạn **không khôi phục được nếu thiếu khóa** (RT-03).

4. **Chiến lược per-mode**:
   - **Standalone** (một máy): khóa ở Keychain/DPAPI local.
   - **LAN** (nhiều máy trạm): **Mỗi máy trạm giữ khóa riêng** — backup dữ liệu từ mỗi máy xảy ra độc lập. Nếu một máy mất Keychain → không khôi phục được backup của máy đó (rủi ro chấp nhận: người dùng phải bảo vệ cài đặt Keychain/DPAPI). Phương án thay thế (v1.1): server share encrypted key hoặc key-wrapping-key.
   - **Xoay khóa**: master key rotate (backup lại với khóa mới) → phức tạp, hoãn v1.1.

5. **Setup khi cài đặt**:
   - Installer tạo random key → lưu OS keystore.
   - Backup lưu trữ: encrypt với key này.
   - Người dùng backup → họ không có access khóa (nằm ở OS keystore).

## Consequences

### Tích cực

- **Bí mật không rò**: rò DB không lộ mật khẩu, token, PIN.
- **Backup an toàn**: file backup unreadable nếu thiếu khóa (bảo vệ lưu trữ dài hạn).
- **OS keystore**: OS quản lý khóa, developer không lo implementation details.
- **Compliance**: đáp ứng yêu cầu pháp lý mã hóa PII (bí mật, lương, HĐĐT).

### Tiêu cực / Đánh đổi

- **Phục hồi khó**: nếu user mất OS keystore (Windows uninstall, macOS reset) → không restore được backup.
- **LAN complexity**: share khóa giữa máy trạm khó; nếu không share → backup riêng per-machine.
- **Rotate key**: master key rotation phức tạp, cần re-encrypt toàn bộ.
- **Performance**: decrypt secrets mỗi request → caching cần (lưu bộ nhớ, TTL).

## Reversal cost

- **Bỏ mã hóa**: phải decrypt backup, load plaintext vào DB → rủi ro bảo mật.
- **Đổi sang HSM**: thêm hardware, thêm độ phức tạp, bỏ offline capability.
- **Bỏ backup mã hóa**: khó unencrypt nếu đã sử dụng, mất audit trail.

## Related FR

- **FR-NFR-014/015**: Bảo mật khóa, bí mật.
- **FR-NFR-020..023**: Sao lưu/khôi phục an toàn.
- **FR-NFR-016**: 2FA (totp_secret).
- **SRS 19 §9, RT-03/05**: Rủi ro bảo mật.
- **LD-16**: Ràng buộc thiết kế.
- **ADR-016**: eSign token storage.

---

**Phase 2**: Setup OS keystore integration (Keychain/DPAPI wrapper); phase 11: backup encryption; xoay khóa (v1.1).

**Ghi chú**: Nếu sau này chuyển sang cloud → phải thay OS keystore bằng cloud KMS (AWS KMS, Azure Key Vault), sửa lại key-loading logic.

---

### Chiến lược lưu trữ per-mode (chi tiết)

| Mode | Khóa app | Backup | Xoay khóa | Phục hồi |
|------|----------|--------|-----------|---------|
| Standalone | Keychain/DPAPI local | Encrypt local key | Manual: decrypt + re-encrypt | User backup → app restore (cần key trong Keychain) |
| LAN | Mỗi máy riêng HOẶC server share encrypted | Encrypt per-machine | Phức tạp: sync mọi máy | Mỗi máy restore từ backup của nó |
| Xoay khóa v1.1 | Master key wrapper (key-encrypting-key) | Re-encrypt backup với khóa mới | Wrapper support versioning | Cần key version history |
