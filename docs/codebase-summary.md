# Tóm tắt mã nguồn — Konek Két

**Cập nhật:** 2026-09-05 (lát 7C-2) · **Trạng thái:** phase 1 xong; phase 2 xong lát 2A–2C-5 (còn 2C-6: spike S1 esign + S4 đóng gói, chờ phần cứng); **phase 3 xong**; **phase 4 xong** (posting engine, sổ cái hai sổ, số dư ban đầu, khóa kỳ, toàn vẹn); phase 5 xong lát 5A–5E (gói cấu hình, formula engine, statement builder, report engine metadata-driven, render job nền); **phase 6 xong** (6A: protocol, guards, auto-posting, TK ngân hàng; 6B: quỹ tiền mặt; 6C: thủ quỹ, bank, engine đối trừ chung; 6D: sao kê, đối chiếu, 2FA, số dư đầu kỳ chia TK; 6E-1: BFF, 9 báo cáo metadata; 6E-2: mẫu in 01-TT/02-TT; 6F: UI "Tiền vào tiền ra"; 6G-1: chiều bank_account, RLS sao kê; 6G-2: CRUD hồ sơ sao kê, đóng băng kernel); **phase 7 đang chạy** (7A xong: sổ phụ công nợ `ar_ap_ledger` + module `receivables`; 7B xong: module `purchase` server — hóa đơn mua, chi phí mua hàng phân bổ, guard ngưỡng nợ đối tác; **7C-1 xong**: danh mục bảng giá + bảng giá nhiều mức theo đơn vị + bậc chiết khấu + bộ định giá `kernel/pricing`; **7C-2 xong**: module `sales` server — hóa đơn bán năm nghiệp vụ, trả lại/giảm giá đối trừ hóa đơn gốc, định giá theo lô; cả ba chưa có UI client).

> ⚠️ **Nợ tài liệu:** mục 1–2 mới được cập nhật tới lát 6D; **phần mô tả chi tiết phase 4, 5A, 5C–5E chưa được viết lại** (tài liệu này đứng ở mốc 2026-08-18 trước đó). Cần một lượt refresh riêng.

Tài liệu này mô tả **thứ đang có thật trong repo**. Kiến trúc đích của cả v1
nằm ở `docs/system-architecture.md` — phần lớn nội dung ở đó chưa dựng.

---

## 1. Đang có gì

`server/src/ket` ≈ **50.400 dòng Python**: **hạ tầng** (định tuyến dữ
liệu, phân quyền tầng DB, nhật ký, phép tính tiền, danh tính), **posting engine + sổ cái hai sổ** (phase 4), **gói cấu hình TT99/TT133** (5A), **formula engine & statement builder** (5B), **report engine metadata-driven** (5C–5D), **render job nền** (5E), **protocol liên-module RT-18 + guards + auto-posting** (6A), **module quỹ tiền mặt (cash_book) hoàn chỉnh** (6B), **module ngân hàng (bank) + thủ quỹ (warehousing/treasurer) server** (6C), **sao kê + đối chiếu ngân hàng + số dư đầu kỳ ngân hàng** (6D), **BFF cashflow + 9 báo cáo phân hệ metadata-driven** (6E-1), **mẫu in chứng từ tiền 01-TT/02-TT/08a-TT** (6E-2), **chiều `bank_account` trên dòng sổ + RLS sao kê** (6G-1); phía client có **trọn bộ UI "Tiền vào tiền ra"** — thẻ + lưới + form phiếu, kiểm kê quỹ (6F-1), đối chiếu ngân hàng + thủ quỹ (6F-2); **CRUD hồ sơ sao kê + đóng băng kernel** (6G-2); **sổ phụ công nợ `ar_ap_ledger` + module `receivables`** (7A); **module mua hàng (`purchase`) server** — hóa đơn mua (năm nghiệp vụ gom trong một `kind`: hàng/dịch vụ/tài sản/hàng đi đường/trả lại), chi phí mua hàng phân bổ theo giá trị/số lượng/thủ công, guard ngưỡng nợ + nợ quá hạn đối tác (7B, **chưa có UI client**). **bộ định giá `kernel/pricing` + danh mục bảng giá + hai bảng con giá của vật tư** (7C-1, **chưa có UI client**); **module bán hàng (`sales`) server** — hóa đơn bán (năm nghiệp vụ gom trong một `kind`: hàng hóa/dịch vụ/trả lại/giảm giá/đại lý), chiết khấu thương mại trừ thẳng trên dòng, trả lại và giảm giá đối trừ hóa đơn gốc, định giá theo lô (7C-2, **chưa có UI client**). **Chưa có** module kho/lương/thuế (phase 8–9).

