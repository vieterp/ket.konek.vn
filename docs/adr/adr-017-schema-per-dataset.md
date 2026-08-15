---
adr: 017
title: "Schema-per-dataset trong một PostgreSQL DB"
status: accepted
date: 2026-08-15
supersedes: []
related: [ADR-012, ADR-014]
---

# ADR-017: Schema-per-dataset trong một PostgreSQL DB

## Context

Hệ thống hỗ trợ **nhiều dữ liệu kế toán độc lập** (FR-SYS-001 MUST): mỗi công ty, mỗi năm tài chính, mỗi phân nhánh riêng. Cô lập dữ liệu là **tuyệt đối** (không được trộn lẫn số liệu cộng ty A vào cộng ty B).

Các cách tiếp cận:
- Một DB/dataset: phình số DB, khó backup, quản lý schema migration phức tạp.
- Một table, cột `dataset_id`: rủi ro filter bị quên, tính toán cross-tenant khó.
- **Một schema/dataset** (PostgreSQL schema = namespace bảng): cô lập hoàn toàn, schema migration centralized.

**LD-15 (D2)**: **Nhiều dữ liệu kế toán = schema-per-dataset trong MỘT PostgreSQL DB**. Routing schema theo dataset ở tầng session/connection.

**RT-17** (Critical): Không chủ sở hữu → chốt ADR-017.

## Decision

1. **Một PostgreSQL DB, nhiều schema** (mỗi schema = một dataset kế toán).
   - Ví dụ: `dataset_1`, `dataset_2`, ... (tên tương ứng ID dataset từ config).
   - Mỗi schema chứa toàn bộ bảng: `accounts`, `vouchers`, `gl_postings`, `audit_log`, v.v.
   - Không bảng shared; nếu cần danh mục shared → replicate vào mỗi schema hoặc generic schema `shared` (read-only từ app).

2. **Routing schema ở tầng session**:
   - Khi user đăng nhập → phát session → ghi vào session `search_path = 'dataset_XYZ, shared, public'`.
   - Mọi query sau đó tự động hoạt động trên dataset_XYZ (không cần `dataset_id` WHERE clause).
   - FastAPI middleware: extract dataset từ JWT token → `SET search_path` trong connection pool.

3. **Handshake lúc đăng nhập**:
   - Query lấy schema version của dataset → so với client version.
   - Nếu không match → return 400, yêu cầu nâng cấp client/server.

4. **Đánh số per-dataset**:
   - Bảng `numbering_counters` nằm trong mỗi schema (không shared).
   - Mỗi counter độc lập per dataset.

5. **Nhật ký audit per-dataset**:
   - `audit_log` nằm trong schema dataset → backup/restore cũng đi kèm dataset.

6. **RLS (Row-Level Security) per-dataset**:
   - Schema cô lập dataset → RLS chỉ cần cô lập chi nhánh trong dataset.
   - Policy: `USING (branch_id = current_setting('app.tenant_branch')::int)`.

7. **Sao lưu/khôi phục per-dataset**:
   - `pg_dump --schema=dataset_1` → backup chỉ dataset này.
   - Restore độc lập, không ảnh hưởng dataset khác.

## Consequences

### Tích cực

- **Cô lập theo schema**: Nằm ở tầng database (namespace), cùng role `konek_app` nhưng access bị hạn chế bởi `search_path` + `REVOKE USAGE ON SCHEMA` các dataset khác. Điều kiện bắt buộc: phải `REVOKE USAGE` trên schema khác hoặc cấp quyền động theo phiên (không phụ thuộc filter tầng ứng dụng). **Phải thực hiện ở phase 2** (tránh SQL injection thủ công).
- **Backup/restore nhanh**: mỗi dataset độc lập, phục hồi một dataset không ảnh hưởng khác.
- **Schema migration centralized**: migration chạy trên mọi schema tự động.
- **Hiệu năng**: index, statistics per-dataset tối ưu hóa.

### Tiêu cực / Đánh đổi

- **Danh mục chia sẻ**: TK kế toán, hàng hóa, đối tác có thể chung (nếu multi-tenant thật) → phải replicate vào mỗi schema hoặc generic schema (phải quản lý sync).
- **Connection pool**: mỗi connection phải biết dataset → Middleware phức tạp hơn.
- **Migration logic**: Alembic phải chạy trên mỗi schema (hoặc dùng script wrapper).
- **Monitoring**: phải track stats per-schema, backup per-schema.

## Reversal cost

- **Đổi sang cột `dataset_id`**: phải sửa tất cả bảng thêm cột, tất cả query thêm WHERE, tất cả index thêm dataset_id → massive migration.
- **Đổi sang DB riêng**: phải tách connection pool, distribute transaction, backup script, disaster recovery plan từ đầu.
- **Bỏ schema, dùng DB**: không khả thi sau khi đã có dữ liệu.

## Related FR

- **FR-SYS-001**: Nhiều dữ liệu kế toán cô lập (MUST).
- **FR-SYS-071/072/074**: Phân quyền chi nhánh (RLS per-dataset).
- **FR-NFR-020..023**: Sao lưu/khôi phục.
- **LD-15**: Ràng buộc thiết kế.
- **D2**: Quyết định của user (apply).
- **RT-17**: Critical finding.
- **ADR-012**: Khóa chính per-dataset.
- **ADR-013**: Đánh số per-dataset.

---

**Phase 2**: Setup migration `env.py` support multi-schema; phase 3: đánh số + danh mục per-schema; phase 11: backup/restore per-schema.

**Ghi chú**: Tên schema có thể `dataset_{id}` hoặc UUID; phải quy ước sớm để không thay đổi schema migration sau.
