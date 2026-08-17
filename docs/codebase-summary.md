# Tóm tắt mã nguồn — Konek Két

**Cập nhật:** 2026-08-17 · **Trạng thái:** phase 1 xong; phase 2 xong lát 2A–2C-5 (còn 2C-6: spike S1 esign + S4 đóng gói, chờ phần cứng); phase 3 xong lát 3A + 3B-1 + 3B-2 (registry + 19 danh mục + chiều phân tích + gộp bản ghi).

Tài liệu này mô tả **thứ đang có thật trong repo**. Kiến trúc đích của cả v1
nằm ở `docs/system-architecture.md` — phần lớn nội dung ở đó chưa dựng.

---

## 1. Đang có gì

`server/src/ket` ≈ **5.400 dòng Python**, tất cả là **hạ tầng**: định tuyến dữ
liệu, phân quyền tầng DB, nhật ký, phép tính tiền, danh tính. **Chưa có một
nghiệp vụ kế toán nào** — không chứng từ, không sổ cái, không báo cáo.

| Có thật | Chưa có |
| --- | --- |
| Vai trò DB tách đôi, RLS, nhật ký bất biến, **cô lập dataset bằng vai trò per-dataset** | Bảng chứng từ / `gl_postings` / số dư |
| **RBAC tới cấp `{module}.{chứng từ}.{hành vi}`** + `require_permission`, **định tuyến dataset theo header `X-Dataset`**, phạm vi chi nhánh cho RLS | Bảng chứng từ / `gl_postings` / số dư |
| **Idempotency cùng transaction** (giành khóa → làm việc → điền kết quả), **khóa lạc quan `row_version`**, **tùy chọn hai cấp**, **hạn mức request** | Bảng chứng từ / `gl_postings` / số dư |
| **Sổ đăng ký danh mục** — `CatalogRegistry` + `CatalogSpec` (slug, model, extra_fields, flags, references); **router sinh tự động từ registry** — 7 thao tác/danh mục (GET danh sách/một, POST/PUT/DELETE/chuyển nhánh, gộp bản ghi), 175 operation tổng; phạm vi chi nhánh trong cấu trúc; `CatalogFlag` (bộ lọc `?flag=` theo cột boolean); `CatalogReference` (kiểm khóa ngoại sang danh mục khác từ DB lúc chạy) | — |
| **19 danh mục** — 15 danh mục lát 3B-1 + 2 chiều lõi lát 3A + `partners`/`employees` lát 3B-2: `projects`, `project_types`, `contracts`, `warehouses`, `units_of_measure`, `asset_types`, `tool_types`, `payment_terms`, `banks`, `timekeeping_symbols`, `document_types`, `invoice_forms`, `pit_tables`, `excise_tax_tables`, `resource_tax_tables`, `partners`, `employees`, `cost_objects`, `expense_items` | Dòng 4–6 của SRS §1–2 hoãn tới phase 5/7/9 |
| **Chiều phân tích mở rộng** — hai bảng `analysis_dimensions` + `analysis_dimension_values`, `DimensionService`, gieo mầm "Mã thống kê" (STAT, FR-SYS-051) lúc cấp dữ liệu kế toán; `value_source` + `master_slug` phân tách (không chuỗi ghép) | — |
| **Hàng đợi job + tiến trình worker riêng** (không FastAPI); **lease/heartbeat/reaper** chống job mồ côi; **vai trò `ket_worker`**; **API `/api/v1/jobs` + OpenAPI → type TypeScript** | Các module nghiệp vụ kế toán (phase 4 trở đi) |
| Schema-per-dataset + provisioning + `ensure_cluster` + `repair_dataset_privileges_statements` + **gieo mã quyền/vai trò `admin`** | API nghiệp vụ (mới có `/health`, `/api/v1/auth/*`, `/api/v1/system/*`) |
| **Đăng nhập Argon2id (có trần đồng thời), phiên lưu DB thu hồi được ngay, 2FA TOTP chống phát lại, khóa tạm khi dò mật khẩu** | Client (mới là bộ khung rỗng) |
| **Phiên hạn chế `totp_enrollment`** — tài khoản bị bắt bật 2FA tự đăng ký thiết bị được, không cần ai chạm máy chủ | Bắt tay schema-version với client |
| **Khóa mã hóa từ OS keystore** (fail-closed) — `totp_secret` không bao giờ ở dạng rõ trong DB/backup | Bộ cài app server (S4/phase 11); ký chứng thư OS |
| **Hợp đồng lỗi RFC 7807** + mã tương quan mỗi request (mã HTTP khai ở lớp lỗi) | — |
| 13 bảng nền + 2 bảng điều khiển mới, migration `0001`, **schema điều khiển có bước nâng cấp tường minh (v4)** | — |
| `money` (Decimal, ROUND_HALF_UP) + **luật cứng quét AST cấm tiêm SQL không tham số** | — |
| **`python -m ket.admin`**: ensure-cluster · create-user · reset-password · reset-totp · grant-role · grant-branch · generate-app-key · **prune-sessions · prune-idempotency-keys** | — |
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
| `api/` | Tầng HTTP: `middleware/{request_context,problem_details,rate_limit}`, `dependencies` (principal theo trạng thái phiên, dataset, quyền, `require_permission`), **`idempotency` (đọc khóa + đánh dấu route để test coverage bắt endpoint quên khai)**, `routers/{auth,system,system_settings}` (+ `*_schemas`). **`kernel` không được import tầng này** (contract C1) | Thêm endpoint, đổi hợp đồng lỗi |
| `admin/` | `python -m ket.admin` — lệnh chạy tại máy chủ, không qua HTTP | Khởi tạo cụm, tài khoản phá-kính |
| `kernel/security/` | `rls`, `tenant` (GUC chi nhánh), `grants`, `dataset_roles`, `roles.sql`, `models` (RBAC + `branches` + `settings`); `passwords` (Argon2id + trần đồng thời), `totp`, `keystore`, `auth_models` (`auth_sessions` + `SessionScope`), `auth_service` (đăng nhập/phiên/kiểm mật khẩu hiện tại), `account_service`; **`permissions` (registry loại chứng từ → mã quyền), `authorization` (quyền hiệu lực, cấm thắng cho phép), `role_service` (gieo mầm + gán vai trò/chi nhánh)** | Chạm phân quyền hoặc danh tính |
| `kernel/auditing/` | `models` (`audit_log` của dataset), `listener` (4 móc `Session`), **`control_log` (`control_audit_log` — sự kiện tài khoản, ghi tường minh vì `User` không đi qua listener)** | Hiểu vì sao mọi thay đổi đều có vết |
| `kernel/datasets/` | `naming` (trần 60→56 ký tự), `models` (schema điều khiển), **`bootstrap` (`ensure_control_schema`, `ensure_dataset_roles`, `ensure_cluster`), `provisioning` (`assert_dataset_role_administrable`)**, `service` | Tạo/định tuyến dữ liệu kế toán |
| `kernel/idempotency/` | `models` + **`service` (`execute_once`: giành khóa → làm việc → điền kết quả, tất cả trong một transaction)** | Thêm endpoint POST đổi trạng thái |
| `kernel/config/` | **`catalog` (danh mục khóa tùy chọn, đóng), `settings_service` (phân giải user → system → mặc định)** | Thêm một tùy chọn cấu hình |
| `kernel/persistence/versioning.py` | **Mixin `RowVersioned` + `require_row_version`** — khóa lạc quan hai lớp | Bảng người dùng sửa qua form |
| `kernel/jobs/` | `models` (bảng `jobs`, `ResumeSemantics`, `JobStatus`), **`registry` (loại job + quyền + semantics), `queue` (giành job), `reaper` (dọn mồ côi), `builtin` (ba loại job mẫu)** | Thêm loại job mới, chạm job metadata |
| `kernel/numbering/` | `models` (`number_sequences` + sổ cấp số `allocated_numbers`, `ResetRule`), **`service` (`NumberingRule` + `NumberingService`: `FOR UPDATE` trong transaction của người gọi, nên rollback trả lại số)** | Cấp số chứng từ, đổi quy tắc đánh số |
| `kernel/identifiers.py` | **`uuid7()` RFC 9562 tự viết** — `uid` ổn định của danh mục (RT-19). Khóa bảo vệ **tính đơn điệu**, không phải tính duy nhất | Đụng khóa danh mục |
| `kernel/master_data/` | **`registry` (đăng ký danh mục + loại quyền), `tree_path` (materialized path, chuyển nhánh bằng một UPDATE), `base` (`MasterDataRow`), `service` (`MasterDataService[ModelT]` generic), `usage` (bộ đếm tham chiếu), `models/` (15 danh mục + 2 danh mục lõi)** | Thêm một danh mục → thêm model + khai registry |
| `kernel/dimensions/` | **`models` (`analysis_dimensions`/`analysis_dimension_values`), `service` (API cây giá trị + kiểm nguồn), `seed` (gieo chiều lõi)** | Thêm chiều → thêm dòng vào bảng giá trị |
| `kernel/currency/` | **`models` (`currencies`/`exchange_rates`), `money_fc` (`MoneyFc` kiểm bất biến lúc dựng), `exchange_rate_service` (tra tỷ giá gần nhất ≤ ngày; thiếu → lỗi, **không bao giờ dùng 1**)** | Chạm nguyên tệ hoặc quy đổi |
| `kernel/periods/` | **`models` (`fiscal_years`/`accounting_periods`), `service` (sinh 12 kỳ, tra kỳ, khóa/mở có vết)** | Chạm kỳ kế toán, khóa sổ |
| `kernel/organization/service.py` | **`BranchService`** — cây chi nhánh. Bảng `Branch` vẫn ở `kernel/security/models.py` vì nó là neo cô lập dữ liệu của luồng đăng nhập | Thêm/chuyển chi nhánh |
| `kernel/persistence/sequences.py` | **`reserve_id`** — lấy khóa chính trước khi `INSERT` để `path` chứa đúng id | Bảng cây mới |
| `api/routers/master_data` | **Router sinh tự động từ registry** — 7 thao tác × 19 danh mục = 175 operation. Endpoint `/api/v1/master/{slug}` (GET danh sách/một, POST, PUT, DELETE, PUT .../parent, POST .../actions/merge). Response model sinh động từ `pydantic.create_model` + `extra_fields`. RLS chi nhánh + quyền theo danh mục | Danh mục mới = không cần đổi router |
| `api/routers/dimensions` | **API `/api/v1/dimensions`** — đọc chiều + cây giá trị, khai chiều mới, thêm giá trị (chưa có sửa/xóa; UI người dùng cuối hoãn v1.1 theo RT-20) | Chiều mở rộng, giá trị mới |
| `api/routers/jobs` | **API `/api/v1/jobs/{types,list,detail,cancel}`** + schema request/response | Thêm loại job, đổi hợp đồng |
| `worker/` | **`__main__.py` (điểm vào `python -m ket.worker`), `runner` (vòng lặp), `progress` (tiến độ + hủy), `contracts`** | Đổi cơ chế giành/chạy job |
| `modules/*`, `posting/`, `reporting/` | Chỉ có `contracts.py` rỗng — chỗ giữ sẵn cho phase sau | — |