| Có thật | Chưa có |
| --- | --- |
| Vai trò DB tách đôi, RLS, nhật ký bất biến, **cô lập dataset bằng vai trò per-dataset** | Module ngân hàng / mua / bán (phase 6–7) |
| **RBAC tới cấp `{module}.{chứng từ}.{hành vi}`** + `require_permission`, **định tuyến dataset theo header `X-Dataset`**, phạm vi chi nhánh cho RLS | Mua / bán / công nợ / HĐĐT (phase 7) |
| **Idempotency cùng transaction** (giành khóa → làm việc → điền kết quả), **khóa lạc quan `row_version`**, **tùy chọn hai cấp**, **hạn mức request** | Kho / CCDC / TSCĐ (phase 8), thuế / lương / giá thành (phase 9) |
| **Formula engine** (lát 5B) — `ket.kernel.config.statements.formula`: parser (lexer + đệ quy xuống), account range (khớp tiền tố), evaluator (tô-pô) cho 7 hàm (`DR`/`CR`/`BAL`/`DR_PS`/`CR_PS`/`DR_NET`/`CR_NET`) | B03/LCTT (hoãn 10a — cần phát sinh đối ứng) |
| **Statement builder** (lát 5B) — `ket.reporting.statements`: lấy số từ `opening_balances` + `gl_postings` (**không** snapshot), lập BCTC theo layout, API + quyền `reporting.statement.view` | — |
| **Report engine metadata-driven** (lát 5C–5D) — `ket.reporting.engine`: metadata `report_definitions`, `ket.reporting.rendering` (PDF WeasyPrint + XLSX openpyxl), sandbox Jinja2 + URL fetcher chặn `file://`, tham số động per-report, phân trang ADR-009, preview lưới trực tiếp | — |
| **Render job nền** (lát 5E) — `ket.reporting.render_job`: `JobType.branch_scope` (REQUESTER_BRANCHES), threshold chuyển-job cấu hình, tiến độ + hủy trên UI báo cáo phía client | — |
| **Protocol liên-module RT-18** (lát 6A) — `ket.kernel.protocols`: `Protocol` registry + `PROVIDERS`; guard ba mức (lát 6A) — `PostingGuard` + `CashBalanceGuard` (FR-SYS-062), định khoản tự động `kernel/config/auto_posting_*` (FR-SYS-025) | — |
| **Danh mục thứ 21: TK ngân hàng doanh nghiệp** (lát 6A) — `company_bank_accounts`, search server-side `search=`/`ids=` trên `/api/v1/master/{slug}` | — |
| **Bộ báo cáo phân hệ Quỹ & Ngân hàng** (lát 6E-1) — 8 definition trên 6 dataset, mã theo Công báo TT99 Phụ lục III: `S07-DN` (sổ quỹ thủ quỹ), `S07a-DN`, `S08-DN`, `S03a1-DN`, `S03a2-DN` + `chenh-lech-so-quy-so-ke-toan`, `doi-chieu-ngan-hang`, `bang-ke-so-du-ngan-hang`. Cộng `du-bao-dong-tien` (6B) = **9 báo cáo phân hệ, 0 dòng renderer riêng**. Hai cột metadata mới: `fixed_params` (ghim giá trị tham số — hai mẫu sổ khác nhau một tham số dùng chung dataset) và `required_permission_module` (cổng quyền theo phân hệ) | TT133 cho quỹ/ngân hàng chờ kế toán duyệt |
| **Module quỹ tiền mặt (cash_book)** (lát 6B) — `ket.modules.cash_book`: service PT/PC, settlement_service đối trừ công nợ + FX (515/635 FR-SYS-066), posting_mapper cặp Nợ/Có, guards `CashBalanceGuard` soi số dư thấp nhất, count_sheet_service kiểm kê (FR-QUY-030/031), balance_service; `SettlementTargetSource` protocol + opening balances, hook vòng đời; router `/api/routers/cash_book.py`; dataset `cash_forecast` + definition `du-bao-dong-tien` (FR-QUY-032) | — |
| **Sổ đăng ký danh mục** — `CatalogRegistry` + `CatalogSpec` (slug, model, extra_fields, flags, references); **router sinh tự động từ registry** — 7 thao tác/danh mục (GET danh sách/một, POST/PUT/DELETE/chuyển nhánh, gộp bản ghi), 190+ operation tổng; phạm vi chi nhánh trong cấu trúc; `CatalogFlag` (bộ lọc `?flag=` theo cột boolean); `CatalogReference` (kiểm khóa ngoại sang danh mục khác từ DB lúc chạy) | — |
| **Ba cơ chế chung** — `merge_hooks` (số nhiều, từ chối/chuẩn bị gộp bản ghi), `extra_update_fields` (trường chốt một lần), `update_guard` (luật liên-trường ở đường sửa) | Lát 3B-3: người dùng thứ nhất là vật tư, bảng con đơn vị quy đổi + mã quy cách |
| **21 danh mục** — 15 danh mục lát 3B-1 + 2 chiều lõi lát 3A + `partners`/`employees` lát 3B-2 + `items` lát 3B-3 + `company_bank_accounts` lát 6A: `projects`, `project_types`, `contracts`, `warehouses`, `units_of_measure`, `asset_types`, `tool_types`, `payment_terms`, `banks`, `timekeeping_symbols`, `document_types`, `invoice_forms`, `pit_tables`, `excise_tax_tables`, `resource_tax_tables`, `partners`, `employees`, `cost_objects`, `expense_items`, `items`, `company_bank_accounts` | Dòng 4–6 của SRS §1–2 hoãn tới phase 6–9 |
| **Chiều phân tích mở rộng** — hai bảng `analysis_dimensions` + `analysis_dimension_values`, `DimensionService`, gieo mầm "Mã thống kê" (STAT, FR-SYS-051) lúc cấp dữ liệu kế toán; `value_source` + `master_slug` phân tách (không chuỗi ghép) | — |
| **Hàng đợi job + tiến trình worker riêng** (không FastAPI); **lease/heartbeat/reaper** chống job mồ côi; **vai trò `ket_worker`**; **API `/api/v1/jobs` + OpenAPI → type TypeScript** | Các module nghiệp vụ kế toán (phase 7 trở đi) |
| Schema-per-dataset + provisioning + `ensure_cluster` + `repair_dataset_privileges_statements` + **gieo mã quyền/vai trò `admin`** | API nghiệp vụ (mới có `/health`, `/api/v1/auth/*`, `/api/v1/system/*`, `/api/v1/reports/*`, `/api/v1/cash-book/*`) |
| **Đăng nhập Argon2id (có trần đồng thời), phiên lưu DB thu hồi được ngay, 2FA TOTP chống phát lại, khóa tạm khi dò mật khẩu** | Client (mới là bộ khung rỗng) |
| **Phiên hạn chế `totp_enrollment`** — tài khoản bị bắt bật 2FA tự đăng ký thiết bị được, không cần ai chạm máy chủ | Bắt tay schema-version với client |
| **Khóa mã hóa từ OS keystore** (fail-closed) — `totp_secret` không bao giờ ở dạng rõ trong DB/backup | Bộ cài app server (S4/phase 11); ký chứng thư OS |
| **Hợp đồng lỗi RFC 7807** + mã tương quan mỗi request (mã HTTP khai ở lớp lỗi) | — |
| 17 bảng nền + 2 bảng điều khiển, migration `0001`–`0017`, **schema điều khiển có bước nâng cấp tường minh (v4)** | — |
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
| `kernel/config/statements/` | **`formula/` (parser + account_range + evaluator), `models` (`StatementLayout`, `StatementRow`), fail-closed loader cho gói cấu hình** — grammar 7 hàm, kiểm công thức + rowref + chu trình + TK khớp | Thêm hàm công thức, kiểm schema layout mới |
| `reporting/statements/` | **`builder.py` (builder BCTC từ `opening_balances`+`gl_postings`), `balance_source.py` (SQL trực tiếp, không snapshot), `models`, API `/api/v1/statements`** | Thêm cột layout, loại báo cáo mới |
| `kernel/persistence/versioning.py` | **Mixin `RowVersioned` + `require_row_version`** — khóa lạc quan hai lớp | Bảng người dùng sửa qua form |
| `kernel/jobs/` | `models` (bảng `jobs`, `ResumeSemantics`, `JobStatus`), **`registry` (loại job + quyền + semantics), `queue` (giành job), `reaper` (dọn mồ côi), `builtin` (ba loại job mẫu)** | Thêm loại job mới, chạm job metadata |
| `kernel/numbering/` | `models` (`number_sequences` + sổ cấp số `allocated_numbers`, `ResetRule`), **`service` (`NumberingRule` + `NumberingService`: `FOR UPDATE` trong transaction của người gọi, nên rollback trả lại số)** | Cấp số chứng từ, đổi quy tắc đánh số |
| `kernel/identifiers.py` | **`uuid7()` RFC 9562 tự viết** — `uid` ổn định của danh mục (RT-19). Khóa bảo vệ **tính đơn điệu**, không phải tính duy nhất | Đụng khóa danh mục |
| `kernel/master_data/` | **`registry` (đăng ký danh mục + loại quyền + ba cơ chế chung), `tree_path` (materialized path, chuyển nhánh bằng một UPDATE), `base` (`MasterDataRow`), `service` (`MasterDataService[ModelT]` generic), `usage` (bộ đếm tham chiếu), `models/` (22 danh mục + 2 danh mục lõi)** | Thêm một danh mục → thêm model + khai registry |
| `kernel/quantity.py` | **Kiểu `NUMERIC(20,6)` cho số lượng + tỷ lệ quy đổi** — khóa `quantity.scale` hoãn phase 8 | Phép tính số lượng, quy đổi |
| `api/routers/master_data_guards.py` | **Phép kiểm phạm vi trích từ `master_data.py`** — bảng con vật tư (đơn vị quy đổi, mã quy cách, **mức giá, bậc chiết khấu** 7C-1) + đối tác (tài khoản ngân hàng) + **dòng bảng giá** (7C-1) là sáu người dùng | Thêm bảng con mới → cô lập kiểm phạm vi ở đây |
| `kernel/dimensions/` | **`models` (`analysis_dimensions`/`analysis_dimension_values`), `service` (API cây giá trị + kiểm nguồn), `seed` (gieo chiều lõi)** | Thêm chiều → thêm dòng vào bảng giá trị |
| `kernel/currency/` | **`models` (`currencies`/`exchange_rates`), `money_fc` (`MoneyFc` kiểm bất biến lúc dựng), `exchange_rate_service` (tra tỷ giá gần nhất ≤ ngày; thiếu → lỗi, **không bao giờ dùng 1**)** | Chạm nguyên tệ hoặc quy đổi |
| `kernel/periods/` | **`models` (`fiscal_years`/`accounting_periods`), `service` (sinh 12 kỳ, tra kỳ, khóa/mở có vết)** | Chạm kỳ kế toán, khóa sổ |
| `kernel/organization/service.py` | **`BranchService`** — cây chi nhánh. Bảng `Branch` vẫn ở `kernel/security/models.py` vì nó là neo cô lập dữ liệu của luồng đăng nhập | Thêm/chuyển chi nhánh |
| `kernel/persistence/sequences.py` | **`reserve_id`** — lấy khóa chính trước khi `INSERT` để `path` chứa đúng id | Bảng cây mới |
| `api/routers/master_data` | **Router sinh tự động từ registry** — 7 thao tác × 22 danh mục = 200+ operation. Endpoint `/api/v1/master/{slug}` (GET danh sách/một, POST, PUT, DELETE, PUT .../parent, POST .../actions/merge), search server-side `search=`/`ids=`. Response model sinh động từ `pydantic.create_model` + `extra_fields`. RLS chi nhánh + quyền theo danh mục | Danh mục mới = không cần đổi router |
| `api/routers/dimensions` | **API `/api/v1/dimensions`** — đọc chiều + cây giá trị, khai chiều mới, thêm giá trị (chưa có sửa/xóa; UI người dùng cuối hoãn v1.1 theo RT-20) | Chiều mở rộng, giá trị mới |
| `api/routers/jobs` | **API `/api/v1/jobs/{types,list,detail,cancel}`** + schema request/response | Thêm loại job, đổi hợp đồng |
| `api/routers/reports` | **API `/api/v1/reports/{definitions,preview,render}`** + schema request/response, định tuyến `JobType.branch_scope` | Báo cáo, render job |
| `api/routers/cash_book` | **API `/api/v1/cash-book/{vouchers,open-invoices,count-sheets}`** — tạo/sửa phiếu (hành động post/unpost/xóa đi qua `/api/v1/vouchers` chung), picker công nợ, biên bản kiểm kê (lát 6B) | Quỹ tiền mặt, đối trừ công nợ |
| `api/routers/auto_posting_schemas` | **Schema + model `auto_posting_*` (FR-SYS-025)** — định khoản tự động từ gói cấu hình | Auto-posting |
| `kernel/protocols.py` | **Protocol liên-module (RT-18)** — `ReceivableProvider`/`PayableProvider`/`SettlementTargetSource`/`InventoryPosting`/`CommitmentProvider` + registry `PROVIDERS` (module đăng ký bản cài lúc import qua `model_registry`) | Thêm provider = đăng ký lúc import, không sửa kernel |
| `worker/` | **`__main__.py` (điểm vào `python -m ket.worker`), `runner` (vòng lặp), `progress` (tiến độ + hủy), `contracts`** | Đổi cơ chế giành/chạy job |
| `posting/` | **Phase 4–6B** — `engine/` (`gl_postings`, validator, `PostingService`, `guards.py` PostingGuard), `documents/` (hook `after_post`/`after_unpost`/`before_delete` trên registry), `balances/` (snapshot + recalc + bảng cân đối), `opening_balances/` (`settlement_source.py` SettlementTargetSource protocol), `periods/` (khóa kỳ), `integrity/` | Ghi sổ, số dư, khóa kỳ |
| `reporting/` | **Phase 5C–5E** — `engine/` (metadata `report_definitions`, param động, executor, grouping), `rendering/` (PDF WeasyPrint + XLSX openpyxl, sandbox Jinja2), `statements/` (builder BCTC, balance_source), `printing/` (print_log, `template_service` render `DocumentPrintContext`), `render_job.py` (job nền, branch_scope, threshold), API `/api/v1/reports` | Báo cáo, render nền, BCTC |
| `modules/cash_book/` | **Phase 6B** — `service.py` (PT/PC, định khoản), `settlement_service.py` (đối trừ công nợ + FX 515/635), `posting_mapper.py` (Nợ/Có → 2 dòng), `guards.py` (CashBalanceGuard soi số dư thấp nhất), `count_sheet_service.py` (kiểm kê FR-QUY-030/031), `balance_service.py`, `models.py`, `schemas.py` | Ghi sổ quỹ, đối trừ công nợ |
| `modules/bank/` | **Phase 6C** — `service.py` (BC/UNC/SEC/CTNB, tiền tệ khớp TK ngân hàng, chuyển nội bộ cùng tiền tệ), `settlement_service.py` (vỏ mỏng trên `posting/settlements`), `posting_mapper.py` (money-side theo tiền tố 111/112), `models.py` (+`bank_settlements` 0018), `schemas.py`; router `/api/v1/bank/*` | Chứng từ tiền gửi, đối trừ công nợ |
| `modules/warehousing/treasurer/` | **Phase 6C** — `book.py` (bản cài `TreasurerCashBook`), `queue_service.py` (hàng đợi + ghi sổ hàng loạt 1 transaction, BR-WHK-05); cùng `cash_book/treasurer_source.py` (nguồn phiếu + sync sau post/unpost, FR-WHK-021) và cặp Protocol trong `kernel/protocols.py`; setting `treasurer.enabled` (mặc định tắt); router `/api/v1/treasurer/*` | Sổ quỹ thủ quỹ hai sổ song song |
| `posting/settlements.py` | **Phase 6C** — engine đối trừ + chênh lệch tỷ giá thu/trả (FR-SYS-066) dùng chung cho quỹ và ngân hàng (`money_in: bool`); integrity check thứ 8 `treasurer_book_matches_ledger` (BR-WHK-03) | Đối trừ công nợ mọi chứng từ tiền |
| `modules/bank/` (6D) | **Phase 6D** — `statement_import.py` (nhập sao kê CSV/XLSX theo `bank_statement_profiles`, trọn-hoặc-không, unique DB `(bank_account_id, content_hash)` chống nhập đúp, kiểm chéo cột số dư chịu ô thưa), `reconciliation.py` (khớp tự động ±3 ngày + tiebreak reference + đòi phạm vi mọi chi nhánh; khớp tay/gỡ; unique khớp THEO TÀI KHOẢN — CTNB nằm trên hai sao kê; unpost chứng từ đã khớp bị chặn cả hai cửa; carry-forward chia phát sinh 112 theo TK ngân hàng dựng lại ở 6G-2), `statement_merge.py` (hook gộp qua `CatalogRegistry.extend_merge_hooks` mới); router `/api/v1/bank/statements/*` + `/api/v1/bank/reconciliation`; quyền `bank.statement.*` riêng, `bank.ebanking.*` requires_second_factor (FR-NFR-016); sheet "Số dư ngân hàng" kind 1 + `opening_balances.bank_account_id` (migration 0019) | Đối chiếu ngân hàng, số dư đầu kỳ theo TK |
| **Chiều `bank_account` + RLS sao kê** (6G-1) | `kernel/config/accounts_models.py`: `DetailTracking.BANK_ACCOUNT` (chiều thứ 11) + hằng `CASH_ON_HAND_CODE_PREFIX`/`DEPOSIT_ACCOUNT_CODE_PREFIX` dùng chung ba tầng; cột `gl_postings.bank_account_id` (+ `cash_voucher_lines`, `gl_journal_lines`) — chủ sở hữu dòng 112x GHI lúc ghi sổ thay vì suy lúc đọc, luật sống một chỗ ở `bank/posting_mapper._deposit_owner`, xoá 6 bản chép; ba mapper chỉ dán chiều lên bên 112x; `bank_statements`/`bank_statement_lines` nhận `branch_id` (theo TK ngân hàng, `NULL` = dùng chung) + policy RLS, đặt qua `bank/statement_branch.sync_statement_branch` (một hàm, hai cửa); `require_bank_account` kiểm phạm vi ở cả bốn cửa; 7 báo cáo bộ sổ đóng cổng `general_ledger` + cổng ấy kiểm LẠI ở cửa tải tệp job render; loader ĐÒI và CẤM chiều theo tiền tố 112; migration 0022 (cột + index + backfill + RLS + `UPDATE` bật chiều cho dataset đã cấp; `_refresh_builtin_data` dời 0021→0022) | Thêm một chiều cố định cho dòng hạch toán |
| **Hạ tầng 6G-2** — phạm vi chi nhánh chung | `kernel/security/branch_scope.py` — hàm tra phạm vi có phủ mọi chi nhánh, dùng chung cho khóa kỳ (`posting/periods/lock_service`, trước đó giữ bản chép riêng), năm cửa đối chiếu ngân hàng, và cổng phạm vi của báo cáo; trả *chi nhánh còn thiếu* cho từng cửa gọi có câu lỗi riêng |
| **Cổng báo cáo theo phạm vi** | `report_definitions.requires_full_branch_scope` (migration 0023) — cột boolean canh công ty, khác `required_permission_module` (canh quyền): "con số đúng không" vs "được đọc phân hệ không"; `_may_open` giấu báo cáo người dùng không mở được |
| **Guard tham chiếu** | `posting/documents/registry.py::REFERENCE_GUARDS` — bộ guard "còn ai trỏ vào chứng từ này không", chạy trong `PostingService.unpost` (mọi cửa bỏ ghi sổ đều đi qua đó). **Không** chạy ở `VoucherService.delete`: guard hiện có canh chứng từ đã ghi sổ, mà đường xóa chỉ nhận chứng từ Đã cất — chiều xóa do FK `RESTRICT` canh. Module `bank` đăng ký luật "đã khớp sao kê thì không bỏ ghi sổ", nên `cash_book`/`general_ledger` không phải biết phân hệ ngân hàng tồn tại (C3 vẫn đứng). Guard đọc qua **migration 0024** `voucher_has_matched_statement_line` (SECURITY DEFINER, `search_path FROM CURRENT`) để nhìn thấy dòng sao kê NGOÀI phạm vi RLS người gọi |
| **Tính lại chiều suy-ra** (6G-2 M-9) | `posting/engine/dimension_recompute.py` — không viết lại luật, dựng lại `PostingRequest` mà module sẽ dựng, so từng dòng theo `(sổ, line_no)` + `order_by(ledger, line_no)`; hai job: `posting.dimensions.recompute` (báo, `apply=false` mặc định, mã quyền `posting.integrity.create`) và `posting.dimensions.apply` (ghi, `apply=true`, mã quyền `posting.integrity.edit` MỚI); bỏ qua kỳ khóa nhưng báo `locked_vouchers` |
| **Đọc tên ràng buộc từ IntegrityError** | `kernel/persistence/constraints.py` — helper phân tách chuỗi IntegrityError, dùng cho hồ sơ sao kê (`_flush_profile`) và đối chiếu (bản chép cũ ở `reconciliation.py` xoá) |
| **CRUD hồ sơ sao kê** (6G-2) | `/api/v1/bank/statements/profiles` (POST/PUT/DELETE/GET all), quyền **riêng** `bank.statement_profile.*` (view/edit, không create/delete quản trị riêng); ràng buộc DB: unique `(bank_id, name)`, FK RESTRICT từ `bank_statements`, bốn CHECK hình dạng; `_flush_profile` dịch IntegrityError → 409 đọc được |
| **Client: màn khai hồ sơ sao kê** | `statement-profile-page.tsx` — lưới + drawer 16 ô (bank_id/name/…), ô trống → `null` (khác chuỗi rỗng), drawer unmount có điều kiện + `key` theo bản ghi, 409 từ server hiện đúng |
| **Gộp hai lookup hook** (6F-2 nợ) | `danh-muc-thiet-lap/use-master-search-lookup.ts` (moved từ nhóm 3) — gộp `useMasterSearchLookup` + `useSlugLookup` (nhóm 9), khác trong: một tra MÃ chờ, một tra chuỗi không chờ; khóa bộ nhớ phụ theo **cả** slug lẫn dataset |
| **Đóng băng kernel** (6G-2 bước 23) | `kernel/protocols.py::__all__` tường minh (không có nó thì dependency không liên quan cũng thành cổng); `tests/frozen_kernel_api.txt` (178 dòng) + `tests/test_frozen_kernel_api.py` chụp chữ ký `kernel.protocols` + `posting.contracts`, CI đỏ nếu sửa; đường đổi hợp lệ = ADR bổ sung → `KET_UPDATE_FROZEN_API=1 pytest` → commit ảnh chụp cùng ADR; ADR-020 ghi quyết định + rủi ro; `DepositMovementSource` + `movement_source.py` **XÓA** |
| **In chứng từ tiền** (6E-2) | `kernel/formatting.py` (chuyển từ `reporting/rendering/formats.py` — module không import ngược `reporting` được), `kernel/money_words.py` (đọc số thành chữ cho dòng "(Viết bằng chữ)" của 01-TT/02-TT, phần lẻ không làm tròn), `kernel/config/printing/`: `context.py` (`DocumentPrintDetails` sáu vùng của biểu mẫu giấy), `voucher_fields.py` (`money_side_amounts` — số tiền in ra là số THẬT vào/ra TK tiền, không phải tổng mọi dòng), `subjects.py` (registry bản in KHÔNG phải chứng từ → mã quyền `view` của phân hệ); hook `PostingDocumentType.print_details` + `modules/{cash_book,bank}/print_details.py`; 8 mẫu builtin (GLE, PT/PC theo 01-TT/02-TT, UNC/BC/SEC/CTNB bản in nội bộ vì thông tư không có mẫu, KKQ theo 08a-TT); `POST /cash-book/count-sheets/{id}/print` (không ghi `print_log`); migration 0021 chỉ gieo dữ liệu | Thêm mẫu in cho một phân hệ mới |
| `api/routers/cashflow` | **Phase 6E-1** — BFF chỉ-đọc `/api/v1/cashflow/{overview,transactions}` cho màn hình "Tiền vào tiền ra": hàng thẻ quỹ + từng TK ngân hàng, lưới giao dịch đổi theo thẻ. Gộp hai module Ở TẦNG API (C3 vẫn cấm `cash_book` ↔ `bank` import nhau); **quyền theo TỪNG nửa** — thiếu quyền quỹ thì không thẻ quỹ, thiếu quyền ngân hàng thì `?source=bank` trả 403, thiếu cả hai mới 403 cả màn hình; `unassigned_deposit` phơi phần 112 chưa quy được TK ngân hàng | Màn hình dòng tiền U-Quỹ |
| `modules/bank/balance_service` | **Phase 6E-1** — số dư từng TK ngân hàng tới một ngày (kind-1 opening + phát sinh); `deposit_owner_account()` đưa luật quy chủ TK ngân hàng (CTNB: dòng Nợ thuộc TK đích) về MỘT chỗ Python, dùng chung với `movement_source` | Thẻ tài khoản, carry-forward |
| `modules/receivables/` | **Phase 7A** — chủ sở hữu `ar_ap_ledger` (sổ phụ công nợ theo từng chứng từ, PK **UUID** vì dòng là đích đối trừ). `ledger_service.py` cài `ArApSubledger` (ADR-021): `record` **thay trọn theo `voucher_id`** chứ không cộng dồn, `remove` từ chối khoản đã đối trừ, cả hai tự ghi vết vì `delete()` hàng loạt không qua listener `Audited`. `settlement_source.py` cài `SettlementTargetSource` cho hai loại hóa đơn + hai view **khóa cứng chiều** (`ReceivableProvider`/`PayableProvider`). Không có loại chứng từ riêng: dữ liệu do mua/bán sinh (7B/7C), do quỹ/ngân hàng tiêu thụ | Công nợ phải thu/phải trả |
| `modules/receivables/guards.py` (7B) | **`PartnerDebtGuard`** (FR-SYS-032) — `PostingGuard` thứ hai đăng ký vào `GUARD_REGISTRY` (bản cài cùng khuôn `cash_book.guards.CashBalanceGuard`), đặt ở `receivables` vì luật thuộc về chủ sổ phụ: soi MỌI chứng từ làm tăng nợ một đối tác (hóa đơn mua 7B, hóa đơn bán 7C, cả bút toán GLE thẳng vào 131/331) — `purchase`/`sales` không cần biết guard tồn tại (luật C3). Hai phép kiểm khi chứng từ tăng nợ: **ngưỡng nợ** (`partners.credit_limit` so với tổng còn nợ) và **nợ quá hạn** (hạn = `due_date` khoản, hoặc ngày chứng từ + `due_days` của điều khoản thanh toán). Số còn nợ đọc qua hàm `partner_open_debt()` (migration 0026, `SECURITY DEFINER`, ngoài RLS — cùng cách vá với guard khớp sao kê 0024) vì `credit_limit` là ngưỡng của đối tác trước TOÀN CÔNG TY còn `ar_ap_ledger` khoanh theo chi nhánh; ba mức (none/warn/block) theo setting `warning.partner_debt` | Cảnh báo/chặn vượt ngưỡng nợ, nợ quá hạn |
| `modules/purchase/` | **Phase 7B** — chủ sở hữu chứng từ mua hàng, document type `PUR` (migration 0026: bốn bảng `purchase_invoices`/`purchase_invoice_lines`/`landed_costs`/`purchase_settlements`, cùng khuôn `cash_vouchers`, không bảng nào mang `branch_id` — phạm vi là của header). `models.py`: năm nghiệp vụ mua gom trong một cột `kind` (hàng nhập kho/dịch vụ/tài sản/hàng đi đường/trả lại) vì chỉ khác TK Nợ và chiều bút toán, không khác hình dạng dữ liệu; `payable_account_id` lưu thật trên thân (không tra lại gói cấu hình lúc ghi sổ). `landed_cost.py` — phân bổ chi phí mua hàng THUẦN `Decimal` theo giá trị dòng / số lượng / thủ công, phần lẻ dồn về dòng trọng số lớn nhất. `posting_mapper.py` — mỗi dòng hàng và mỗi cặp (chi phí, dòng hàng) thành một cặp Nợ/Có riêng (không gộp một dòng Có tổng, để giữ cân khi tỷ giá lẻ). `settlement_service.py` — trả lại hàng mua (`kind=RETURN`) đối trừ hóa đơn gốc qua `posting/settlements` dùng chung với quỹ/ngân hàng, không sinh công nợ âm. Router `/api/v1/purchase/*` chỉ tạo/sửa/đọc thân hóa đơn + picker `open-invoices` (công nợ phải trả còn nợ cho chứng từ trả lại); ghi sổ/bỏ ghi sổ/xóa đi qua `/api/v1/vouchers` dùng chung (PUR đăng ký hook vòng đời vào registry posting, cố ý KHÔNG có `/actions/post/unpost` riêng). Cùng migration: quyền xem báo cáo tuổi nợ phải trả (`tuoi-no-phai-tra`) chuyển từ module `receivables` sang `purchase`; gói builtin bỏ theo dõi `item` trên TK `1331` (thuế GTGT đầu vào là sự thật theo hóa đơn, không theo vật tư) | Hóa đơn mua, chi phí mua hàng phân bổ, trả lại hàng mua |
| `kernel/pricing.py` (7C-1) | **Bộ định giá một dòng chứng từ** (FR-SAL §4.2, FR-SYS-042/043/045) — ba tầng nguồn giá xét theo thứ tự và dừng ở tầng đầu tiên trả lời được: bảng giá theo đối tác/hợp đồng → mức giá của mã hàng theo đúng đơn vị → đơn giá mặc định trên danh mục (mức 1, `unit_id IS NULL` — **không** có cột `sale_price` riêng). Không tầng nào khai giá thì trả `source = "none"` kèm đơn giá 0, **không** phải lỗi. Ở `kernel` chứ không `modules/sales` vì cả hai bảng giá mang cột `direction` mua/bán, mà C3 cấm `purchase` import `sales` — đặt ở `sales` thì chiều mua phải có một bản sao. Độ ưu tiên bảng giá đọc từ **độ cụ thể của dữ liệu** (hợp đồng → đối tác → nhóm sâu → nhóm nông → chung; nhóm đọc bằng `partners.path`), không từ cột ưu tiên gõ tay; chốt chặn tất định cuối là `effective_from` muộn hơn rồi `id` giảm dần. Tách thuế ngược làm tròn về `UNIT_PRICE_SCALE` (6 số lẻ), **không** về số chữ số của tiền — kết quả còn được nhân với số lượng. Thiết lập hệ thống đọc **một lần cho cả transaction** (`price_is_tax_inclusive_default` là tham số, không phải lượt đọc bên trong). Hai quy ước `unit_id` được bắc cầu ở đường đọc (`_normalized_unit`): bảng giá viết đơn vị chính bằng `NULL`, dòng chứng từ viết bằng **id thật** — không quy ước lại thì ca bán theo đơn vị chính trượt cả ba tầng giá lẫn bậc chiết khấu. Tầng 3 quy đổi giá theo `item_units.factor` (giá thùng = giá lon × 24); khai một dòng riêng cho thùng là lối thoát khi giá không tỉ lệ tuyến tính. **Nợ:** N+1, 6–10 truy vấn mỗi dòng — 7C-2 KHÔNG nối nó vào đường cất chứng từ (đơn giá do client chốt), và `/pricing/quote-batch` chỉ gộp request chứ không gộp truy vấn; trần 200 dòng chặn trên, làm phẳng truy vấn theo dòng vẫn mở | Đơn giá + chiết khấu cho dòng mua/bán |
| `kernel/master_data/` giá (7C-1) | **Ba bảng con giá** (migration 0027): `item_price_levels` (FR-SYS-042, mua **và** bán, `unit_id NULL` = theo đơn vị chính), `item_discount_tiers` (FR-SYS-045, ngưỡng theo **đơn vị chính**), `price_list_lines` (FR-SAL-020, ngưỡng theo **đơn vị của chính dòng** — bất đối xứng có chủ đích). Danh mục **thứ 22** `price_lists`: **luôn dùng chung toàn công ty** (`branch_id IS NULL` — danh mục không bật RLS nên lớp cô lập duy nhất là `_visible_to` ở đường đọc danh mục, mà bộ định giá đọc thẳng bảng này bằng đường khác; ràng buộc làm trạng thái sai không biểu diễn được thay vì phải nhớ lọc ở mọi đường đọc mới). Phạm vi áp dụng bằng ba cột nullable (đối tác **hoặc nút nhóm đối tác**, hợp đồng, cả hai NULL = chung) + cửa sổ hiệu lực; nút nhóm không mang chiều giá (`direction_set_unless_group` + `group_has_no_pricing_fields`, khuôn `nature_set_unless_group` **kèm cả chiều cấm**). Hai bảng có `unit_id` dùng **chỉ số duy nhất riêng phần tách theo `unit_id IS NULL`** thay cho `UNIQUE` — `UNIQUE` coi mọi NULL là khác nhau nên nó cho khai vô số dòng "giá theo đơn vị chính" cùng ô; cột nullable còn gỡ ca **dịch vụ không có đơn vị chính** (H69 chốt `base_unit_id` một lần) vĩnh viễn không khai được giá. Cột `items.price_is_tax_inclusive` **ba trạng thái** (NULL = theo khóa `sales.price_is_tax_inclusive`) — hai trạng thái thì thiết lập cấp hệ thống chỉ còn tác dụng với dữ liệu chưa tồn tại. Hình dạng cột đơn giá dời lên `kernel/money.UNIT_PRICE_*`, `modules/purchase` dùng lại | Bảng giá, mức giá, bậc chiết khấu |
| `modules/sales/` | **Phase 7C-2** — chủ sở hữu chứng từ bán hàng, document type `SAL` (migration 0028: ba bảng `sales_invoices`/`sales_invoice_lines`/`sales_settlements`, cùng khuôn `purchase`, không bảng nào mang `branch_id`). `models.py`: năm nghiệp vụ bán gom trong một cột `kind` (hàng hóa/dịch vụ/trả lại/giảm giá/đại lý); `receivable_account_id` lưu thật trên thân; **chiết khấu thương mại trừ thẳng trên dòng** — `amount_fc` là doanh thu SAU chiết khấu và `discount_amount_fc` giữ phần đã giảm cho bảng kê, nên không có bút toán 521 riêng (SRS 06 §3.2 cho cả hai đường; đường trừ trực tiếp là đường hóa đơn GTGT in ra). Ba cột giá vốn + cờ `cogs_posted` chỉ được **lưu**: bút toán Nợ 632/Có 156 là việc của phase 8. `posting_mapper.py` — Nợ TK phải thu / Có doanh thu + thuế đầu ra theo cặp; trả lại (kind 2) và giảm giá (kind 3) đảo chiều cả cặp và thêm cặp chênh lệch tỷ giá với **`money_in=True`** (đối xứng có chủ đích với `False` của trả lại hàng mua: hướng lãi/lỗ theo CHIỀU TIỀN, giảm phải thu cùng hàng phiếu thu). `service._subledger_entries` trả **một** dòng sổ phụ, khác chiều mua gom theo cặp (NCC, TK) — hóa đơn bán chỉ có một người mua và một TK phải thu. Hạn thanh toán rơi về điều khoản của **danh mục khách hàng** (FR-SAL-009) ở server, không chỉ điền sẵn ở form. Đơn giá và chiết khấu **do client chốt**, server không tra lại (quyết định user 2026-09-04): `quote_price` trả `source=none` kèm đơn giá 0 cho mã hàng chưa khai giá, nên ghi đè ở server sẽ xóa im lặng đúng những dòng phải gõ tay; ba cột `price_list_id`/`price_source`/`discount_percent` lưu lại để trả lời "số này ở đâu ra". Router `/api/v1/sales/*` chỉ tạo/sửa/đọc + picker `open-invoices`; ghi sổ/bỏ ghi sổ/xóa đi qua `/api/v1/vouchers` dùng chung. Cùng migration: quyền xem `tuoi-no-phai-thu` chuyển từ `receivables` sang `sales` (thay đổi phá vỡ với vai trò cũ — xem `deployment-guide` §2.2). Nộp nhánh của mình vào **hai check toàn vẹn** cộng theo `UNION`: `settlement_matches_subledger` (`sales_settlements`) và `usage_counter_accurate` (khách hàng + nhân viên bán hàng — tham chiếu ĐẦU TIÊN tới `employees` từ một chứng từ); thiếu nhánh là dòng đỏ **trên dữ liệu đúng**, và không cổng nào khác thấy | Hóa đơn bán, trả lại/giảm giá hàng bán |
| `kernel/master_data/unit_priced_rows.py` (7C-1) | **Luật dùng chung của hai bảng giá theo đơn vị** — `ensure_unit_is_priceable` (đơn vị phải là dòng `item_units` đã khai; đơn vị chính gửi tường minh thì **từ chối**, không âm thầm quy về NULL) và `plan_unit_merge_cleanup`, một hàm **thuần** trả về quyết định `(xóa, chuyển về đơn vị chính)`. Thuần vì phần khác nhau giữa hai bảng là câu truy vấn, phần giống nhau là luật — nhờ đó luật test được bằng bảng giá trị biên không cần DB, và mypy đọc được kiểu từng bảng thay vì một `Protocol` phải đúng ở cả cấp lớp lẫn cấp thể hiện. Ca đắt nhất nó phủ: dòng giá **trở thành "trỏ đúng đơn vị chính"** sau khi `merge_service` dời `items.base_unit_id` — chuyển thành NULL nếu ô còn trống, chỉ **xóa** khi ô đã có chủ. Sáu hook hợp nhất mới: `items` +3, `units_of_measure` +2, `price_lists` +1 | Gộp danh mục không làm mất giá |
| `modules/*` | `cash_book/` (6B–6C, +`balance_service` 6E-1); `bank/`, `warehousing/treasurer/` (6C–6E-1); `general_ledger/journal/` (phase 4, +`settlement_service.py` 7C-3 — phân loại dòng chạm công nợ, đối trừ theo dòng, sinh `SubledgerEntry`); `receivables/` (7A, +`guards.py` 7B, +hai loại đích ghi tay 7C-3); `purchase/` (7B, server; **chưa có UI client**); `sales/` (7C-2, server; **chưa có UI client**) — bộ định giá của nó vẫn sống ở `kernel/pricing` (7C-1) vì chiều mua cũng cần; còn lại chỉ có `contracts.py` rỗng — chỗ giữ sẵn cho phase 8–9 | — |

