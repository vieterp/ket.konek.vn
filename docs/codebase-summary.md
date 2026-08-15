# Tóm tắt mã nguồn — Konek Két

**Cập nhật:** 2026-08-15 · **Trạng thái:** phase 1 xong; phase 2 xong lát 2A (nền dữ liệu), 2B-0 (cô lập dataset + release pipeline), 2B-1a (danh tính lõi).

Tài liệu này mô tả **thứ đang có thật trong repo**. Kiến trúc đích của cả v1
nằm ở `docs/system-architecture.md` — phần lớn nội dung ở đó chưa dựng.

---

## 1. Đang có gì

`server/src/ket` ≈ **4.500 dòng Python**, tất cả là **hạ tầng**: định tuyến dữ
liệu, phân quyền tầng DB, nhật ký, phép tính tiền, danh tính. **Chưa có một
nghiệp vụ kế toán nào** — không chứng từ, không sổ cái, không báo cáo.

| Có thật | Chưa có |
| --- | --- |
| Vai trò DB tách đôi, RLS, nhật ký bất biến, **cô lập dataset bằng vai trò per-dataset** | **RBAC enforcement** (`require_permission`), định tuyến dataset theo request |
| Schema-per-dataset + provisioning + `ensure_cluster` + `repair_dataset_privileges_statements` | API nghiệp vụ (mới có `/health` + `/api/v1/auth/*`) |
| **Đăng nhập Argon2id, phiên lưu DB thu hồi được ngay, 2FA TOTP chống phát lại, khóa tạm khi dò mật khẩu** | Bảng chứng từ / `gl_postings` / số dư |
| **Khóa mã hóa từ OS keystore** (fail-closed) — `totp_secret` không bao giờ ở dạng rõ trong DB/backup | Dịch vụ idempotency, worker chạy job |
| **Hợp đồng lỗi RFC 7807** + mã tương quan mỗi request | Client (mới là bộ khung rỗng) |
| 13 bảng nền + 2 bảng điều khiển mới, migration `0001`, **schema điều khiển có bước nâng cấp tường minh (v2)** | Bộ cài app server (S4/phase 11); ký chứng thư OS |
| `money` (Decimal, ROUND_HALF_UP) + **luật cứng quét AST cấm tiêm SQL không tham số** | — |
| **`python -m ket.admin`**: ensure-cluster · create-user · reset-password · reset-totp · generate-app-key | — |
| **PostgreSQL 16** là phiên bản đích; kiểm lúc khởi động, tắt cổng thì có cảnh báo trong log | — |
| Kênh phát hành `release.yml` (Win msi+nsis, macOS dmg, ký gói updater) — **đã dựng, chưa chạy lần nào** | — |

---

## 2. Bản đồ mã nguồn

### `server/src/ket/`

| Đường dẫn | Vai trò | Đọc khi nào |
| --- | --- | --- |
| `main.py` | App factory, lifespan (dựng pool + **từ chối khởi động** nếu schema/PostgreSQL lệch phiên bản) + **`verify_postgres_version` (PG16+ bắt buộc)** | Thêm router, đổi vòng đời tiến trình |
| `settings.py` | Cấu hình `KET_*`. **Hai DSN tách biệt**: `database_url` (runtime, `ket_app`) và `owner_database_url` (DDL, `ket_owner`) | Thêm tham số cấu hình |
| `model_registry.py` | Nạp **mọi** model cho Alembic. Đặt ở gốc gói vì `kernel` không được import `modules`/`posting` (luật C1) | **Thêm model mới → thêm một dòng ở đây**, nếu không autogenerate bỏ sót |
| `kernel/money.py` | `round_money`, `multiply_money`, `sum_money`, `convert_currency` | Mọi phép tính tiền |
| `kernel/errors.py` | `DomainError` + mã lỗi ổn định | Thêm lỗi nghiệp vụ |
| `kernel/persistence/` | `base` (2 `MetaData`), `engine`, `session`, `unit_of_work`, `types` | Chạm tầng lưu trữ |
| `api/` | Tầng HTTP: `middleware/{request_context,problem_details}`, `dependencies` (principal, khóa app), `routers/auth` (+ `auth_schemas`). **`kernel` không được import tầng này** (contract C1) | Thêm endpoint, đổi hợp đồng lỗi |
| `admin/` | `python -m ket.admin` — lệnh chạy tại máy chủ, không qua HTTP | Khởi tạo cụm, tài khoản phá-kính |
| `kernel/security/` | `rls`, `tenant` (GUC chi nhánh), `grants`, **`dataset_roles`**, `roles.sql`, `models` (RBAC + `branches` + `settings`); **`passwords` (Argon2id), `totp` (chống phát lại), `keystore` (Fernet + OS keystore), `auth_models` (`auth_sessions`), `auth_service` (đăng nhập/phiên), `account_service` (vòng đời tài khoản)** | Chạm phân quyền hoặc danh tính |
| `kernel/auditing/` | `models` (`audit_log` của dataset), `listener` (4 móc `Session`), **`control_log` (`control_audit_log` — sự kiện tài khoản, ghi tường minh vì `User` không đi qua listener)** | Hiểu vì sao mọi thay đổi đều có vết |
| `kernel/datasets/` | `naming` (trần 60→56 ký tự), `models` (schema điều khiển), **`bootstrap` (`ensure_control_schema`, `ensure_dataset_roles`, `ensure_cluster`), `provisioning` (`assert_dataset_role_administrable`)**, `service` | Tạo/định tuyến dữ liệu kế toán |
| `kernel/{jobs,idempotency,numbering}/models.py` | Bảng đã dựng, **dịch vụ chưa viết** (lát 2B/phase 3) | — |
| `modules/*`, `posting/`, `reporting/`, `worker/` | Chỉ có `contracts.py` rỗng — chỗ giữ sẵn cho phase sau | — |

