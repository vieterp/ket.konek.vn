# Chuẩn code — Konek Két

Chốt ở phase 1. Nguyên tắc chọn quy tắc: **chỉ giữ quy tắc kiểm được tự động,
hoặc quy tắc mà vi phạm gây sai số tiền.** Quy tắc "phong cách" không kiểm được
thì không đưa vào đây — không ai theo.

Neo: ADR-015 (kỷ luật kiểu), ADR-004 (luật phụ thuộc), ADR-014 (set-based SQL),
LD-13, LD-14.

---

## 1. Tiền và số học — quy tắc quan trọng nhất

| Chủ đề | Quy tắc | Ép bằng |
| --- | --- | --- |
| Kiểu tiền | Chỉ `decimal.Decimal`. **Cấm `float`** trên **toàn bộ `server/src/ket`** (bộ quét không chừa package nào) | `server/tests/test_no_float_in_domain.py` — quét AST, chạy trong CI |
| Ngoại lệ cấm float | Chỉ mã vẽ biểu đồ (phase 10b). Thêm đường dẫn vào `ALLOWED_PREFIXES` **kèm lý do**, không nới bộ quét | Review |
| Làm tròn | Một module duy nhất `ket.kernel.money` (**đã có** từ phase 2A). `ROUND_HALF_UP`; làm tròn **đúng một lần ở cuối** phép tính; `scale` là tham số, mặc định 2 — từ phase 3 đọc theo dataset (FR-SYS-064) | `tests/test_money_rounding.py` — bảng giá trị biên |
| Cột tiền trong DB | `NUMERIC(18,4)` cho đơn giá / tỷ giá · `NUMERIC(18,2)` cho thành tiền | Review migration |
| Đa tiền tệ | Mỗi dòng phát sinh lưu đủ bộ `(currency, rate, amount_fc, amount_debit, amount_credit)`; sổ cái lưu VND quy đổi | Schema phase 4 |

> Vì sao gắt: một phép cộng `float` lọt vào đường ghi sổ đủ làm lệch cân đối và
> rất khó truy ngược. Bộ quét đọc AST nên `float` trong docstring/comment không
> bị báo nhầm; nó bắt annotation, ép kiểu, và literal dấu phẩy động.

---

## 2. Kiểu tĩnh (Python)

| Chủ đề | Quy tắc |
| --- | --- |
| mypy | `strict = true` trên toàn `server/src/ket`, **fail = chặn merge**. Không có ngoại lệ per-module |
| Cờ bật thêm | `disallow_untyped_defs`, `disallow_any_generics`, `warn_return_any`, `strict_equality`, `no_implicit_optional`, `warn_unused_ignores`, `warn_unreachable` |
| `# type: ignore` | Cấm dạng trần — `enable_error_code = ["ignore-without-code"]` bắt phải nêu mã lỗi. Mỗi ignore phải kèm lý do trong comment và được review. **Đếm số ignore trong báo cáo mỗi phase** |
| Ranh giới API | Pydantic v2 model cho mọi request/response |
| DTO nội bộ | Pydantic model hoặc dataclass có kiểu |
| Cấm | `dict[str, Any]` đi qua ranh giới module |

Hiện trạng (sau phase 2B-0): `mypy --strict` xanh trên **72 file**, **0** `# type: ignore` trong
`server/src`. Ngưỡng 0 là cổng CI thật — nới phải sửa `.github/workflows/ci.yml` trong PR.

---

## 3. Luật phụ thuộc

Bảy luật ở `docs/system-architecture.md §Luật phụ thuộc`. Năm luật ép được bằng
`import-linter` (`server/importlinter.ini`):

| Contract | Nội dung |
| --- | --- |
| C1 | `kernel` không import `posting` / `reporting` / `modules` / `worker` |
| C2 | Phân tầng `worker` → `modules` → `posting` → `kernel` |
| C3 | Không module nghiệp vụ nào import module nghiệp vụ khác (14 module) |
| C4 | `posting` chỉ dựa vào `kernel`, không biết module nào tồn tại |
| C5 | `reporting` chỉ đọc: không import `posting` hay `modules` |