### Ngoài server

| Đường dẫn | Trạng thái |
| --- | --- |
| `server/migrations/` | `env.py` (chạy per-schema) + `versions/0001_core_platform.py` |
| `server/scripts/export_openapi.py` | **Xuất OpenAPI từ `create_app()` ra JSON**, không cần DB. Chạy: `uv run python scripts/export_openapi.py <đường-dẫn.json>` hay `make api-types` |
| `client/packages/api-types/` | **Sinh từ OpenAPI**: `schema.d.ts` (type TypeScript), `openapi.json` (spec). Cả hai **được COMMIT** là bản ghi hợp đồng. Tạo bằng `openapi-typescript` |
| `client/src/` | **Lát 2C-1..2C-3:** `main.tsx`; `app/{providers,router,session-gate,app-layout,navigation,placeholder-page,error-boundary}`; `design-system/{base,tokens}.css` + 13 component (`button`, `text-field`, `select-field`, `alert`, `tabs`, `seg`, `status-pill`, `next-action-cell`, `data-table`, `drawer`, `split-pane`, `checklist-panel`, và **`data-grid/`** = lưới nhập liệu, spike S3); `lib/{api-client,session,session-storage,app-version,i18n,formatters,access,theme,safe-storage}`; `features/auth/*` (đăng nhập, đổi mật khẩu tạm, đăng ký 2FA, cần cập nhật, mất kết nối) + `features/dataset`; `locales/{vi,en}.ts`. **Chỉ có ở bản dev** (gác bằng `__DEV_TOOLS__`): `features/kitchen-sink/` và `features/bench/` |
| `client/bench/` | Bộ đo hiệu năng lưới nhập liệu trên Chromium thật (`make client-bench`, job CI `client-bench`). Ngưỡng spike S3 nằm trong chính bài đo |
| `client/src-tauri/` | Shell Rust, edition 2024, plugin `dialog` + `opener` |