### Ngoài server

| Đường dẫn | Trạng thái |
| --- | --- |
| `server/migrations/` | `env.py` (chạy per-schema) + `versions/0001_core_platform.py` |
| `client/src/` | Bộ khung: `main.tsx`, `app/{router,layout}`, `design-system/{base,tokens}.css`, `lib/api-client.ts` (mới có `get`), thư mục `features/*` **rỗng** |
| `client/src-tauri/` | Shell Rust, edition 2024, plugin `dialog` + `opener` |

---

## 3. Năm cơ chế phải hiểu trước khi sửa bất cứ thứ gì

Cả năm đều hỏng **âm thầm** nếu làm sai — không có thông báo lỗi nào chỉ đúng chỗ.

**1. Một dữ liệu kế toán = một PG schema** (ADR-017). Mỗi transaction mở bằng
`SET LOCAL search_path TO "ds_<mã>", public, pg_temp`. Schema đích **chỉ** lấy từ
bảng `datasets`, không suy từ mã trong token. Không có khóa ngoại chéo schema —
mỗi dataset phải `pg_dump`/restore độc lập được.

**2. Vai trò runtime không sở hữu bảng nào. `ket_app` NOINHERIT, `ket_control` là nhóm, `ds_*_app` per-dataset.**
`ket_owner` sở hữu tất cả, chạy DDL, có CREATEROLE. `ket_app` chạy ứng dụng, là thành viên của mọi
`ds_*_app` (để `SET ROLE`), cấp bằng `GRANT ds_<mã>_app TO ket_app`. Đây là **điều kiện cần**
cô lập dữ liệu: `ket_app` không được quyền bảng trực tiếp, chỉ qua vai trò dataset. Chạy
migration bằng vai trò runtime là vô hiệu hóa cơ chế này mà mọi test khác vẫn xanh.

**3. Cô lập chi nhánh nằm ở DB, không ở tầng ứng dụng.** Policy đọc GUC
`ket.branch_ids` đặt theo transaction. GUC chưa đặt → **không thấy dòng nào**
(fail-closed), không phải thấy tất. Lý do không dùng `WHERE` ở tầng app: một
truy vấn báo cáo quên lọc là rò dữ liệu, và `SUM() OVER ()` rò cả số tổng mà
không rò dòng nào.

**4. Nhật ký ghi trong CÙNG transaction với nghiệp vụ.** Listener bắt mọi thay
đổi qua ORM: `before_flush` chụp diff (chỉ ở đó còn history), `after_flush` ghi
(chỉ ở đó mới có khóa chính), `after_rollback`/`after_soft_rollback` dọn. Thay
đổi mà transaction không khai người thực hiện → **chặn thao tác**.
⚠️ `Query.update()/delete()` hàng loạt và SQL text đi thẳng **không** qua
listener — đường ghi set-based của phase 4/8 phải tự gọi `record_action()`.
Bảng schema **điều khiển** (`users`, `auth_sessions`) cũng không đi qua listener
(nó chỉ biết `DatasetBase`): sự kiện tài khoản ghi tường minh bằng
`record_control_action()` vào `public.control_audit_log`, cùng transaction.