### Ngoài server

| Đường dẫn | Trạng thái |
| --- | --- |
| `server/migrations/` | `env.py` (chạy per-schema) + `versions/0001_core_platform.py` |
| `server/scripts/export_openapi.py` | **Xuất OpenAPI từ `create_app()` ra JSON**, không cần DB. Chạy: `uv run python scripts/export_openapi.py <đường-dẫn.json>` hay `make api-types` |
| `client/packages/api-types/` | **Sinh từ OpenAPI**: `schema.d.ts` (type TypeScript), `openapi.json` (spec). Cả hai **được COMMIT** là bản ghi hợp đồng. Tạo bằng `openapi-typescript` |
| `client/src/` | **Lát 2C-1..2C-3:** `main.tsx`; `app/{providers,router,session-gate,app-layout,navigation,placeholder-page,error-boundary}`; `design-system/{base,tokens}.css` + 13 component (`button`, `text-field`, `select-field`, `alert`, `tabs`, `seg`, `status-pill`, `next-action-cell`, `data-table`, `drawer`, `split-pane`, `checklist-panel`, và **`data-grid/`** = lưới nhập liệu, spike S3); `lib/{api-client,session,session-storage,app-version,i18n,formatters,access,theme,safe-storage}`; `features/auth/*` (đăng nhập, đổi mật khẩu tạm, đăng ký 2FA, cần cập nhật, mất kết nối) + `features/dataset`; `locales/{vi,en}.ts`. **Chỉ có ở bản dev** (gác bằng `__DEV_TOOLS__`): `features/kitchen-sink/` và `features/bench/`. **Lát 3D** thêm 2 component (`tree-picker` — cây nạp lười WAI-ARIA, `lookup-input` — combobox lọc trong bộ nhớ), `api-client` thêm `postForm`/`getBlob`, và nhóm màn hình thật đầu tiên `features/danh-muc-thiet-lap/` (màn danh mục cây+lưới cho cả 20 danh mục theo registry client `catalog-registry.ts` — **được canh khớp `openapi.json` bằng test**, drawer sửa, màn đối tác + thẻ công nợ giữ chỗ H56, wizard nhập Excel 3 bước trên job nền, màn Thiết lập hai nhóm U14). Quy ước rút ra từ review 3D: thân lệnh ghi **bỏ khóa** cho ô trống (server áp default — không gửi `null` vào cột non-nullable), bool luôn gửi tường minh, `row_version` chưa-có-dòng là `null` trên dây |
| `client/src/features/tien-vao-tien-ra/` | **Lát 6F-1/6F-2** — màn "Tiền vào tiền ra" trọn bộ: `cashflow-page` (hàng thẻ quỹ + từng TK ngân hàng qua BFF 6E-1, lưới giao dịch đổi theo thẻ, ghi chú 112 chưa gắn), `cash-voucher-form` (PT/PC) + `bank-voucher-form` (BC/UNC/SEC/CTNB một thân) với nghiệp vụ tự điền cặp Nợ/Có (`use-auto-posting`, lưới cặp dùng chung cấu hình chiều GLE — `pair-line-*`), đối trừ công nợ (`settlement-section`, `use-open-invoices`), băng "Vẫn ghi sổ?" `acknowledge_warnings`, nút In chọn mẫu; `count-sheet-page` (kiểm kê quỹ); `reconciliation-page`/`-detail-page` (U5 — sao kê + nhập theo hồ sơ, hai khung SplitPane, ghép/gỡ tay + auto-match); `treasurer-queue-page` + `treasurer-cash-book-page` (U6 — ghi sổ một lô, sổ quỹ phân trang). Quy ước 6F: hydrate form SỬA từ THỨ ĐÃ LƯU (không suy từ danh sách async), ô con reset khi lựa chọn cha đổi, drawer có file-upload phải unmount có điều kiện |
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