**Luật nào ép bằng gì — nói thẳng, vì phase sau sẽ tin bảng này:**

| Luật | Ép bằng | Tự động? |
| --- | --- | --- |
| 1. `modules.*` chỉ dùng `kernel` + `posting` | C2 + C3 | ✅ |
| 2. Qua Protocol trong `kernel` / domain event | — hệ quả của C3, không có kiểm riêng | ❌ review |
| 3. Chỉ `posting` INSERT `gl_postings` | — | ❌ **review migration + code** |
| 4. `reporting` chỉ đọc | C5 chỉ chặn **import**; **không** chặn `reporting` tự ghi DB | ⚠️ một phần |
| 5. `kernel` độc lập | C1 | ✅ |
| 6. UI không gọi API Tauri trong nghiệp vụ | eslint `no-restricted-imports` (**không phải** import-linter) | ✅ |
| 7. Không `dict[str, Any]` qua ranh giới module | — mypy strict **không** phát hiện được | ❌ review |

Ba luật (2, 3, 7) và một nửa luật 4 chỉ sống nhờ review. Đừng nói với nhau là
"CI lo rồi".

Cần dữ liệu của module khác → khai **Protocol trong `kernel`** hoặc dùng domain
event. **Không** truy vấn trực tiếp bảng của module khác. Mọi Protocol chạm
phase 7/8 phải khai từ phase 6 (RT-18 — kernel đóng băng cuối phase 6).

---

## 4. Python — quy ước chung

| Chủ đề | Quy tắc |
| --- | --- |
| Layout | `src/` layout, package `ket` |
| Đặt tên | `snake_case` cho module/hàm/biến, `PascalCase` cho class (ruff `N`) |
| Lint | ruff: `E,W,F,I,N,UP,B,ANN,S,C4,DTZ,T20,RUF` |
| Tắt có chủ đích | `E501` (formatter lo), `RUF001/002/003` (docstring tiếng Việt chứa ký tự "mơ hồ" — en dash, dấu thanh) |
| SQL | Truy vấn nghiệp vụ dùng SQLAlchemy Core/ORM có kiểu; báo cáo dùng SQL text **đã tham số hóa**. **Cấm f-string / chuỗi nối trong `text()` / `exec_driver_sql()`**. Luật cứng: quét AST, 5 tệp DDL-identity miễn trừ kèm lý do. Test: `tests/test_no_sql_string_interpolation.py` |
| Tính khối lượng lớn | Set-based SQL (ADR-014). Vòng lặp Python trên > 1.000 dòng dữ liệu phải có lý do ghi trong comment và được review chấp thuận |
| Ngày | `date` cho ngày hạch toán / ngày chứng từ; `timestamptz` cho dấu vết audit (ruff `DTZ` cấm datetime naive) |
| `print()` | Cấm trong code nghiệp vụ (ruff `T20`) — dùng logger |
| Không bê pattern Odoo | SQLAlchemy tường minh; không metaclass magic; kế thừa không quá 2 tầng |

---

## 5. Cơ sở dữ liệu

| Chủ đề | Quy tắc |
| --- | --- |
| Đặt tên | `snake_case`; bảng số nhiều (`gl_postings`); khóa ngoại `<bảng_số_ít>_id` |
| Khóa chính | UUIDv7 cho bảng chứng từ/phát sinh; `int` cho danh mục **+ cột `uid UUIDv7` ổn định** (ADR-012) |
| Migration | Alembic, chạy **theo từng PG schema dataset** (ADR-017). Một dataset = một schema = một lịch sử migration |
| Quyền | DDL/migration chạy bằng `ket_owner`; runtime dùng `ket_app` (không UPDATE/DELETE/DROP được `audit_log`) — ADR-019 |
| Bảng phát sinh | `gl_postings` append-only; chỉ `ket.posting` được ghi |

