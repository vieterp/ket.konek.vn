---
adr: 002
title: "Python 3.12 / FastAPI + SQLAlchemy 2.x; app server KHÔNG nhúng PKCS#11"
status: accepted
date: 2026-08-15
supersedes: []
related: [LD-03, LD-04, D3, FR-NFR-054, ADR-016]
---

# ADR-002: App server Python/FastAPI + KHÔNG PKCS#11

## Context

Báo cáo nghiên cứu tech-stack khuyến nghị **C#/.NET 8 + Avalonia**. Tuy nhiên đội thực thi có nền Python/Odoo, không C#. Trên miền 504 FR đổi stack = tăng rủi ro thực thi, học curve, không tái dùng experience. Vì hợp đồng giữa client-server là REST + OpenAPI, mỗi tầng độc lập được.

## Decision

App server: **Python 3.12, FastAPI + SQLAlchemy 2.x + Alembic + Pydantic v2 + psycopg 3**. Chọn năng lực đội thay vì ngôn ngữ khuyên. Mọi yêu cầu phi chức năng (tính đúng, hiệu năng, bảo mật) không phụ thuộc ngôn ngữ — bù bằng **mypy strict từ commit đầu** (LD-13).

**CRISP:** app server **KHÔNG bao giờ nhúng PKCS#11 hoặc khóa HSM**. Ký số USB token = dịch vụ esign riêng (client-side Rust). App server gọi esign qua sidecar HTTP/IPC để ký **đồng bộ lúc phát hành**, rồi nhận XML đã ký; outbox chỉ lưu XML ký + retry truyền tải (xem ADR-016).

## Consequences

### Tích cực
- Dùng năng lực hiện tại → giảm rủi ro học curve, tốc độ dev cao
- Tận dụng Odoo experience với ORM, mẫu workflow, module separation
- Pydantic v2 + mypy strict bù được việc Python không kiểm kiểu lúc compile
- Ký số qua esign service → khóa không rời tầng Rust, macOS **ký được token** (gỡ AD-1 Critical)

### Tiêu cực
- Đóng gói Python thành installer Windows/macOS khó hơn .NET (spike S4 phase 2)
- Vòng lặp Python trên hàng trăm nghìn dòng chắc chắn TLE → bắt buộc set-based SQL (LD-14)
- Không tái dùng `.NET` ecosystem (vd Avalonia). Phải dựng Tauri + React client riêng

## Reversal cost

Đảo từ Python sang C#/.NET:
- Viết lại toàn bộ `server/src/ket/` từ Python sang C# (posting, modules, reporting)
- Thay SQLAlchemy sang Entity Framework Core
- Thay Pydantic v2 sang C# records/DataAnnotations
- Thay FastAPI router sang ASP.NET Core controller
- Vứt hết Alembic migration, dùng EF Code-First
- Spike S4 (đóng gói) phải chạy lại Windows/macOS
- **Đã ghi sổ dữ liệu ở DB PostgreSQL không thay đổi** → hợp đồng REST giữ được

## Related FR

- **FR-NFR-001/002** (Decimal, làm tròn): Python `decimal` + custom `rounding` module ở `ket.kernel.money`
- **FR-NFR-041/042** (Hiệu năng báo cáo): set-based SQL, không vòng lặp Python
- **FR-NFR-054** (Auto-update): FastAPI + Alembic migration riêng server
- **LD-03** (Stack chọn)
- **LD-04** (KHÔNG PKCS#11)
- **LD-13** (mypy strict)
- **D3** (Esign integration đã chứng minh macOS ký được token)