## 5. Bộ test (**server: 768 không-DB + 1149 DB (1.917 total); client: 267**) 

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
| `test_unit_merge_cleanup.py` (non-db) | **Lát 7C-1**: bảng giá trị biên của `plan_unit_merge_cleanup` — ai thắng khi hai đơn vị cùng khai một ô, thứ tự dòng **không** đổi kết quả, và ca dòng giá trở thành "trỏ đúng đơn vị chính" sau lần gộp (chuyển về NULL khi ô còn trống, chỉ xóa khi ô đã có chủ). Chạy được không cần DB vì luật là hàm thuần |
| `test_pricing_engine.py` (db) | **Lát 7C-1**: ba tầng nguồn giá theo đúng thứ tự và điều kiện rơi tầng; bảng giá theo đối tác/nhóm/hợp đồng, nhóm sâu thắng nhóm nông, bảng giá gắn hợp đồng **và** đối tác khác không khớp; cửa sổ hiệu lực tính cả ngày cuối; "ngừng theo dõi" có tác dụng; chọn tay bảng giá là **ép** chứ không ưu tiên; bậc chiết khấu chọn theo số lượng quy về đơn vị chính; chiết khấu không áp cho chiều mua; tách thuế ngược + cờ ba trạng thái FR-SYS-043 |
| `test_pricing_merge_hooks.py` (db) | **Lát 7C-1**: sáu hook đi qua `merge_records` thật (không gọi thẳng hook — thứ đáng nghi là **thứ tự**: hook phải chạy trước `UPDATE` vô danh, hook từ chối trước hook dọn). Ba chiều gộp: mã hàng, đơn vị tính, bảng giá. Phạm vi chi nhánh đọc **lúc chạy** — truyền tập rỗng là bài xanh khi chạy một mình và đỏ khi tệp khác tạo chi nhánh trước |
| `test_item_pricing_api.py` (db) | **Lát 7C-1**: ba bảng con giá qua HTTP — `unit_id` bỏ trống = đơn vị chính, gửi id đơn vị chính tường minh thì **409**; hai chỉ số duy nhất riêng phần chặn hai dòng "giá theo đơn vị chính" cùng ô; bậc chiết khấu đòi đơn vị chính; nút nhóm bảng giá không cần chiều giá và **không được** mang nó; đường `POST /pricing/quote` trả kèm **nguồn** đã trả lời; **lát 7C-2**: `POST /pricing/quote-batch` trả đúng thứ tự và đúng số phần tử (client ghép theo vị trí), dòng chưa khai giá vẫn có phần tử `source=none` thay vì hỏng cả lô, lô rỗng và lô vượt trần đều 422 |
| `test_sales_invoice_flow.py` (db) | **Lát 7C-2**: vòng đời hóa đơn bán — cấp số `SAL{YY}-`, nghiệp vụ phải thuộc gói và phải là nghiệp vụ của khách hàng, TK công nợ phải theo dõi `customer`; chiết khấu trừ thẳng trên dòng (bút toán chỉ chạm ĐÚNG ba TK, không có 521 và không có cặp giá vốn); ghi sổ sinh **một** dòng sổ phụ rồi bỏ ghi sổ gỡ sạch; ngoại tệ tỷ giá lẻ khớp sổ cái từng đồng; hạn thanh toán rơi về điều khoản của **danh mục khách hàng**; trả lại và giảm giá (hai `kind`, cùng một đường) đối trừ hóa đơn gốc và đảo chiều bút toán; **hóa đơn gốc đã thu đủ thì chứng từ giảm trừ bị từ chối**; đối trừ vào khoản nằm trên TK khác bị từ chối; bộ đếm tham chiếu khách hàng + nhân viên bán hàng nhích/lùi |
| `test_sales_schemas.py` | **Lát 7C-2**: luật biên của payload, không cần DB — dòng có thuế phải có TK thuế, dòng xuất kho phải có vật tư, `discount_percent` trong [0,100], hai `kind` giảm trừ **bắt buộc** có dòng đối trừ còn ba `kind` thường thì cấm, một đích chỉ một dòng đối trừ, `extra="forbid"` |
| `test_master_data_registry.py` (non-db) | **Lát 3B-1**: mọi lớp con `MasterDataRow` phải có trong registry; `extra_fields` khớp cột hai chiều; response model không thiếu trường; đủ 4 mã quyền, không thừa `post`/`print`; đủ 6 route cho **mỗi** danh mục; slug phải là identifier; đăng ký trùng bị từ chối |
| `test_master_data_api.py` (db) | **Lát 3B-1**: CRUD danh mục cây thuần + danh mục có cột riêng; validator liên-trường; phạm vi chi nhánh (404 không phải 403); quyền từng danh mục; chuyển nhánh; idempotency; khóa lạc quan |
| `test_analysis_dimensions.py` (db) | **Lát 3B-1**: gieo mầm + chạy lại; mã quyền tới bảng permissions; cây giá trị; duy nhất trong chiều; `subtree_of` không rò sang chiều khác; cha khác chiều bị từ chối; nguồn `master` trỏ slug có thật |
| `test_item_catalog_api.py` (db) | **Lát 3B-3**: 47 test cho vật tư — CRUD danh mục cây, bảng con đơn vị quy đổi + mã quy cách (8 endpoint), phạm vi chi nhánh, quyền, chuyển nhánh, khóa lạc quan, idempotency; gộp bản ghi từ chối khi đơn vị chính khác (H71) hoặc bản ghi là nhóm (H74); luật liên-trường qua `update_guard` (H75); nhóm bị cấm `nature`/`base_unit_id` (H76) |
| `test_statement_formula.py` (non-db) | **Lát 5B**: grammar parser, account range prefix-match, evaluator tô-pô, chu trình/rowref không hợp lệ, payload kiểu `eval`/SSTI bị từ chối |
| `test_statement_layout_loader.py` (non-db) | **Lát 5B**: fail-closed loader — sai công thức, rowref, chu trình, TK không khớp accounts.csv; golden test B01/B02 khớp mẫu đúng thứ tự; chỉ tiêu ngoại lệ không cộng dương; layout income cấm hàm số dư |
| `test_statement_builder_api.py` (db) | **Lát 5B**: dataset riêng `bctc5b` — statement builder lấy `opening_balances`+`gl_postings`, cột so sánh (N/A khi chưa lập), test BR-GLE-04/BR-RPT-01/BR-RPT-04; API `/api/v1/statements` + `/api/v1/statements/{layout_code}/preview` + quyền + 403/404 |