**5. Đăng nhập chạy TRƯỚC khi chọn dữ liệu kế toán.** Nhóm `/api/v1/auth` dùng
`control_session` — không `SET ROLE`, không `search_path`, không GUC chi nhánh —
vì lúc kiểm mật khẩu chưa biết người dùng sắp mở doanh nghiệp nào. Hệ quả kéo
theo: quyền là per-dataset nhưng **cờ bắt buộc 2FA phải toàn cục**
(`users.totp_required`), và nhật ký sự kiện tài khoản phải nằm ở schema điều
khiển chứ không ở dataset nào. An toàn của đường này không đến từ `SET ROLE` mà
từ chỗ `ket_app` chỉ được cấp quyền trên đúng năm bảng điều khiển.

Hai cơ chế của luồng đăng nhập là **đọc–sửa–ghi** trên một dòng `users` (bộ đếm
khóa tài khoản, bước thời gian TOTP đã dùng) nên câu truy vấn phải giữ
`with_for_update()`. Bỏ nó ra thì cả hai vô hiệu dưới truy cập song song **mà
mọi test tuần tự vẫn xanh** — đo được: 10 lần sai đồng thời để lại
`failed_login_count = 1` thay vì khóa tài khoản.

---

## 4. Chạy tại máy

```bash
# 1. PostgreSQL 16 đang chạy trên cổng 5433, tài khoản hiện tại là superuser
# 2. Tạo database + vai trò DB (một lần cho mỗi cụm)
psql -p 5433 -d postgres -c 'CREATE DATABASE ket'
psql -p 5433 -d ket -f server/src/ket/kernel/security/roles.sql

# 3. Chạy test
make check          # server + client
make server-test    # không cần DB, không cần cấu hình gì
make server-test-db # cần PostgreSQL 16; Makefile tự đặt PGPORT=5433 + KET_TEST_DESTRUCTIVE_CLUSTER=1
make server-coverage # toàn bộ test + số độ phủ (con số CI dán vào PR)
```

Bộ test tự dựng database `ket_test`, tự tạo vai trò, tự provision hai dataset
(`alpha`, `beta`) rồi **xóa và dựng lại từ đầu mỗi phiên** — dữ liệu sót lại có
thể làm một test xanh vì lý do sai. Để xóa vai trò cụm cần `KET_TEST_DESTRUCTIVE_CLUSTER=1`
(đặt sẵn `Makefile` + CI).

Kết nối không mật khẩu (`trust`/`peer` cục bộ). Ghi đè bằng
`KET_TEST_ADMIN_DSN`, `KET_TEST_KET_OWNER_DSN`, `KET_TEST_KET_APP_DSN`.

> **Khởi tạo cụm bằng CLI**: `uv run python -m ket.admin ensure-cluster` rồi
> `create-user`. Khởi động app server trên một database chỉ mới chạy `roles.sql`
> vẫn bị chặn đúng như thiết kế: `SchemaVersionMismatchError: Chưa dựng schema
> điều khiển`. `provision_dataset()` còn là **hàm Python** (chưa có lệnh) —
> endpoint tạo dữ liệu kế toán thuộc lát sau; installer thuộc phase 11.

---

## 5. Bộ test (**203 case**: 140 cần PostgreSQL 16, 63 không)