### Luật rút ra khi dựng migration `0001` — áp cho **mọi** migration sau

Sáu quy tắc dưới đây không phải phong cách: mỗi cái tương ứng một lỗi đã gặp
thật khi dựng phase 2A, và cả sáu đều hỏng **âm thầm** nếu quên.

| # | Quy tắc | Hỏng thế nào nếu quên |
| --- | --- | --- |
| D1 | **Cấp quyền ngay cạnh `create_table`** bằng `grant_app_read_write()` / `grant_app_append_only()`. Không dùng `GRANT ... ON ALL TABLES` ở cuối migration | `ON ALL TABLES` chỉ áp cho bảng đang tồn tại lúc chạy → bảng của migration sau không có quyền, lỗi lộ ra ở chỗ khác và muộn hơn nhiều |
| D2 | **Không khóa ngoại chéo schema** — kể cả tới `public.users`. Liên hệ lưu bằng `user_id` trần, kiểm ở tầng ứng dụng | Một schema dataset phải `pg_dump`/`pg_restore` độc lập được. FK trỏ ra ngoài làm bản dump không restore sang cụm khác được — đúng lúc cần nó nhất (RT-03/RT-14) |
| D3 | **Bảng mang `branch_id` phải gọi `enable_branch_rls_statements()`** | Không có policy = dữ liệu chi nhánh khác lọt ra, kể cả qua `SUM() OVER ()`. Có `tests/test_rls_policy_coverage.py` canh theo metadata: bảng mới thiếu policy là test đỏ |
| D4 | **Cột `NOT NULL` có default phía Python thì cũng phải có `server_default`** | Đường ghi set-based SQL (LD-14) và thao tác khôi phục của DBA không đi qua ORM → `NOT NULL violation` |
| D5 | **Ràng buộc duy nhất có cột NULL phải dùng index bộ phận**, không `UNIQUE(a, b, c)` | PostgreSQL coi mọi NULL là khác nhau → dòng trùng vẫn lọt, và truy vấn đọc trúng dòng nào tùy thứ tự. Lỗi không tái hiện được |
| D6 | **`SET LOCAL search_path` phải nêu `pg_temp` ở CUỐI** | Không nêu thì PostgreSQL tìm `pg_temp` **trước**; một `CREATE TEMP TABLE audit_log (…)` che được bảng thật và vô hiệu hóa nhật ký bất biến mà **không** cần UPDATE/DELETE dòng nào |
| D7 | **`CHECK (is_group OR …)` chỉ *miễn*, không *cấm*** — một nút nhóm vẫn có thể vi phạm luật đó. Nếu luật là "không được có `nature='goods'` trừ khi `is_group=true`" thì phải ghi: `CHECK (NOT is_group OR (nature IS NULL …))` để cấm nhóm mang `nature` nào | Nhóm sẽ khai được `nature='goods'`; phase 8 truy vấn tồn kho `WHERE nature IN (...)` sẽ cộng cả nút nhóm vào báo cáo, lỗi chỉ lộ ở phase sau trên dữ liệu khách hàng |
| D8 | **`CHECK` của model phải có mặt trong migration, và điều đó cần cổng riêng** — Alembic `compare_metadata()` **không** so sánh `CHECK`, nên quên chép một ràng buộc sang migration đi qua được mọi cổng hiện có | Migration là thứ dựng DB ở máy khách hàng, còn `CHECK` là nơi phần lớn luật của lớp danh mục được ép: thiếu một cái là mở một đường ghi dữ liệu sai mà máy lập trình viên vẫn xanh. Cổng: `tests/test_migrations_match_models.py` đọc thẳng `pg_constraint` |

Migration đã phát hành thì **cấm sửa** — viết migration mới. Model và migration
phải mô tả cùng một schema: `tests/test_migrations_match_models.py` so metadata
với DB thật sau khi chạy migration, còn khác biệt là còn thiếu migration.

### Cô lập dữ liệu bằng vai trò per-dataset (phase 2B-0)