Tổng **1.917 test** (1.149 cần PostgreSQL).

**Băm mật khẩu chạy tham số RẺ trong test.** `conftest.cheap_password_hashing`
(autouse, phạm vi HÀM) hạ Argon2id xuống mức tối thiểu: hồ sơ cProfile cho thấy
băm chiếm **16% CPU** của bộ test, và bỏ nó rút bộ DB từ **985s xuống 868s
(−12%)**. Bài nào cần tham số THẬT phải mang dấu `real_password_hashing` —
hiện có hai: `test_password_policy.py` (khẳng định trên chính cấu hình
production) và `test_auth_concurrency.py` (cửa sổ đua rộng đúng bằng thời gian
băm, hạ băm là làm bài mất khả năng bắt lỗi).

**Bốn tệp có fixture actor phạm vi MODULE** (`test_master_data_merge.py`,
`test_bank_statements_api.py`, `test_item_catalog_api.py`,
`test_import_api.py`): người dùng "phạm vi toàn công ty" được gán MỌI chi
nhánh, mỗi chi nhánh một lời gọi `assign_branch` (~56ms), mà chi nhánh tích lũy
suốt lượt chạy (đo được tới **156**) — nên dựng lại mỗi bài tốn ~5s ở cuối bộ.
Dựng một lần cho cả tệp: bộ DB **985s → 456s (7:36)** qua ba lát.