---

## 3. Bảy cơ chế phải hiểu trước khi sửa bất cứ thứ gì

Cả bảy đều hỏng **âm thầm** nếu làm sai — không có thông báo lỗi nào chỉ đúng chỗ.

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

**6. Quyền là per-dataset; request phải khai dataset, và mặc định của mọi cổng là CHẶN.**
Mã quyền `{module}.{chứng từ}.{hành vi}` **sinh từ registry** (`kernel/security/permissions.py`),
không gõ tay — thêm loại chứng từ mới không phải đụng schema quyền. Phân giải
quyền đọc `user_roles → role_permissions`, và **cấm tường minh thắng cho phép**
(`allow = false` thắng mọi vai trò khác). Không có vai trò nào trong dataset =
`dataset.access_denied`, không phải "tập quyền rỗng".

Ba trạng thái phiên đều **đóng theo mặc định**: thiếu header `X-Dataset` → 400 (không
có mặc định "dataset đầu tiên" — đoán sai là ghi sổ nhầm doanh nghiệp); phiên
`totp_enrollment` chỉ mở đúng đường đăng ký 2FA; `must_change_password` chặn mọi
endpoint trừ đổi mật khẩu/`me`/`logout`. Endpoint mới không khai gì sẽ bị chặn cả ba
— hướng mặc định phải là như vậy.

