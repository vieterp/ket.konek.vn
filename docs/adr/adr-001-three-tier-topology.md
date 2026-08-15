---
adr: 001
title: "Kiến trúc ba tầng: desktop client, app server, database"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-01, LD-03, FR-NFR-031, FR-SYS-001]
---

# ADR-001: Kiến trúc ba tầng

## Context

Hệ thống kế toán phải chạy trên **desktop tại từng máy trạm + database dùng chung trên LAN**. Mô hình 2-tier (client nối thẳng DB) bị loại vì không đảm bảo tính toàn vẹn dữ liệu, không kiểm soát được mọi phép tính tiền và quyền hạn. Mô hình cloud multi-tenant nằm ngoài phạm vi v1 (LD-01).

## Decision

Ba tầng: **Tauri desktop (client) ↔ FastAPI app server ↔ PostgreSQL**. Client gửi REST request HTTPS, server xử lý logic nghiệp vụ, ghi sổ, kiểm quyền; database lưu trạng thái duy nhất. Client KHÔNG nối thẳng PostgreSQL. Chế độ một-máy (app server + DB cùng PC) là cấu hình cài đặt, không nhánh code.

## Consequences

### Tích cực
- Mọi phép tính tiền, kiểm quyền, ghi sổ ở một chỗ → dễ kiểm chứng đúng sai
- App server là nơi duy nhất kiểm tra toàn vẹn (FR-NFR-007); client chỉ render
- Dễ mở rộng lên web/browser mode sau này (không phụ thuộc Tauri)
- Hỗ trợ offline (chế độ một-máy): app server + PostgreSQL cùng PC với client (LD-01), không phụ thuộc mạng

### Tiêu cực
- Mọi thay đổi logic phải qua server → không thể patch client đơn lẻ mà không server
- Phụ thuộc network latency; gõ chứng từ "gõ không trễ" phải spike thử ở phase 2 (grid 500 dòng)

## Reversal cost

Nếu đảo quyết định → **thay toàn bộ hàng rào bảo mật, kiểm quyền, tính tiền từ server sang client**:
- Sửa `main.py` (FastAPI route + auth) → client code (React)
- Ghi lại toàn bộ `ket.kernel` logic vào TypeScript
- Thay generator route + OpenAPI type → generator client-side
- Migrate `audit_log` từ DB audit sang client logger
- Không thể đảo được vì đã ghi sổ ở DB

## Related FR

- **FR-NFR-031** (Hai sổ): thiết kế 3-tier giữ yêu cầu này
- **FR-SYS-001** (Nhiều DN): schema-per-dataset chỉ giữ được trên server + DB
- **FR-NFR-007** (Kiểm tra toàn vẹn): chạy ở server trước ghi DB
- **LD-01** (Desktop + DB LAN)
- **LD-03** (Stack: Tauri + FastAPI + PostgreSQL)