Đổi lại bốn tệp ấy mang hai bất biến mà 30 tệp API khác không có — **đừng chép
`scope="module"` sang tệp mới mà không chép cả hai**, docstring của từng fixture
ghi rõ:

1. không bài nào trong tệp được tạo chi nhánh;
2. cửa sổ hạn mức dùng chung cả tệp nên phải tắt hạn mức.

Fixture autouse `_shared_actor_still_spans_every_branch` canh vế thứ nhất —
nhưng chỉ canh **số chi nhánh**, không canh phạm vi của chính người dùng dùng
chung: gỡ chi nhánh khỏi người ấy (`DELETE /system/users/{id}/branches/{code}`)
sẽ lọt lưới, và các bài sau đỏ bằng `scope_incomplete` trỏ nhầm vào mã
production. Chưa bài nào làm thế; nếu thêm, hãy mở rộng canh gác sang
`UserBranch` thay vì tin nó là đủ.

Fixture băm rẻ phải ở phạm vi **hàm**, không phải phiên: bản đầu dùng phạm vi phiên và
để tệp cần tham số thật "tắt" bằng cách khai trùng tên — cách ấy không gỡ được
bản vá đã áp từ module chạy trước, nên tệp ấy xanh RỖNG trong mọi lượt chạy
đầy đủ. `test_password_policy.py::test_the_real_argon2_parameters_are_live` là
cổng canh chính chuyện đó. Máy không có DB thì bỏ qua; CI đặt `KET_TEST_REQUIRE_DB=1` để **đỏ** thay vì bỏ qua.