| Khía cạnh | Quy tắc |
| --- | --- |
| Cấu trúc vai trò | `ket_owner` (sở hữu bảng + chạy DDL) · `ket_control` (vai trò **nhóm**, giữ quyền trên 3 bảng schema điều khiển) · `ds_<mã>_app` per-dataset (NOLOGIN, **INHERIT**, thành viên `ket_control`, giữ quyền bảng của schema mình) |
| `ket_app` | LOGIN, **NOINHERIT**, không có quyền trên bảng dataset nào. Là thành viên của mọi `ds_*_app` để `SET ROLE` được — `NOINHERIT` là thứ khiến tư cách thành viên đó không tự biến thành quyền |
| Mở transaction | `SET LOCAL ROLE ds_<mã>_app` → `SET LOCAL search_path` → `set_config('ket.branch_ids', …, local)`, trong `persistence.session.bind_transaction_scope`. Cả ba là `LOCAL` nên hết hiệu lực khi transaction kết thúc — **đó** là thứ cho phép dùng chung pool, không phải lớp pool đang dùng. Test canh: `test_transaction_scope_does_not_leak_through_pool.py` (chạy trên `QueuePool` thật vì `NullPool` không quan sát được rò) |
| Trần mã dataset | **56 ký tự** để chừa hậu tố `_app` (tên vai trò ≤ 63 ký tự). Vượt quá thì PostgreSQL cắt tên âm thầm và hai dataset dùng chung một vai trò. Kiểm ở `role_name_for_schema()` |
| Truy vấn chéo schema | ❌ `SELECT … FROM ds_beta.x` từ phiên đã bind `ds_alpha` → `42501 permission denied` |
| Giới hạn đã biết | Cơ chế này **không** chặn tiêm SQL **nhiều câu lệnh** (`SET ROLE` xét `session_user`). Xem `docs/adr/adr-017-schema-per-dataset.md` §Consequences |
| Luật cứng bù | Quét AST cấm f-string / chuỗi nối trong `text()`/`exec_driver_sql()`. Tham số ràng buộc ép psycopg dùng extended protocol, protocol đó không cho nhiều câu lệnh trong một lần gửi. Test: `test_no_sql_string_interpolation.py` |

---

## 6. TypeScript / web UI

| Chủ đề | Quy tắc |
| --- | --- |
| tsconfig | `strict` + `noUnusedLocals`, `noUnusedParameters`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, `noImplicitReturns`, `verbatimModuleSyntax` |
| Đặt tên | File `kebab-case.tsx`; component `PascalCase`; hook `useXxx` |
| Cấu trúc | `src/features/<nhóm-màn-hình>/` theo **nhóm màn hình của design**, không theo module backend |
| Type của API | Sinh từ OpenAPI vào `packages/api-types` — **không viết tay** |
| Phép tính tiền | **Không có ở client.** Cộng/trừ/làm tròn/quy đổi tỷ giá đều ở server. Client chỉ định dạng hiển thị |
| API Tauri | Chỉ được import `@tauri-apps/*` trong `src/lib/tauri/`. Ép bằng eslint `no-restricted-imports` — giữ đường lên chế độ trình duyệt LAN |
| Lint | eslint 10 flat config + `typescript-eslint` recommendedTypeChecked + react-hooks |
| TypeScript | 6.x. **Chưa lên 7** dù đã có bản stable: `typescript-eslint` 8 khai peer `<6.1.0`, nâng lên là mất lint |
| Không khai `baseUrl` | TS 6 deprecate, TS 7 bỏ hẳn. `paths` viết dạng `./` nên tự giải theo vị trí tsconfig |
| Tailwind | 4.x — theme khai bằng `@theme` trong `base.css`, **không có** `tailwind.config.js`. Token ngữ nghĩa dùng tiền tố `--ds-color-*` để không đè biến `--color-*` do Tailwind sinh (xem `docs/design-guidelines.md §2`) |

---

## 7. Rust (Tauri shell)