Cờ `users.totp_required` là **toàn cục** còn vai trò là per-dataset, nên gán một vai
trò nhạy cảm chạm hai schema và không thể nguyên tử. Thứ tự bắt buộc: **bật cờ trước,
ghi vai trò sau** (`role_service.grant_role`). Đảo lại là tạo ra tài khoản quản trị
không bị đòi lớp thứ hai, im lặng.

Và phạm vi chi nhánh **không tự nới được**: qua HTTP, chỉ gán/thu được chi nhánh mà
chính người thực hiện đang thấy. Không có luật đó thì ai nắm `system.user.edit` cũng
tự cấp mình mọi chi nhánh còn lại trong một request — và dòng nhật ký của thao tác ấy
mang chi nhánh **đích**, tức là vô hình với chính người vừa bị vượt qua. Đường CLI
(`actor_branch_ids=None`) là phá-kính có chủ đích: lúc dựng bản cài chưa ai thấy chi
nhánh nào.

**7. Hàng đợi job chạy trong tiến trình riêng, không FastAPI.** Worker giành job
bằng `FOR UPDATE SKIP LOCKED` dưới danh tính `ket_worker`, rồi mới chạy thân job
dưới `SET LOCAL ROLE ds_<mã>_app` + phạm vi chi nhánh từ `jobs.branch_id`. Hai
lý do không chạy trong FastAPI: GIL (job tính toán bận sẽ khóa request khác) và
hai DSN khác nhau (`KET_WORKER_DATABASE_URL` vs `KET_DATABASE_URL`). Worker tự
quản lease/heartbeat/reaper (RT-13): nếu worker chết, reaper quét và xếp lại job
vào hàng. Ba loại job mẫu có sẵn: `system.diagnostic.slow_task` (chứng minh tiến
độ), `system.maintenance.prune_idempotency_keys` (per-dataset), `system.maintenance.prune_sessions`
(chạm bảng dùng chung của cả bản cài: đòi quyền `system.installation.create` có
2FA **và** một DSN owner khai tường minh cho worker; mặc định không có thì job
hỏng fail-closed). Mọi loại job mới phải khai bốn thuộc tính — `code`,
`permission`, `resume_semantics`, `params_model` — và loại nào cần kết nối đặc
quyền phải nằm trong danh sách đóng `CONTROL_OWNER_JOB_TYPES`.

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

