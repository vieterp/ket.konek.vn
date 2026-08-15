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
   - Tên schema: **`ds_<mã dataset>`** (`kt2026` → `ds_ktoan2026`). Mã do người dùng đặt,
     đi qua whitelist ký tự ở `kernel/datasets/naming.py` trước khi ghép vào SQL —
     tên schema là identifier nên không tham số hóa được.
   - Tiền tố `ds_` là thứ khiến mã trùng tên schema hệ thống (`public`, `pg_catalog`)
     vẫn an toàn: bất biến cần khóa là **kết quả** không bao giờ là tên dành riêng.
   - Mỗi schema chứa toàn bộ bảng: `accounts`, `vouchers`, `gl_postings`, `audit_log`, v.v.
   - **Ba bảng nằm ngoài**, ở schema điều khiển `public`: `datasets` (sổ đăng ký),
     `users` (danh tính đăng nhập toàn cục), `system_metadata`. Quyền của người dùng thì
     per-dataset (`user_roles`, `user_branches` nằm trong schema dataset) vì vai trò của
     một người ở mỗi doanh nghiệp là khác nhau.
   - **Không có khóa ngoại chéo schema**, kể cả tới `public.users`: liên hệ lưu bằng
     `user_id` trần. Lý do là vận hành — một bản dump per-schema có FK trỏ ra ngoài sẽ
     không restore được sang cụm khác, đúng vào lúc cần nó nhất (RT-03/RT-14).

2. **Routing schema ở tầng session**:
   - Mỗi transaction bắt đầu bằng `SET LOCAL ROLE ds_<mã>_app` (quyền), rồi
     `SET LOCAL search_path TO "ds_<mã>", public, pg_temp` (tầm nhìn). Xem §Consequences
     để biết vì sao cần cả hai — `search_path` một mình không cấm được gì.
   - Mọi query sau đó tự động hoạt động trên schema đó (không cần `dataset_id` WHERE clause).
   - `LOCAL` là bắt buộc: connection quay lại pool phải sạch, nếu không request kế tiếp
     kế thừa dataset của request trước.
   - Schema đích chỉ lấy từ **bảng `datasets`** (`kernel/datasets/service.resolve_dataset`),
     không suy trực tiếp từ mã trong token: một mã bịa phải dừng ở tra cứu, không được
     biến thành `search_path` trỏ vào hư không.

3. **Handshake lúc đăng nhập**:
   - Query lấy schema version (revision Alembic) của dataset → so với client version.
   - Không khớp → yêu cầu nâng cấp client/server.
   - App server cũng **từ chối khởi động** nếu bất kỳ dataset nào lệch revision
     (`main.verify_schema_versions`) — thà không chạy còn hơn ghi sổ vào cấu trúc cũ.

4. **Đánh số per-dataset**:
   - Bảng `number_sequences` nằm trong mỗi schema (không shared).
   - Mỗi counter độc lập per dataset.

5. **Nhật ký audit per-dataset**:
   - `audit_log` nằm trong schema dataset → backup/restore cũng đi kèm dataset.

6. **RLS (Row-Level Security) per-dataset**:
   - Schema cô lập dataset → RLS chỉ cần cô lập chi nhánh trong dataset.
   - GUC là **danh sách** chi nhánh, không phải một giá trị: một người dùng thường
     được gán nhiều chi nhánh.
   - Policy thực tế:
     `branch_id = ANY (string_to_array(nullif(current_setting('ket.branch_ids', true), ''), ',')::int[])`.
     `nullif` giữ tính **fail-closed**: GUC chưa đặt → biểu thức NULL → không dòng nào lọt.
   - Bảng danh mục `branches` **không** bật RLS: `WITH CHECK` trên chính nó sẽ khiến
     không ai tạo được chi nhánh mới (id do sequence cấp lúc INSERT, không thể nằm sẵn
     trong phạm vi người tạo). Ai được xem/sửa danh mục là câu hỏi của RBAC.
   - `user_branches` cũng không bật RLS: nó là **nguồn** dựng nên phạm vi RLS.

7. **Sao lưu/khôi phục per-dataset**:
   - `pg_dump --schema=ds_<mã>` → backup chỉ dataset này.
   - Restore độc lập, không ảnh hưởng dataset khác.

8. **Alembic quản schema dataset; schema điều khiển dùng DDL bootstrap idempotent**:
   - Migration chạy **lặp cho từng schema**, `alembic_version` nằm trong chính schema đó.
   - Để Alembic quản luôn `public` sẽ cần nhánh migration thứ hai với bảng phiên bản
     riêng — thêm hẳn một chiều phức tạp cho ba bảng gần như không đổi. Đổi lại, schema
     điều khiển có số phiên bản riêng trong `system_metadata` và **cũng được kiểm** lúc
     khởi động.
   - Ngưỡng đảo quyết định: nếu ba bảng điều khiển bắt đầu đổi thường xuyên (khả năng
     cao nhất là khi thêm SSO hoặc chính sách mật khẩu), chuyển sang nhánh Alembic thứ
     hai — **không** chồng thêm bước thủ công.