Rust edition **2024**, `rust-version = 1.85`.

Shell chỉ làm bốn việc: tự cập nhật, in ấn, chọn/lưu tệp, giữ phiên. **Không có
logic nghiệp vụ ở tầng Rust.** Không nhúng PKCS#11 — ký USB token là dịch vụ
esign riêng (ADR-016).

Trạng thái thực tế của bốn việc đó:

| Việc | Trạng thái |
| --- | --- |
| Chọn/lưu tệp, mở tệp in | plugin `dialog` + `opener` đã có |
| Tự cập nhật | **chưa có** — `tauri-plugin-updater` cần cặp khóa ký + endpoint phát hành, cả hai là sản phẩm của phase 11. Thêm sớm chỉ tạo cấu hình giả |
| Giữ phiên | phase 2 |

Quyền khai trong `src-tauri/capabilities/default.json`, giữ ở mức tối thiểu.
Quyền `fs:*` **chưa cấp**; phase nào cần đọc/ghi tệp thì cấp theo phạm vi hẹp
của đường dẫn do hộp thoại trả về, **không** cấp đệ quy cả Documents/Downloads.
Thêm quyền phải nêu lý do trong PR.

CSP trong `tauri.conf.json` giới hạn `connect-src` về cổng app server mặc định
(`https://*:5443`). Khách cấu hình cổng khác thì phase 11 (installer) phải sinh
CSP theo cấu hình, không nới về `https:`.

---

## 8. Hàng đợi tác vụ nền

| Chủ đề | Quy tắc |
| --- | --- |
| Loại job mới | Đăng ký một `JobType` vào `kernel.jobs.registry.REGISTRY`. Bốn thuộc tính **bắt buộc**, không có mặc định cho ba: `code` (`{nhóm}.{việc}`), `permission` (mã quyền cần để xếp hàng), `resume_semantics` (`IDEMPOTENT_RESTART` nếu chạy lại từ đầu an toàn, `CHECKPOINTED` nếu ghi mốc theo lô), `params_model` (Pydantic model của tham số) |
| Thân job | Chữ ký `(context: JobContext, params: <ParamsModel>) -> JobResult`. **Không** tự `commit()` — transaction do worker mở và đóng, cùng hợp đồng với `unit_of_work` của một request |
| Hủy | Kiểm `context.progress.cancel_requested()` giữa các **lô**, rồi ném `JobCancelled` — ném để transaction rollback; trả về một giá trị đặc biệt sẽ commit phần việc dở dang |
| Tiến độ | Báo qua `context.progress.report(...)`. Nó ghi bằng connection riêng (ngoài transaction nghiệp vụ) và gia hạn lease cùng lúc — không tự `UPDATE jobs` |
| Kết nối đặc quyền | Mặc định `JobPrivilege.DATASET` (chạy dưới `ds_<mã>_app` + RLS chi nhánh). `CONTROL_OWNER` chỉ cho việc chạm bảng schema điều khiển, và chỉ chạy khi bản cài khai `KET_WORKER_OWNER_DATABASE_URL` |
| Worker vs API | `api` không được import `worker` (import-linter C2): xếp hàng là ghi một dòng, chạy job là việc của tiến trình khác. Tầng API không bao giờ chạy thân job, kể cả job ngắn |

---

## 9. Hợp đồng client↔server (OpenAPI)

| Chủ đề | Quy tắc |
| --- | --- |
| Nguồn sự thật | Mã server. `scripts/export_openapi.py` → `client/packages/api-types/openapi.json` → `openapi-typescript` → `schema.d.ts`. **Cả hai tệp sinh đều được commit; không sửa tay** |
| Sinh lại | `make api-types`. Cổng: `make api-types-check` (job CI `api-types`) sinh lại rồi `git diff --exit-code` — đổi hợp đồng mà quên sinh lại thì đỏ, và diff cho người review thấy đổi gì. `tests/test_openapi_contract.py` canh cùng bất biến ở phía server |
| Model | Mọi thân request/response là Pydantic v2 **có docstring** (docstring thành `description` trong đặc tả). Không `dict[str, Any]` ở ranh giới API |
| Lỗi | Hợp đồng RFC 7807 (`ProblemDetails`) khai một lần cho mọi endpoint ở `create_app`, nên client bắt lỗi **có kiểu**. `error_code` là hợp đồng công khai — đổi mã là breaking change |