## 5. Bộ test (**847 case**: 493 cần PostgreSQL 16, 354 không) — lát 3B-1 tăng 145 test; lát 3B-2 tăng 104 test

| Tệp | Chứng minh điều gì |
| --- | --- |
| `test_audit_immutability_owner_split.py` (8) | `ket_app` không UPDATE/DELETE/TRUNCATE/DROP/DISABLE-RLS được `audit_log`; không sở hữu bảng nào |
| `test_audit_pending_batch_lifecycle.py` (3) | Flush hỏng không để lại dòng nhật ký cho thao tác đã bị hủy; ảnh chụp bản ghi mới mang giá trị thật |
| `test_rls_branch_isolation.py` (6) | Chi nhánh A không thấy B, **kể cả qua `count() OVER ()`**; GUC không sống qua transaction |
| `test_rls_policy_coverage.py` (4) | **Theo metadata**: mọi bảng có `branch_id` phải có policy — canh cho các phase sau. Lát 3B-1 thêm cổng ngược lại: mọi danh mục phải được **khai miễn trừ từng bảng** (thử miễn trừ "suy theo lớp gốc" rồi đảo lại — đối trọng của nó là hằng đúng, H53) |
| `test_dataset_routing.py` (21) | Hai dataset không thấy dữ liệu/đánh số/nhật ký của nhau; whitelist tên schema |
| `test_search_path_shadowing.py` (4) | Không che được bảng thật bằng `pg_temp` (kiểm **từng lớp** phòng thủ riêng) |
| `test_startup_schema_version_gate.py` (8) | App khởi động được khi đã có dataset; lệch revision **hoặc** PostgreSQL cũ hơn phiên bản đích → **từ chối khởi động** |
| `test_dataset_role_isolation.py` (22) | **Mới**: `ket_app` trần không đọc được bảng dataset nào; phiên đã bind `ds_alpha` bị từ chối khi ghi rõ `ds_beta.x`; hình dạng vai trò (NOINHERIT, không LOGIN, không ACL cho `ket_app`); dựng lại vai trò sau khôi phục; từ chối vai trò không quản trị được |
| `test_alembic_offline_sql.py` (1) | **Mới**: `alembic upgrade --sql` chạy được không cần DB — migration lấy schema từ `Config.attributes` chứ không hỏi `current_schema()` |
| `test_no_sql_string_interpolation.py` (4) | **Mới**: quét AST, cấm f-string/chuỗi nối trong `text()`/`exec_driver_sql()`; năm tệp sinh DDL identifier miễn trừ kèm lý do |
| `test_transaction_scope_does_not_leak_through_pool.py` (2) | **Mới**: trên `QueuePool` một connection, transaction kế tiếp phải sạch cả vai trò, GUC chi nhánh lẫn `search_path` |
| `test_migrations_match_models.py` (1) | Model và migration mô tả cùng một schema |
| `test_money_rounding.py` (20) | Bảng giá trị biên: nửa lên, số âm, làm tròn một lần ở cuối |
| `test_no_float_in_domain.py` (4) | Không `float` trong `src/ket` **và** trong `migrations/` |
| `test_app_smoke.py` (2) | Khung app dựng được, OpenAPI sinh được |
| `test_control_audit_immutability.py` (9) | **Mới**: `control_audit_log` chỉ-thêm ép ở tầng DB (5 đường sửa/xóa/DDL đều bị từ chối; quyền đo được đúng `INSERT,SELECT`); `public` chỉ hợp lệ ở đường cấp quyền, đường định tuyến vẫn từ chối |
| `test_control_schema_upgrade.py` (11) | Diễn tập nâng cấp v1→v2 **và** v2→v3 trên database dùng một lần **có dữ liệu**; chạy lại được; bảng mới được cấp quyền trên cụm đã nâng cấp; phiên đang mở không bị hạ thành phiên hạn chế; hai đường dán nhãn phiên bản khống đều bị chặn |
| `test_auth_login_flow.py` (22) | **Mới**: cấp phiên, chỉ lưu băm token, đường sai vẫn commit vết + bộ đếm, khóa tạm và tự hết khóa, vô hiệu hóa tài khoản giết phiên ngay, vòng đời 2FA, bí mật không bao giờ ở dạng rõ |
| `test_auth_concurrency.py` (2) | 10 lần sai **đồng thời** vẫn khóa tài khoản; một mã TOTP chỉ đổi được đúng một phiên |
| `test_authorization_concurrency.py` (3) | **Mới**: gán vai trò / gán chi nhánh / gieo mầm chạy **song song** không sinh `UniqueViolation` (tức không thành HTTP 500 cho thao tác đã hứa là idempotent) |
| `test_auth_api_contract.py` (13) | **Mới**: hợp đồng HTTP + RFC 7807 — mã lỗi, `correlation_id`, token rác là 401 chứ không 500, 500 không lộ traceback |
| `test_admin_cli.py` (20) | Từng lệnh `ket.admin` chạy đúng cách người vận hành gọi; lỗi nghiệp vụ in một dòng, không traceback; **`prune-sessions` giữ phiên đang sống, và vai trò runtime vẫn bị DB từ chối `DELETE`** |
| `test_password_policy.py` (13) | **Mới**: chính sách mật khẩu; hash giả của đường chống liệt kê người dùng phải **thật** và đúng tham số |
| `test_totp_second_factor.py` (9) | **Mới**: cửa sổ chấp nhận và chống dùng lại mã |
| `test_keystore_secret_box.py` (8) | Thiếu khóa/sai khóa/keystore hỏng đều fail-closed, không rơi về lưu dạng rõ |
| `test_permission_registry.py` (15) | **Mới**: thêm loại chứng từ mới **không** phải đụng schema quyền; mã quyền đúng ba phần; nhật ký không có mã ghi |
| `test_authorization_access.py` (14) | **Mới lát 2B-2b**: cấm thắng cho phép; không vai trò = `dataset.access_denied`; gieo mầm chạy lại được và cấp mã quyền mới cho `admin`; **thứ tự fail-safe** — ghi vai trò hỏng vẫn để lại cờ 2FA đã bật |
| `test_idempotency_same_txn.py` (11) | **Mới lát 2B-2b**: gửi lại → đúng một bản ghi; lệnh ghi hỏng → **khóa cũng biến mất**; khóa hết hạn fail-closed; khóa của người khác không dùng lại được; **4 luồng cùng khóa → đúng một lần thực hiện**; lỗi ràng buộc nghiệp vụ không bị hóa trang thành tranh chấp khóa |
| `test_idempotency_route_coverage.py` (6) | **Mới**: mọi route `POST` phải khai idempotency hoặc nằm trong danh sách miễn trừ **trong mã nguồn**; khóa route không trùng và vừa cột |
| `test_optimistic_locking.py` (2) | **Mới**: hai transaction cùng đọc một phiên bản → chỉ một ghi được (`StaleDataError`); handler đổi nó thành `409`, không phải `500` |
| `test_settings_service.py` (9) | **Mới**: user đè system đè mặc định; ràng buộc kiểu/khoảng/danh sách chọn; **4 luồng cùng ghi lần đầu → một thắng, ba nhận `409` chứ không phải lỗi DB thô** |
| `test_rate_limit.py` (9) | **Mới**: chặn quá hạn mức kèm `Retry-After`; `429` vẫn là RFC 7807 **có `correlation_id`** (khóa thứ tự middleware); nhóm `auth` có ngân sách riêng; `/health` miễn trừ; hai người gọi không dùng chung ngân sách; **header `Authorization` bịa không mua được ngân sách mới, và bơm định danh giả không xóa được bộ đếm người thật** |
| `test_write_contract_api.py` (16) | **Mới**: chuỗi hợp đồng ghi qua HTTP — thiếu `X-Idempotency-Key` là `400`, gửi lại là `200` (không phải `201`), cùng khóa khác nội dung là `409`; người lưu sau nhận `409` **kèm bản mới nhất**; `row_version` không lọt vào nhật ký; tùy chọn riêng không rò sang người khác; sửa tùy chọn cấp hệ thống cần quyền riêng |
| `test_api_authorization.py` (16) | **Mới lát 2B-2b**: chuỗi định tuyến đầu-cuối qua HTTP; thiếu/sai `X-Dataset`; thiếu quyền nêu đúng mã còn thiếu; **RLS cắt `/system/audit-log` dù câu truy vấn không lọc chi nhánh**; mật khẩu tạm và phiên hạn chế chặn ở server; vòng đăng ký 2FA đi trọn bằng HTTP |
| `test_master_data_registry.py` (non-db) | **Lát 3B-1**: mọi lớp con `MasterDataRow` phải có trong registry; `extra_fields` khớp cột hai chiều; response model không thiếu trường; đủ 4 mã quyền, không thừa `post`/`print`; đủ 6 route cho **mỗi** danh mục; slug phải là identifier; đăng ký trùng bị từ chối |
| `test_master_data_api.py` (db) | **Lát 3B-1**: CRUD danh mục cây thuần + danh mục có cột riêng; validator liên-trường; phạm vi chi nhánh (404 không phải 403); quyền từng danh mục; chuyển nhánh; idempotency; khóa lạc quan |
| `test_analysis_dimensions.py` (db) | **Lát 3B-1**: gieo mầm + chạy lại; mã quyền tới bảng permissions; cây giá trị; duy nhất trong chiều; `subtree_of` không rò sang chiều khác; cha khác chiều bị từ chối; nguồn `master` trỏ slug có thật |