## Consequences

### Tích cực

- **Cô lập theo schema**: nằm ở tầng database (namespace), không phụ thuộc `WHERE` do lập
  trình viên viết.
- **Backup/restore nhanh**: mỗi dataset độc lập, phục hồi một dataset không ảnh hưởng khác.
- **Schema migration centralized**: migration chạy trên mọi schema tự động.
- **Hiệu năng**: index, statistics per-dataset tối ưu hóa.

### Tiêu cực / Đánh đổi

- **⚠ GIẢI MỘT PHẦN (phase 2 slice 2B-0, 2026-08-15) — cô lập nay dựa vào quyền, nhưng
  KHÔNG chặn được tiêm SQL nhiều câu lệnh.** Trạng thái cũ: `ket_app` có `GRANT USAGE`
  trên **mọi** schema dataset, nên câu truy vấn ghi rõ tên schema
  (`SELECT … FROM ds_beta.gl_postings`) vẫn đọc được dataset khác.

  **Ranh giới thật, đã đo trên PostgreSQL 16.15** (đọc kỹ trước khi dựa vào cơ chế này —
  nhất là phase 5, nơi report engine ghép SQL):

  | Loại tiêm | Trước 2B-0 | Sau 2B-0 |
  | --- | --- | --- |
  | Một câu lệnh, ghi rõ `ds_beta.x` | đọc được | **bị chặn** (`42501`, chặn ngay lúc lập kế hoạch) |
  | Một câu lệnh + `set_config('role', …)` trong subquery/CTE | — | **bị chặn** |
  | Nhiều câu lệnh ngăn bằng `;` | đọc được | **VẪN đọc và ghi được** |

  Vì sao vế cuối không chặn được: `SET ROLE` xét tư cách thành viên của **`session_user`**
  (`ket_app`), không xét vai trò đang có hiệu lực. Mà `ket_app` buộc phải là thành viên của
  mọi `ds_*_app` — đó chính là điều kiện để `SET LOCAL ROLE` chạy được. Nên từ một phiên đã
  bind `ds_alpha`, một câu `; SET ROLE ds_beta_app;` là đủ. `WITH SET FALSE` của PG16 không
  dùng được vì cơ chế này cần đúng quyền `SET`.

  **Đường siết triệt để duy nhất trên PG16**: vai trò dataset là vai trò **ĐĂNG NHẬP** riêng
  + connection pool riêng mỗi dataset — không còn tư cách thành viên thì không `SET ROLE`
  được. Đánh đổi: N pool, quản lý mật khẩu cho từng vai trò, `pg_hba` dài hơn.

  **Luật bắt buộc chừng nào chưa siết** (phase 5 trở đi): mọi `text()` chạy chuỗi có phần do
  người dùng ảnh hưởng **phải** kèm tham số ràng buộc — psycopg khi đó dùng extended
  protocol, và extended protocol không cho nhiều câu lệnh trong một lần gửi.

  Cơ chế đã dựng (`kernel/security/dataset_roles.py`):

  ```
  ket_app        LOGIN, NOINHERIT     -- không có USAGE trên schema dataset nào
  ket_control    NOLOGIN (nhóm)       -- quyền trên 3 bảng schema điều khiển
  ds_<mã>_app    NOLOGIN, INHERIT     -- thành viên ket_control; giữ quyền bảng schema mình
  ```

  `GRANT ds_<mã>_app TO ket_app` để app **chuyển được** vai trò; `NOINHERIT` để nó
  **không tự động có** quyền của các vai trò đó — thiếu vế nào cũng mất tác dụng. Mỗi
  transaction mở bằng `SET LOCAL ROLE` → `SET LOCAL search_path` → `set_config` GUC chi
  nhánh (`persistence/session.bind_transaction_scope`).

  Hai vướng mắc nêu ở bản trước được giải thế này: bảng điều khiển cấp quyền cho **nhóm**
  `ket_control` (vai trò dataset kế thừa) **và** cấp thẳng cho `ket_app` (đường đăng nhập,
  chạy trước khi chọn dataset — `ket_app` NOINHERIT nên hai lần cấp không thừa nhau).

  Hệ quả kèm theo: `ket_owner` cần `CREATEROLE` để tạo vai trò lúc provision; trần mã
  dataset hạ xuống **56** ký tự (63 − `ds_` − `_app`) vì tên vai trò dài quá bị PostgreSQL
  cắt âm thầm, khiến hai dataset dùng chung một vai trò.

  Test canh: `server/tests/test_dataset_role_isolation.py`.
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