---

## 10. Lỗi và chuỗi hiển thị

| Chủ đề | Quy tắc |
| --- | --- |
| Lỗi nghiệp vụ | `DomainError(code, **args)`; handler FastAPI đổi thành RFC 7807 (phase 2) |
| Không lộ | Không trả exception thô / stack trace ra client (FR-NFR-050) |
| Chuỗi tiếng Việt | **Không hard-code trong logic nghiệp vụ.** Server trả `error_code` + tham số; client dựng thông điệp (FR-NFR-050, FR-NFR-034) |
| Đa ngôn ngữ | Resource vi/en cho UI + cột `name_en` trên danh mục và hệ thống tài khoản |

---

## 11. Bí mật

Không commit: mật khẩu DB, khóa mã hóa backup, khóa ký gói cấu hình, khóa ký
bản cập nhật Tauri, `totp_secret`, token eSign, file `.env`. Tất cả nằm ở OS
keystore hoặc biến môi trường lúc chạy (ADR-019).

---

## 12. Cổng CI

`make check` chạy đúng bộ CI chạy:

```
server:  ruff check · ruff format --check · mypy --strict · lint-imports
         pytest -m "not db"   (make server-test)
         pytest -m db         (make server-test-db — cần PostgreSQL thật)
client:  tsc --noEmit · eslint · vite build
shell:   cargo fmt --check · cargo clippy -D warnings · tauri build
```

**Nhóm test `db`.** Test nào chạm RLS, tách quyền sở hữu bảng, hay định tuyến
schema **phải** đánh `@pytest.mark.db`: đó là hành vi của PostgreSQL, không mô
phỏng được — một bộ test chạy trên SQLite sẽ xanh trong khi cơ chế cô lập dữ
liệu đã hỏng. Máy lập trình không có DB thì nhóm này tự bỏ qua; CI đặt
`KET_TEST_REQUIRE_DB=1` để nó **đỏ** thay vì bỏ qua.

**CI chia theo câu hỏi cần trả lời** (`.github/workflows/ci.yml`):

| Lúc | Chạy gì | Vì sao |
| --- | --- | --- |
| PR vào master | kiểu + luật phụ thuộc (1 lần, Linux) · pytest Linux + Windows · pytest có PostgreSQL · client (khi không đóng gói desktop) | Nhanh, chạy mọi lần. Windows là nền tảng khách hàng chính (LD-02) |
| Merge vào master | thêm pytest macOS · đóng gói desktop khi `client/**` đổi | "Còn đóng gói và chạy được không" |
| Hằng tuần | toàn bộ | Bắt trôi phụ thuộc khi mã của ta không đổi dòng nào |

Kiểm tra **không phụ thuộc HĐH** (ruff, mypy, import-linter, tsc, eslint, vite)
chạy **đúng một lần** — chạy ba lần cho ba HĐH không phát hiện thêm gì. Job chỉ
chạy khi vùng mã tương ứng đổi; PR chỉ sửa tài liệu không chạy job nào.

Cổng bắt buộc cho branch protection là **một** job duy nhất: `CI`. Nó tổng hợp
kết quả (bỏ qua = chấp nhận, đỏ = chặn) nên đặt từng job làm required check sẽ
chặn nhầm PR không đụng tới vùng đó.

Bất kỳ cổng nào đỏ = chặn merge. PR template hỏi: **"thay đổi này có ADR nào
liên quan không?"** — nếu đổi một quyết định trong `docs/adr/`, phải cập nhật
ADR đó (hoặc viết ADR mới supersede) trong cùng PR.