**Job CI chạy bộ test trên BA SHARD song song** (`.github/workflows/ci.yml`,
job `server-db`), mỗi shard một container `postgres:16` riêng. Chọn shard thay
vì `pytest-xdist` vì `conftest` xóa/dựng lại vai trò ở phạm vi **cụm** và dùng
dataset phạm vi phiên dùng chung — hai worker trong một tiến trình sẽ giẫm lên
nhau, ba tiến trình với ba cụm thì không. Hai shard nhận danh sách tệp tường
minh (17 tệp nặng nhất), shard thứ ba chạy phần còn lại bằng `--ignore` dựng
**từ chính hai danh sách kia**, nên tệp test mới luôn có chỗ chạy.

Chia shard có đúng một kiểu hỏng nghiêm trọng và im lặng: một tệp không thuộc
shard nào thì bài của nó không chạy ở đâu cả và CI vẫn xanh.
`.github/scripts/check_test_shards.py` canh đúng chuyện đó — cộng số bài trong
junit XML của ba shard rồi so với số `pytest --collect-only` thu thập trên toàn
bộ. Không con số nào viết cứng: thêm bài mới thì cả hai vế cùng tăng.

Con số độ phủ đến từ job `server-db-coverage` sau `coverage combine`, không từ
shard nào — báo cáo của một shard là báo cáo trên một phần ba bộ test.

**Chờ khóa trong test hỏng sau 60 giây, không treo.** `tests/lock_diagnostics.py`
đặt `lock_timeout` ở phạm vi **database** (`ALTER DATABASE ket_test SET …`), nên
mọi connection thừa hưởng — kể cả connection do mã production tự dựng, và đó là
chủ ý: một lượt CI từng chết ở trần 30 phút vì `bind_seed_schema` chờ
`pg_advisory_xact_lock` vô hạn, và đường treo đi qua engine của
`provision_dataset`. Khi ngưỡng nổ, hook `pytest_exception_interact` đính vào
report bản đổ `pg_stat_activity` + `pg_locks` nêu đích danh phiên đang giữ khóa.
Đừng chuyển ngưỡng này về `connect_args` của từng engine trong conftest —
`test_lock_timeout_diagnostics.py` đỏ ngay, vì cách đó bỏ sót đúng những đường
ít ai nhớ.

---

## 6. Việc tiếp theo

**Phase 6 XONG** (2026-08-31) — toàn bộ dòng tiền Quỹ & Ngân hàng đi trọn: chứng từ lập → cất → ghi sổ → lên báo cáo + bảng cân đối; hạ tầng trải rộng cho phase 7–9 (branch_scope, dimension_recompute, REFERENCE_GUARDS, frozen kernel API).

