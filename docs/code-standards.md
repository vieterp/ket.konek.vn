# Chuẩn code — Konek Kế toán

Chốt ở phase 1. Nguyên tắc chọn quy tắc: **chỉ giữ quy tắc kiểm được tự động,
hoặc quy tắc mà vi phạm gây sai số tiền.** Quy tắc "phong cách" không kiểm được
thì không đưa vào đây — không ai theo.

Neo: ADR-015 (kỷ luật kiểu), ADR-004 (luật phụ thuộc), ADR-014 (set-based SQL),
LD-13, LD-14.

---

## 1. Tiền và số học — quy tắc quan trọng nhất

| Chủ đề | Quy tắc | Ép bằng |
| --- | --- | --- |
| Kiểu tiền | Chỉ `decimal.Decimal`. **Cấm `float`** trên **toàn bộ `server/src/konek`** (bộ quét không chừa package nào) | `server/tests/test_no_float_in_domain.py` — quét AST, chạy trong CI |
| Ngoại lệ cấm float | Chỉ mã vẽ biểu đồ (phase 10b). Thêm đường dẫn vào `ALLOWED_PREFIXES` **kèm lý do**, không nới bộ quét | Review |
| Làm tròn | Một module duy nhất `konek.kernel.money` (phase 3). `ROUND_HALF_UP`; scale lấy từ cấu hình, không hard-code (FR-SYS-064) | Review + test |
| Cột tiền trong DB | `NUMERIC(18,4)` cho đơn giá / tỷ giá · `NUMERIC(18,2)` cho thành tiền | Review migration |
| Đa tiền tệ | Mỗi dòng phát sinh lưu đủ bộ `(currency, rate, amount_fc, amount_debit, amount_credit)`; sổ cái lưu VND quy đổi | Schema phase 4 |

> Vì sao gắt: một phép cộng `float` lọt vào đường ghi sổ đủ làm lệch cân đối và
> rất khó truy ngược. Bộ quét đọc AST nên `float` trong docstring/comment không
> bị báo nhầm; nó bắt annotation, ép kiểu, và literal dấu phẩy động.

---

## 2. Kiểu tĩnh (Python)

| Chủ đề | Quy tắc |
| --- | --- |
| mypy | `strict = true` trên toàn `server/src/konek`, **fail = chặn merge**. Không có ngoại lệ per-module |
| Cờ bật thêm | `disallow_untyped_defs`, `disallow_any_generics`, `warn_return_any`, `strict_equality`, `no_implicit_optional`, `warn_unused_ignores`, `warn_unreachable` |
| `# type: ignore` | Cấm dạng trần — `enable_error_code = ["ignore-without-code"]` bắt phải nêu mã lỗi. Mỗi ignore phải kèm lý do trong comment và được review. **Đếm số ignore trong báo cáo mỗi phase** |
| Ranh giới API | Pydantic v2 model cho mọi request/response |
| DTO nội bộ | Pydantic model hoặc dataclass có kiểu |
| Cấm | `dict[str, Any]` đi qua ranh giới module |

Hiện trạng phase 1: `mypy --strict` xanh trên 41 file, **0** `# type: ignore`.

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
| Layout | `src/` layout, package `konek` |
| Đặt tên | `snake_case` cho module/hàm/biến, `PascalCase` cho class (ruff `N`) |
| Lint | ruff: `E,W,F,I,N,UP,B,ANN,S,C4,DTZ,T20,RUF` |
| Tắt có chủ đích | `E501` (formatter lo), `RUF001/002/003` (docstring tiếng Việt chứa ký tự "mơ hồ" — en dash, dấu thanh) |
| SQL | Truy vấn nghiệp vụ dùng SQLAlchemy Core/ORM có kiểu; báo cáo và tính khối lượng lớn dùng SQL text **đã tham số hóa**. **Cấm nối chuỗi SQL** |
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
| Quyền | DDL/migration chạy bằng `konek_owner`; runtime dùng `konek_app` (không UPDATE/DELETE/DROP được `audit_log`) — ADR-019 |
| Bảng phát sinh | `gl_postings` append-only; chỉ `konek.posting` được ghi |

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
| Lint | eslint 9 flat config + `typescript-eslint` recommendedTypeChecked + react-hooks |

---

## 7. Rust (Tauri shell)

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

## 8. Lỗi và chuỗi hiển thị

| Chủ đề | Quy tắc |
| --- | --- |
| Lỗi nghiệp vụ | `DomainError(code, **args)`; handler FastAPI đổi thành RFC 7807 (phase 2) |
| Không lộ | Không trả exception thô / stack trace ra client (FR-NFR-050) |
| Chuỗi tiếng Việt | **Không hard-code trong logic nghiệp vụ.** Server trả `error_code` + tham số; client dựng thông điệp (FR-NFR-050, FR-NFR-034) |
| Đa ngôn ngữ | Resource vi/en cho UI + cột `name_en` trên danh mục và hệ thống tài khoản |

---

## 9. Bí mật

Không commit: mật khẩu DB, khóa mã hóa backup, khóa ký gói cấu hình, khóa ký
bản cập nhật Tauri, `totp_secret`, token eSign, file `.env`. Tất cả nằm ở OS
keystore hoặc biến môi trường lúc chạy (ADR-019).

---

## 10. Cổng CI

`make check` chạy đúng bộ CI chạy:

```
server:  ruff check · mypy --strict · lint-imports · pytest
client:  tsc --noEmit · eslint · vite build
shell:   cargo fmt --check · cargo clippy · tauri build
```

Bất kỳ cổng nào đỏ = chặn merge. PR template hỏi: **"thay đổi này có ADR nào
liên quan không?"** — nếu đổi một quyết định trong `docs/adr/`, phải cập nhật
ADR đó (hoặc viết ADR mới supersede) trong cùng PR.