Tổng **847 test** (493 cần PostgreSQL 16). Máy không có DB thì bỏ qua; CI đặt `KET_TEST_REQUIRE_DB=1` để **đỏ** thay vì bỏ qua.

---

## 6. Việc tiếp theo

Lát **2C**: client (design system, layout, quản lý phiên, i18n) + ba spike S1/S3/S4 (lưới nhập liệu, IME Việt, đóng gói bộ cài).

**Còn mở:**

1. Độ phủ có đặt ngưỡng chặn không — CI mới chỉ **báo cáo**.
2. Đường build Windows và bộ cài chưa ký chứng thư OS — xác minh ở lần chạy
   `release.yml` đầu tiên.
3. Hạn mức request đếm **trong tiến trình**: chạy hai tiến trình API thì hạn mức
   thực tế nhân đôi. Chấp nhận ở quy mô LAN; chỗ sửa khi cần chính xác là chuyển
   bộ đếm xuống PostgreSQL, không phải thêm Redis vào bản cài.
4. Dọn phiên `prune_sessions` và `prune_idempotency_keys` có sẵn làm job (xếp hàng qua API); lịch tự động thuộc phase 3 hoặc sau (tuỳ chức năng Scheduler).
5. Bộ vai trò mẫu (`ke-toan`, `thu-quy`, `xem`) vẫn chờ gói cấu hình TT200/TT133
   ở phase 5 — hiện chỉ gieo `admin`.