**Phase 7 đang chạy**, chia 8 lát 7A→7H. **7A xong (2026-09-01)**: nền công nợ — `ar_ap_ledger`, module `receivables`, báo cáo tuổi nợ AR/AP, và bằng chứng cho tiêu chí liên-phase "màn thu tiền của phase 6 thấy hóa đơn phase 7 mà **`cash_book` không đổi một dòng nào**".

**7B xong (2026-09-03)**: module `purchase` server — hóa đơn mua (`kind` gom năm nghiệp vụ: hàng nhập kho/dịch vụ/tài sản/hàng đi đường/trả lại), chi phí mua hàng phân bổ theo giá trị/số lượng/thủ công (`landed_cost.py`), trả lại hàng mua đối trừ hóa đơn gốc qua `posting/settlements` dùng chung với quỹ/ngân hàng. Guard ngưỡng nợ + nợ quá hạn `PartnerDebtGuard` (FR-SYS-032, setting `warning.partner_debt`) đặt ở `receivables` vì thuộc chủ sổ phụ — soi MỌI chứng từ tăng nợ đối tác, không riêng `purchase`. Quyền xem báo cáo tuổi nợ phải trả chuyển sang module `purchase`. Router `/api/v1/purchase/*` chỉ tạo/sửa/đọc + picker công nợ; ghi/bỏ ghi sổ/xóa đi qua `/api/v1/vouchers` dùng chung. **Chưa có UI client** cho phân hệ này.

**7C-1 xong (2026-09-04)**: danh mục bảng giá (thứ 22) + hai bảng con giá của vật tư + bậc chiết khấu + bộ định giá `kernel/pricing` — đặt ở kernel vì cả hai bảng giá mang chiều mua/bán và C3 cấm `purchase` import `sales`.

**7C-2 xong (2026-09-05)**: module `sales` server — hóa đơn bán (`kind` gom năm nghiệp vụ: hàng hóa/dịch vụ/trả lại/giảm giá/đại lý), chiết khấu thương mại trừ thẳng trên dòng, trả lại và giảm giá đối trừ hóa đơn gốc qua `posting/settlements` dùng chung. Hai quyết định user: đơn giá và chiết khấu **do client chốt** (server ghi đè sẽ xóa im lặng những dòng gõ tay, vì bộ định giá trả 0 cho mã hàng chưa khai giá) — nợ N+1 của 7C-1 nói tới đường CẤT chứng từ, mà D1 làm đường ấy không còn gọi bộ định giá, nên phần nợ đó hết vì không còn chỗ xảy ra; đường hỏi giá của form gộp bằng `POST /pricing/quote-batch` — N request thành một, nhưng truy vấn **mỗi dòng** không đổi (trần 200 dòng chặn trên); và chứng từ giảm trừ cho hóa đơn **đã thu đủ** bị **từ chối** thay vì sinh một khoản phải trả khách hàng đứng riêng, giữ `SettlementTargetKind` nguyên vẹn. Quyền xem báo cáo tuổi nợ phải thu chuyển sang module `sales`. **Chưa có UI client**.

**7C-3 xong (2026-09-05)**: chứng từ nghiệp vụ khác trở thành **nguồn ghi `ar_ap_ledger` thứ ba** (quyết định user). Dòng trên TK công nợ ở bên THUẬN tính chất sinh một khoản nợ mới (`SettlementTargetKind.JOURNAL_RECEIVABLE`/`JOURNAL_PAYABLE` — hai giá trị chứ không một-cộng-cột-chiều, để hai view provider khóa được chiều); dòng ở bên NGƯỢC là một lượt **đối trừ** qua bảng mới `gl_journal_settlements` (migration 0029). Bù trừ 131 ↔ 331 cùng đối tác vì thế là hai lượt đối trừ, không phải hai khoản nợ mới. Đối trừ gắn vào **dòng định khoản** chứ không vào chứng từ — khác ba bảng đối trừ trước — vì một chứng từ GLE chạm nhiều đối tác và nhiều TK công nợ cùng lúc; kéo theo BR-QUY-03 ("tổng đối trừ = tổng tiền chứng từ") áp cho từng dòng. Hạn thanh toán rơi về điều khoản của danh mục đối tác. Check `arap_matches_control` **vẫn ngoài `CHECKS`**: điều kiện #1/#3/#4 đóng, nhưng lát này tìm thêm hai điều kiện — khoản **ứng trước** không có dòng sổ phụ (rộng hơn mô tả cũ: phiếu thu/chi cũng để `settlements` tùy chọn) và đẳng thức per-ledger không bao giờ đúng ở **sổ quản trị** (sổ cái nhân đôi hai sổ, sổ phụ chỉ ghi sổ tài chính — đúng với mọi nguồn công nợ). Cả hai ghi ở đầu tệp `.sql`, hẹn 7C-4. Review pre-landing bắt hai lỗi thật đã sửa: `journal/posting_mapper` là bản cài thứ **năm** có đối trừ và là bản duy nhất không sinh cặp bù chênh lệch tỷ giá 515/635 (chứng từ ngoại tệ lệch đúng `Σ fx_diff`, và lối "người dùng tự gõ cặp bù" không tồn tại — dòng gõ tay trên 131 bị `classify` đọc thành khoản phải thu ma), nay sinh tự động và **chẻ theo chiều lấy từ BÊN của dòng định khoản**, không từ `target_kind` của đích; và hai dataset báo cáo `ar_ap_aging`/`cash_forecast` còn `target_kind IN (0,1)` nên khoản nợ ghi tay biến mất khỏi tuổi nợ + dự báo dòng tiền — nới cả `WHERE` lẫn `CASE` ánh xạ chiều, vì nới mỗi `WHERE` sẽ đẩy phải thu ghi tay sang phía phải trả. Kèm hai sửa nhỏ: kiểm vượt số còn nợ nay gộp theo đích qua nhiều dòng, và `posting/settlements` đối chiếu loại đích client gửi với loại thật của dòng (lợi cho cả bốn phân hệ đối trừ).

Tiếp theo: **7C-4** — khoản ứng trước thành dòng sổ phụ chiều ngược + bù hóa đơn với khoản ứng trước, rồi **đăng ký check toàn vẹn 131/331**.

**Còn mở:**

1. Độ phủ có đặt ngưỡng chặn không — CI mới chỉ **báo cáo**.
2. Đường build Windows và bộ cài chưa ký chứng thư OS — xác minh ở lần chạy
   `release.yml` đầu tiên.
3. Hạn mức request đếm **trong tiến trình**: chạy hai tiến trình API thì hạn mức
   thực tế nhân đôi. Chấp nhận ở quy mô LAN; chỗ sửa khi cần chính xác là chuyển
   bộ đếm xuống PostgreSQL, không phải thêm Redis vào bản cài.
4. Dọn phiên `prune_sessions` và `prune_idempotency_keys` có sẵn làm job (xếp hàng qua API); lịch tự động thuộc phase 3 hoặc sau (tuỳ chức năng Scheduler).
5. Bộ vai trò mẫu (`ke-toan`, `thu-quy`, `xem`) vẫn chờ gói cấu hình TT99/TT133
   ở phase 5 — hiện chỉ gieo `admin`.
6. Setting `warning.cash_balance` (none/warn/block, mặc định none) được khai sẵn lát 6B; UI thực thi cảnh báo hoãn phase 6–7.
7. **Bút toán GLE gõ thẳng vào TK công nợ 131/331 kèm chiều đối tác không sinh dòng sổ phụ** — thao tác hợp lệ hôm nay, và là lý do check toàn vẹn "sổ phụ khớp TK công nợ" chưa đăng ký được (bản thảo `posting/integrity/checks/arap_matches_control.sql` cố ý ngoài `CHECKS`). Câu hỏi sản phẩm cho 7C/10a: cấm, hay tự sinh khoản phải thu/phải trả? Giữ nguyên thì các khoản ấy vô hình với màn thu nợ.
8. **Cụm dev có thể là PostgreSQL 18** (Ubuntu 26.04 không còn `postgresql-16`) trong khi CI chạy `postgres:16` — xanh ở máy không còn bảo đảm xanh ở CI. Đường dựng + đánh đổi ghi ở `deployment-guide.md` §1.
9. **Module `purchase` (7B) và `sales` (7C-2) chưa có UI client** — router `/api/v1/purchase/*` + `PartnerDebtGuard` chạy được qua API/test, màn hình nhập hóa đơn mua/bán + chi phí mua hàng (và "Vẫn ghi sổ?" cho cảnh báo `warning.partner_debt`) thuộc lát 7H.