| Tệp | Chứng minh điều gì |
| --- | --- |
| `test_audit_immutability_owner_split.py` (8) | `ket_app` không UPDATE/DELETE/TRUNCATE/DROP/DISABLE-RLS được `audit_log`; không sở hữu bảng nào |
| `test_audit_pending_batch_lifecycle.py` (3) | Flush hỏng không để lại dòng nhật ký cho thao tác đã bị hủy; ảnh chụp bản ghi mới mang giá trị thật |
| `test_rls_branch_isolation.py` (6) | Chi nhánh A không thấy B, **kể cả qua `count() OVER ()`**; GUC không sống qua transaction |
| `test_rls_policy_coverage.py` (3) | **Theo metadata**: mọi bảng có `branch_id` phải có policy — canh cho các phase sau |
| `test_dataset_routing.py` (21) | Hai dataset không thấy dữ liệu/đánh số/nhật ký của nhau; whitelist tên schema |
| `test_search_path_shadowing.py` (4) | Không che được bảng thật bằng `pg_temp` (kiểm **từng lớp** phòng thủ riêng) |
| `test_startup_schema_version_gate.py` (8) | App khởi động được khi đã có dataset; lệch revision **hoặc** PostgreSQL cũ hơn phiên bản đích → **từ chối khởi động** |
| `test_dataset_role_isolation.py` (15) | **Mới**: `ket_app` trần không đọc được bảng dataset nào; phiên đã bind `ds_alpha` bị từ chối khi ghi rõ `ds_beta.x`; hình dạng vai trò (NOINHERIT, không LOGIN, không ACL cho `ket_app`); dựng lại vai trò sau khôi phục; từ chối vai trò không quản trị được |
| `test_alembic_offline_sql.py` (1) | **Mới**: `alembic upgrade --sql` chạy được không cần DB — migration lấy schema từ `Config.attributes` chứ không hỏi `current_schema()` |
| `test_no_sql_string_interpolation.py` (4) | **Mới**: quét AST, cấm f-string/chuỗi nối trong `text()`/`exec_driver_sql()`; năm tệp sinh DDL identifier miễn trừ kèm lý do |
| `test_transaction_scope_does_not_leak_through_pool.py` (2) | **Mới**: trên `QueuePool` một connection, transaction kế tiếp phải sạch cả vai trò, GUC chi nhánh lẫn `search_path` |
| `test_migrations_match_models.py` (1) | Model và migration mô tả cùng một schema |
| `test_money_rounding.py` (20) | Bảng giá trị biên: nửa lên, số âm, làm tròn một lần ở cuối |
| `test_no_float_in_domain.py` (4) | Không `float` trong `src/ket` **và** trong `migrations/` |
| `test_app_smoke.py` (2) | Khung app dựng được, OpenAPI sinh được |
| `test_control_audit_immutability.py` (9) | **Mới**: `control_audit_log` chỉ-thêm ép ở tầng DB (5 đường sửa/xóa/DDL đều bị từ chối; quyền đo được đúng `INSERT,SELECT`); `public` chỉ hợp lệ ở đường cấp quyền, đường định tuyến vẫn từ chối |
| `test_control_schema_upgrade.py` (8) | **Mới**: diễn tập nâng cấp v1→v2 trên database dùng một lần **có dữ liệu**; chạy lại được; bảng mới được cấp quyền trên cụm đã nâng cấp; hai đường dán nhãn phiên bản khống đều bị chặn |
| `test_auth_login_flow.py` (18) | **Mới**: cấp phiên, chỉ lưu băm token, đường sai vẫn commit vết + bộ đếm, khóa tạm và tự hết khóa, vô hiệu hóa tài khoản giết phiên ngay, vòng đời 2FA, bí mật không bao giờ ở dạng rõ |
| `test_auth_concurrency.py` (2) | **Mới**: 10 lần sai **đồng thời** vẫn khóa tài khoản; một mã TOTP chỉ đổi được đúng một phiên |
| `test_auth_api_contract.py` (13) | **Mới**: hợp đồng HTTP + RFC 7807 — mã lỗi, `correlation_id`, token rác là 401 chứ không 500, 500 không lộ traceback |
| `test_admin_cli.py` (10) | **Mới**: từng lệnh `ket.admin` chạy đúng cách người vận hành gọi; lỗi nghiệp vụ in một dòng, không traceback |
| `test_password_policy.py` (11) | **Mới**: chính sách mật khẩu; hash giả của đường chống liệt kê người dùng phải **thật** và đúng tham số |
| `test_totp_second_factor.py` (9) | **Mới**: cửa sổ chấp nhận và chống dùng lại mã |
| `test_keystore_secret_box.py` (8) | **Mới**: thiếu khóa/sai khóa/keystore hỏng đều fail-closed, không rơi về lưu dạng rõ |

Độ phủ **92%** trên `src/ket`. Máy không có DB thì bỏ qua; CI đặt `KET_TEST_REQUIRE_DB=1` để **đỏ** thay vì bỏ qua.

---

## 6. Việc tiếp theo

Lát **2B-1** (tiếp): nhật ký thay đổi tài khoản → `public.control_audit_log`.
Lát **2B-2**: vai trò `ket_worker` riêng, worker claim rồi mới đặt GUC.
Lát **2C**: client + ba spike S1/S3/S4.

**Còn mở:**

1. Vai trò `ket_worker` (D2) nhìn dữ liệu dataset thế nào — cũng `SET ROLE ds_*_app`
   (khi đó nó cũng phải là thành viên của mọi vai trò dataset, mở lại đúng bề mặt
   tiêm-nhiều-câu-lệnh của ADR-017), hay chỉ là thành viên `ket_control`? Chốt
   trước khi viết worker.
2. Độ phủ có đặt ngưỡng chặn không — hiện 92%, CI mới chỉ **báo cáo**.
3. Đường build Windows và bộ cài chưa ký chứng thư OS — xác minh ở lần chạy
   `release.yml` đầu tiên.
