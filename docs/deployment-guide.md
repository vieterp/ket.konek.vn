# Hướng dẫn triển khai — Konek Két

**Cập nhật:** 2026-08-16 · phản ánh **đúng thứ đang có trong repo** sau phase 2 lát 2C-1.

Phạm vi: dựng cụm PostgreSQL, khóa mã hóa, tài khoản đầu tiên, chạy app server và tiến trình worker, khôi phục sau sao lưu,
chạy test cục bộ, kênh phát hành bộ cài. Lệnh quản trị chạy qua `python -m ket.admin`.
Chưa có **bộ cài** app server/worker — cả hai chạy từ mã nguồn; đóng gói là spike S4 / phase 11.

---

## 1. PostgreSQL 16 là phiên bản đích

App **từ chối khởi động** trên cụm cũ hơn (`ket.main.verify_postgres_version`, đọc
`server_version_num`). Tắt được bằng `KET_VERIFY_POSTGRES_VERSION_ON_STARTUP=false` — cờ
này **tách riêng** khỏi cờ kiểm phiên bản schema, có chủ đích: hai cổng canh hai thứ khác
nhau (phiên bản **cụm** vs phiên bản **schema**).

CI chạy service container `postgres:16` (`.github/workflows/ci.yml`, job `server-db`).

### Cài song song với cụm cũ (macOS)

Homebrew mặc định cho PostgreSQL 16 cổng **5432** — trùng cụm cũ, nên phải đổi tay:

```bash
brew install postgresql@16
# đặt `port = 5433` trong /opt/homebrew/var/postgresql@16/postgresql.conf
brew services start postgresql@16
/opt/homebrew/opt/postgresql@16/bin/psql -p 5433 -d postgres -c 'select version()'
```

Linux: cài `postgresql-16`, sửa `port` trong `/etc/postgresql/16/main/postgresql.conf`,
`systemctl restart postgresql@16-main`.

---

## 1b. Hai biến môi trường mới (lát 2C-1)

### `KET_MINIMUM_CLIENT_VERSION` — mặc định bằng phiên bản server

Bản client cũ nhất còn **ghi** được vào bản cài này (LD-05, FR-NFR-054). Client
cũ hơn giá trị này vẫn đăng nhập và **tra cứu** bình thường; chỉ lệnh ghi
(`POST`/`PUT`/`PATCH`/`DELETE`) trả `426 Upgrade Required`, và client hiện màn
hình "cần cập nhật".

Năm đường của `/api/v1/auth` được miễn trừ (đăng nhập, đăng xuất, đổi mật khẩu,
đăng ký và xác nhận 2FA) — nếu không, tài khoản mang mật khẩu tạm hoặc chưa đăng
ký thiết bị 2FA sẽ bị **khóa cứng** thay vì chỉ-đọc, vì chúng cũng không đọc
được gì cho tới khi làm xong hai việc đó.

Mặc định bằng chính phiên bản server — hướng siết, vì nới ra là quyết định có
chủ đích của người triển khai còn siết vào là thứ họ sẽ quên làm. Trên thực tế
**chỉ tăng khi một migration phá tương thích**; tăng theo mỗi bản phát hành là
cách nhanh nhất để cả văn phòng rơi vào chế độ chỉ đọc vì một máy trạm chưa kịp
tự cập nhật.

```bash
KET_MINIMUM_CLIENT_VERSION=0.5.0   # client >= 0.5.0 ghi được; cũ hơn thì chỉ đọc
```

Giá trị không đúng khuôn `MAJOR.MINOR.PATCH` làm **tiến trình không khởi động**
(kiểm ở `Settings`), thay vì im lặng thành một cổng không làm gì cả.

### `KET_CORS_ALLOWED_ORIGINS` — mặc định là ba origin của webview Tauri

Webview Tauri không chạy ở origin của app server, nên mọi lời gọi của client
desktop là xuyên origin. Mặc định đã phủ cả hai hệ điều hành:

```
tauri://localhost          # macOS
http://tauri.localhost     # Windows
https://tauri.localhost    # Windows
```

**Cú pháp là JSON**, không phải danh sách ngăn cách bằng dấu phẩy — đây là kiểu
phức nên `pydantic-settings` đọc bằng JSON:

```bash
# Máy lập trình chạy `vite dev` (thêm origin 5173 vào mặc định)
KET_CORS_ALLOWED_ORIGINS='["tauri://localhost","http://tauri.localhost","https://tauri.localhost","http://localhost:5173"]'
```

Chế độ **trình duyệt trong LAN** (v1.x) không cần thêm mục nào: khi ấy app server
tự phục vụ chính bundle web đó nên request là same-origin.

### `VITE_KET_SERVER_URL` — **giá trị mặc định** cho địa chỉ app server

Địa chỉ app server mà bundle web sẽ gọi, đọc lúc `vite build`.

**Từ lát 2C-4 đây chỉ là mặc định cho lần chạy đầu, không còn là thứ chốt hạ.**
Người dùng khai lại địa chỉ ngay trên màn hình "không tới được máy chủ", và giá
trị ấy lưu tại máy trạm rồi **thắng** giá trị ghim lúc dựng. Đổi máy host không
còn đòi dựng lại gói và cài lại trên từng máy.

Cùng địa chỉ đó dùng cho **cả** lời gọi API lẫn đường tự cập nhật — khai một lần
là xong cả hai, không có hai chỗ cấu hình để trôi lệch khỏi nhau.

| Tình huống | Giá trị |
| --- | --- |
| App server tự phục vụ bundle (trình duyệt LAN, chế độ một máy) | **để trống** — client gọi chính origin đang phục vụ trang |
| `pnpm dev` trên máy lập trình | `http://127.0.0.1:5443` |
| Bản đóng gói Tauri | địa chỉ máy host thường gặp, ví dụ `https://host.lan:5443` — hoặc **để trống** và để người dùng khai ở lần chạy đầu |

Hai tình huống dưới là **xuyên origin**, nên origin tương ứng phải có trong
`KET_CORS_ALLOWED_ORIGINS` — thiếu thì trình duyệt chặn request trước khi nó rời
máy, và log máy chủ không có gì cả.

Mẫu đầy đủ kèm giải thích: `client/.env.example`.

Danh sách **đóng**, không `*`: hệ này giữ PII lương và bí mật doanh nghiệp, và
`*` biến mọi trang web mà nhân viên mở thành một chỗ có thể gọi API nội bộ. CORS
đặt ở **lớp ngoài cùng** của chuỗi middleware nên preflight `OPTIONS` được trả
lời trước hạn mức và trước cổng phiên bản — nếu không, một phản hồi `429`/`426`
tới trình duyệt mà thiếu header CORS và người dùng chỉ thấy "lỗi mạng".

---

## 2. Khởi tạo cụm lần đầu

### 2.1 Bốn vai trò nền (chạy bằng superuser, một lần cho mỗi cụm)

```bash
psql -p 5433 -d postgres -c 'CREATE DATABASE ket OWNER ket_owner'
psql -p 5433 -d ket -f server/src/ket/kernel/security/roles.sql
```

> `roles.sql` tạo cả bốn vai trò, nên lệnh `CREATE DATABASE … OWNER ket_owner` ở trên chỉ
> chạy được khi vai trò đã có từ một cụm trước. Lần đầu tuyệt đối: chạy `roles.sql` trên
> database `postgres` trước, rồi mới `CREATE DATABASE`.

`roles.sql` tạo — **không vai trò nào là superuser**:

| Vai trò | Thuộc tính | Việc |
| --- | --- | --- |
| `ket_owner` | LOGIN, INHERIT, **CREATEROLE**, NOSUPERUSER | Sở hữu schema/bảng, chạy DDL + migration, tạo vai trò dataset |
| `ket_app` | LOGIN, **NOINHERIT**, NOSUPERUSER | Vai trò runtime của app server. Không sở hữu bảng nào, không có quyền trên bảng dataset nào |
| `ket_control` | NOLOGIN, INHERIT (nhóm) | Giữ quyền trên 3 bảng schema điều khiển, cho vai trò dataset kế thừa |
| `ket_worker` | LOGIN, **NOINHERIT**, NOSUPERUSER | Vai trò của tiến trình worker nền. Quyền riêng chỉ trên bảng `jobs` của từng dataset: `SELECT`, và `UPDATE` **theo cột** cho các cột cơ chế (trạng thái, tiến độ, kết quả, lease) — không đổi được `type`/`params`/`requested_by`/`branch_id` của một tác vụ |

`ket_owner` cần `CREATEROLE` vì mỗi dữ liệu kế toán sinh thêm một vai trò `ds_<mã>_app`.
Đây không phải nới quyền thực chất — owner vốn sở hữu toàn bộ dữ liệu — và `CREATEROLE`
vẫn không cấp được `SUPERUSER` hay `BYPASSRLS`, nên nhật ký bất biến và RLS không bị chạm.

### 2.2 Schema điều khiển + vai trò dataset

```bash
cd server && uv run python -m ket.admin ensure-cluster
```

Đọc DSN từ `KET_OWNER_DATABASE_URL` (xem `ket.settings`). Bên trong là
`ensure_control_schema` + `ensure_dataset_roles`: dựng bảng điều khiển, **chạy bước nâng
cấp schema điều khiển nếu cụm cũ hơn mã nguồn**, cấp lại quyền, dựng vai trò cho mọi
dataset đã đăng ký. Chạy lại được, chạy sau mỗi lần nâng cấp bản cài.

Cụm đã dựng mà **mất dòng phiên bản** trong `system_metadata` thì lệnh này **dừng** thay
vì đoán: không có phiên bản cũ thì không suy được cần nâng cấp những gì, và dán nhãn bừa
sẽ cho ra một DB tự nhận là mới trong khi thiếu cột. Cách xử lý: khôi phục bản sao lưu
gần nhất, hoặc ghi đúng phiên bản đang có bằng tay rồi chạy lại.

### 2.2b Khóa mã hóa ứng dụng (ADR-019)

```bash
cd server && uv run python -m ket.admin generate-app-key
```

Ghi một khóa Fernet vào OS keystore của máy chạy app server (Keychain trên macOS,
Credential Manager trên Windows). Bí mật `totp_secret` được mã hóa bằng khóa này, nên
**bản dump và bản sao lưu không chứa bí mật dạng rõ**.

Ba điều phải nhớ:

* Chạy lệnh này **ghi đè** khóa cũ. Bí mật mã hóa bằng khóa cũ không mở lại được — người
  dùng đã bật 2FA phải đăng ký lại thiết bị sinh mã.
* Khôi phục DB sang máy khác mà không mang theo khóa: đăng nhập thường vẫn chạy, chỉ tài
  khoản bật 2FA mới hỏng, với thông điệp nêu đúng nguyên nhân.
* Môi trường không có keystore (container, CI) đặt `KET_APP_KEY` thay thế.

### 2.2c Tài khoản đầu tiên

```bash
cd server && uv run python -m ket.admin create-user ketoantruong --email ke.toan@congty.vn
```

Hỏi mật khẩu hai lần (hoặc `--password-stdin` cho script cài đặt — tham số dòng lệnh nằm
trong `ps` và lịch sử shell, nên **không** truyền mật khẩu qua đó). Tài khoản tạo ra bắt
buộc đổi mật khẩu ở lần đăng nhập đầu.

Các lệnh phá-kính khác, chạy tại máy chủ khi không còn đường nào qua HTTP:

| Lệnh | Dùng khi | Hệ quả |
| --- | --- | --- |
| `reset-password <tên>` | Quên mật khẩu, không có quản trị viên khác | Đặt lại mật khẩu, mở khóa tài khoản, **thu hồi mọi phiên đang mở** |
| `reset-totp <tên>` | Mất điện thoại sinh mã 2FA | Xóa bí mật **và cờ bắt buộc** để người dùng đăng ký lại thiết bị |
| `grant-role <tên> --dataset <mã> --role admin` | Người **đầu tiên** của một dữ liệu kế toán | Gán vai trò; vai trò nhạy cảm bật luôn cờ bắt buộc 2FA |
| `grant-branch <tên> --dataset <mã> --branch <mã CN>` | Sau khi tạo tài khoản | Chưa gán chi nhánh nào = **không thấy dòng nào**, không phải "thấy tất" |

Hai lệnh cuối tồn tại vì lý do vòng: gán vai trò qua HTTP đòi quyền `system.role.edit`,
mà chưa ai được cấp quyền đó trên một dữ liệu kế toán vừa tạo.

### 2.3 Tạo một dữ liệu kế toán

```python
from ket.kernel.datasets.provisioning import provision_dataset

provision_dataset(owner, code="kt2026", name="Công ty A", scheme="TT200")
```

Tạo schema `ds_kt2026`, vai trò `ds_kt2026_app`, chạy toàn bộ migration lên schema đó,
rồi **gieo mã quyền + vai trò hệ thống `admin`** (đủ mọi quyền đã đăng ký).

### 2.4 Cho người đầu tiên vào làm việc

```bash
cd server
uv run python -m ket.admin grant-role ketoantruong --dataset kt2026 --role admin
uv run python -m ket.admin grant-branch ketoantruong --dataset kt2026 --branch CN01
```

`admin` mang quyền nhạy cảm (`system.user.*`, `system.role.*`) nên lệnh đầu **bật luôn
cờ bắt buộc 2FA**. Lần đăng nhập kế tiếp vì thế trả về một **phiên hạn chế**
(`session_scope = "totp_enrollment"`): nó chỉ dùng được cho `/api/v1/auth/totp/enroll`
và `/totp/confirm`. Người dùng tự quét mã QR, xác nhận, rồi đăng nhập lại kèm mã — không
cần ai chạm máy chủ. `reset-totp` chỉ dành cho trường hợp **mất thiết bị**.

Chi nhánh (`CN01` ở trên) phải tồn tại trước; nó được tạo qua
`POST /api/v1/system/branches` bởi một tài khoản có `system.branch.create`.

---

## 3. Sao lưu & khôi phục

**Quy trình đã chốt: `pg_dump` từng database.** Điểm phải nhớ: vai trò là đối tượng **cấp
cụm**, `pg_dump` của một database **không** chứa chúng.

### 3.1 Sao lưu

```bash
pg_dump -p 5433 -Fc ket > ket.dump                     # cả bản cài
pg_dump -p 5433 -Fc -n ds_kt2026 -n public ket > kt2026.dump   # một doanh nghiệp
```

Dump theo schema phải kèm `-n public`: bảng đăng ký `datasets` nằm ở đó, thiếu nó thì
schema khôi phục xong vẫn vô hình với ứng dụng.

### 3.2 Khôi phục — thứ tự bắt buộc

```bash
# 1. Database rỗng thuộc ket_owner + bốn vai trò nền (superuser)
psql -p 5433 -d postgres -c 'CREATE DATABASE ket OWNER ket_owner'
psql -p 5433 -d ket -f server/src/ket/kernel/security/roles.sql

# 2. Dữ liệu — chạy BẰNG ket_owner, bỏ phần phân quyền (bước 3 cấp lại)
pg_restore -p 5433 -U ket_owner -d ket --no-privileges ket.dump

# 3. Dựng lại vai trò của từng dataset + cấp lại quyền  ← KHÔNG BỎ QUA
cd server && uv run python -m ket.admin ensure-cluster

# 4. Khởi động app
cd server && uv run uvicorn ket.main:app --host 127.0.0.1 --port 5443
```

Bỏ bước 3 thì app chết lúc khởi động với `role "ds_<mã>_app" does not exist`, vì đường
kiểm phiên bản schema chuyển sang vai trò dataset trước khi đọc `alembic_version`.

**Vì sao `--no-privileges`.** Bản dump chứa `GRANT … TO ds_<mã>_app`, mà bước 1 mới chỉ
dựng bốn vai trò nền — vai trò dataset chưa tồn tại. Không có cờ này, `pg_restore` in một
loạt `ERROR: role "ds_<mã>_app" does not exist`, kết bằng `errors ignored on restore: N` và
**thoát mã 1**. Dữ liệu vào đủ và bước 3 sửa được, nhưng script `set -e` sẽ dừng ngay đó và
không bao giờ chạy bước 3, còn người làm tay thì tưởng bản sao lưu hỏng.

**Vì sao `-U ket_owner`, và vì sao KHÔNG dùng `--no-owner`.** `--no-owner` gán mọi đối
tượng cho vai trò **đang chạy** `pg_restore`. Khôi phục bằng superuser kèm cờ đó cho ra một
database mà `ket_owner` **không sở hữu gì cả**, và bước 3 đổ ngay ở
`GRANT … ON public.users`: *permission denied for table users*. Chạy `pg_restore` bằng
chính `ket_owner` giữ đúng chủ sở hữu — cũng là điều kiện cần của nhật ký bất biến và RLS.

Đã diễn tập trọn vẹn quy trình trên PostgreSQL 16.15: `pg_restore` thoát 0; trước bước 3
app từ chối khởi động; sau bước 3 cổng khởi động xanh và `audit_log` vẫn đúng
`INSERT+SELECT`, `alembic_version` vẫn chỉ `SELECT`.

**Vai trò do superuser tạo lại** (ví dụ khôi phục `pg_dumpall --globals-only`) sẽ khiến
`ket_owner` không có ADMIN OPTION trên chúng; provisioning báo lỗi
`dataset.role_not_administrable` kèm câu lệnh sửa:

```sql
GRANT ds_<mã>_app TO ket_owner WITH ADMIN OPTION;   -- chạy bằng superuser
```

---

## 4. Cô lập dữ liệu kế toán bằng vai trò

```
ket_owner     LOGIN, CREATEROLE   -- sở hữu bảng, chạy DDL
ket_app       LOGIN, NOINHERIT    -- runtime; thành viên mọi ds_*_app để SET ROLE được
ket_control   NOLOGIN (nhóm)      -- quyền trên bảng schema điều khiển
ds_<mã>_app   NOLOGIN, INHERIT    -- thành viên ket_control, giữ quyền bảng của schema mình
```

Mỗi transaction mở bằng `SET LOCAL ROLE ds_<mã>_app` → `SET LOCAL search_path` →
`set_config('ket.branch_ids', …, local)` (`kernel.persistence.session.bind_transaction_scope`).
Cả ba là `LOCAL` nên hết hiệu lực khi transaction kết thúc — đó là điều kiện để nhiều
người dùng dùng chung một connection pool.

### Ranh giới thật (đo trên PostgreSQL 16.15)

| Loại tiêm SQL | Bị chặn? |
| --- | --- |
| Một câu lệnh ghi rõ `ds_beta.x` | ✅ `42501`, chặn ngay lúc lập kế hoạch |
| Một câu lệnh + `set_config('role', …)` lồng trong subquery/CTE | ✅ |
| Nhiều câu lệnh: `…; SET ROLE ds_beta_app; SELECT … FROM ds_beta.x` | ❌ **KHÔNG** |

`SET ROLE` xét tư cách thành viên của `session_user` (`ket_app`), mà `ket_app` buộc phải
là thành viên của mọi `ds_*_app` — đó chính là điều kiện để `SET LOCAL ROLE` chạy được.

**Luật bù, bắt buộc:** mọi `text()` chạy chuỗi có phần do người dùng ảnh hưởng **phải**
kèm tham số ràng buộc; psycopg khi đó dùng extended protocol, và protocol đó không cho
nhiều câu lệnh trong một lần gửi. Canh bằng `tests/test_no_sql_string_interpolation.py`
(quét AST, miễn trừ phải khai kèm lý do). Đường siết triệt để — vai trò đăng nhập riêng +
pool riêng mỗi dataset — ghi trong `docs/adr/adr-017-schema-per-dataset.md`.

### Trần mã dữ liệu kế toán = 56 ký tự

Schema là `ds_<mã>`, vai trò là `ds_<mã>_app`. Trần identifier của PostgreSQL là 63, nên
mã phải trừ cả `ds_` lẫn `_app`. Vượt quá thì PostgreSQL **cắt tên âm thầm** và hai dataset
dùng chung một vai trò. `validate_dataset_code` và `role_name_for_schema` ném
`InvalidSchemaNameError`.

---

## 5. Chạy test cục bộ

Mọi lệnh chạy từ **gốc repo**; Makefile tự `cd server` và đặt biến môi trường.

```bash
make server-test      # nhóm không cần DB
make server-test-db   # nhóm cần PostgreSQL 16 (tự đặt PGPORT=5433)
make server-coverage  # toàn bộ test + số độ phủ — đúng con số CI dán vào PR
make check            # toàn bộ cổng: server + client
```

Cụm chạy 16 ngay trên 5432 thì đè: `make server-test-db PGPORT=5432`.

Nhóm `db` **xóa và dựng lại vai trò ở phạm vi cụm** mỗi phiên (vai trò sót lại từ phiên
trước từng làm cổng bảo mật xanh vì trạng thái cũ). Vì vậy nó đòi
`KET_TEST_DESTRUCTIVE_CLUSTER=1` — Makefile và CI đã đặt sẵn; chạy `pytest` tay thì phải
tự đặt. **Đừng trỏ nhóm test này vào cụm có bản cài thật.**

---

## 6. Kênh phát hành bộ cài desktop

`.github/workflows/release.yml` chạy khi **merge vào master và phiên bản đổi** (so với tag
`v<phiên bản>`), hoặc chạy tay qua `workflow_dispatch`.

| Nền tảng | Sản phẩm |
| --- | --- |
| macOS | `.dmg` (universal: arm64 + x86_64), `.app.tar.gz` + `.sig` cho updater |
| Windows | `.msi` (triển khai hàng loạt qua Group Policy) và NSIS `-setup.exe` (cài không cần quyền admin) |
| Chung | `latest.json` — manifest updater, gộp sau khi cả hai nền tảng xong |

Release tạo ở dạng **nháp**, tự đánh dấu prerelease khi phiên bản `0.x`.

**Chữ ký:** gói cập nhật ký bằng khóa minisign trong GitHub Secrets
(`TAURI_SIGNING_PRIVATE_KEY`, `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`); khóa công khai nằm
trong `client/src-tauri/tauri.conf.json`. Đây **không phải** chữ ký chứng thư hệ điều hành
— xem §7.

**Tên sản phẩm bundle là `Ket`**, không dấu. Không phải chuyện thẩm mỹ: `bundle_dmg.sh`
**hỏng** với tên `Konek Két`, và chạy sạch với `Ket` trên cùng một lệnh build. `msi` cũng
được thêm lại nhờ đó. Tên hiển thị trong ứng dụng vẫn là "Konek Két".

**Năm tệp khai phiên bản phải khớp** — `.github/scripts/check_version_consistency.py`,
chạy trong CI của mọi PR và là cổng đầu tiên của release:

```
server/pyproject.toml            client/package.json
server/src/ket/__init__.py       client/src-tauri/Cargo.toml
                                 client/src-tauri/tauri.conf.json
```

Chạy cục bộ: `make version-check`.

### 6.1. App server tự phục vụ gói cập nhật (lát 2C-4)

Bản cài LAN **không có internet** (LD-01), nên máy trạm không đi hỏi GitHub
Releases được. App server là nơi duy nhất mọi máy trạm đều với tới, nên nó cũng
là nguồn cập nhật.

**Bật lên:** đặt `KET_UPDATES_DIR` tới một thư mục app server đọc được. Chưa đặt
= chưa bật, và endpoint trả `204` — máy trạm hiểu là "chưa có bản mới" và chạy
bình thường, không nhận lỗi nào.

**Đẩy một bản lên** (chạy tại máy chủ, sau khi đã tải sản phẩm từ release):

```bash
python -m ket.admin publish-update /duong/dan/Ket_0.9.0_x64-setup.exe \
  --version 0.9.0 --target windows --arch x86_64 --notes "Lưới nhập liệu"
```

Cạnh gói **phải có tệp `.sig`** cùng tên — `tauri build` sinh nó khi
`createUpdaterArtifacts` bật. Thiếu chữ ký thì lệnh từ chối ngay: mọi máy trạm
sẽ khước từ một gói không ký, và phát hiện điều đó sau khi đã copy 80MB lên máy
chủ là quá muộn.

`--target` / `--arch` gõ tường minh theo từ vựng updater Tauri
(`darwin|windows|linux`, `x86_64|aarch64|i686|armv7`). Gõ sai thì lệnh từ chối;
nếu suy từ tên tệp thì một lần suy sai là cả một nền tảng không nhận được bản
cập nhật mà không có gì báo.

Lệnh ghi `index.json` trong thư mục đó. **Endpoint chỉ phục vụ tệp có tên trong
danh mục ấy** — kể cả `.sig` nằm ngay cạnh cũng không tải được.

**Máy trạm biết hỏi ở đâu:** shell dựng địa chỉ từ chính app server mà client
đang dùng — cùng địa chỉ với mọi lời gọi API, khai được lúc chạy (xem
`VITE_KET_SERVER_URL` ở §3). Trước lát 2C-4, endpoint updater ghim
`https://localhost:5443` **độc lập** với địa chỉ ấy, nên ngay cả một bản đóng
gói dựng đúng cho khách cũng có updater hỏng: mỗi máy trạm đi hỏi chính nó, im
lặng, mãi mãi.

Thứ giữ an toàn cho việc lấy địa chỉ lúc chạy là **khóa công khai ghim trong
`tauri.conf.json`**: gói không mang chữ ký khớp khóa ấy thì updater từ chối cài.
Vì thế khóa ký **không bao giờ** được cấu hình lúc chạy.

**Kho xếp theo `{target}/{arch}/`** nên hai nền tảng cùng tên gói
(`Ket_0.9.0.tar.gz` trên cả macOS lẫn Linux là chuyện bình thường) không đè lên
nhau. Đường tải vì thế cũng mang cả ba khóa:
`/updates/download/{target}/{arch}/{tên}`.

**Danh mục hỏng không làm sập gì.** `index.json` sửa tay lỗi, mất quyền đọc, hay
do một bản server mới hơn ghi → endpoint trả `204`/`404` và ghi log mức `error`.
Bản cài vẫn ghi sổ bình thường: một sự cố cập nhật không được thành một sự cố kế
toán.

---

## 7. Chạy tiến trình worker

```bash
cd server && uv run python -m ket.worker
```

Tiến trình này chạy song song với app server; bản cài đích sẽ cài cả hai như
dịch vụ cùng một installer (phase 11). Nhiều tiến trình worker chạy được — hàng
đợi dùng `FOR UPDATE SKIP LOCKED` nên hai worker không bao giờ giành cùng một
job. Tùy chọn `--once` chạy một vòng rồi thoát (dùng cho lịch OS):

```bash
cd server && uv run python -m ket.worker --once
```

**Cấu hình worker** qua biến môi trường `KET_*` (cùng hàng với app server):

| Biến | Mặc định | Ghi chú |
| --- | --- | --- |
| `KET_WORKER_DATABASE_URL` | `postgresql+psycopg://ket_worker@localhost/ket` | DSN của vai trò `ket_worker` — **khác** app server |
| `KET_WORKER_OWNER_DATABASE_URL` | `(trống)` | DSN `ket_owner` chỉ cho job dọn phiên đăng nhập. Để trống (mặc định) = worker **không** cầm quyền owner, việc dọn phiên bị từ chối nếu xếp hàng. Chỉ khai khi bản cài muốn chạy dọn phiên qua hàng đợi |
| `KET_WORKER_POLL_SECONDS` | `2` | Nghỉ bao lâu khi mọi dữ liệu kế toán đều hết việc — độ trễ không ai nhận ra, mà vẫn đủ thưa để một bản cài để không cả đêm không tạo ra hàng chục nghìn truy vấn rỗng |
| `KET_JOB_LEASE_SECONDS` | `60` | Một lần giành job giữ bao lâu nếu worker im lặng. Phải ≥ 3× `job_heartbeat_seconds` |
| `KET_JOB_HEARTBEAT_SECONDS` | `15` | Nhịp gia hạn lease trong lúc job chạy. Phải < `job_lease_seconds` ÷ 3 |
| `KET_JOB_MAX_ATTEMPTS` | `3` | Số lần một job được giành trước khi reaper đánh hỏng nó |
| `KET_WORKER_REAP_SECONDS` | `30` | Khoảng cách quét job hết lease (khác với `POLL_SECONDS`); reaper chạy ít thường xuyên hơn để tiết kiệm I/O |

**Ý nghĩa lease/heartbeat/reaper:** để chống job mồ côi (worker chết giữa chừng).
Worker giành một job, được giữ (lease) bao lâu; trong lúc chạy, định kỳ gia hạn
(heartbeat); nếu worker chết (không gia hạn lần nữa), reaper quét và xếp lại
job vào hàng để worker khác chạy. Chi tiết: ADR-014, RT-13.

---

## 8. Đã biết là chưa xong

| # | Việc | Hệ quả |
| --- | --- | --- |
| 1 | **Đường build Windows chưa chạy lần nào** — máy phát triển là macOS | `.msi`/NSIS và gói updater Windows chỉ xác minh được ở lần chạy `release.yml` đầu tiên |
| 2 | **Chưa ký chứng thư hệ điều hành** | macOS Gatekeeper và Windows SmartScreen sẽ cảnh báo. Cần Apple Developer ID + chứng thư Authenticode (phase 11) |
| 3 | ~~**Plugin updater runtime chưa dựng**~~ **XONG (lát 2C-4)** | Endpoint đặt ở tầng Rust lúc chạy, theo địa chỉ app server người dùng đang dùng — xem §6.1. Còn lại: bộ cài chưa ký chứng thư OS (mục 2) và chưa dựng thử trên Windows (mục 1) |
| 4 | **Chưa có bộ cài app server** | Server chạy từ mã nguồn (`uv run uvicorn`). Đóng gói Python + native deps là spike S4 |
| 5 | Cụm dev đã tạo dataset bằng mã **trước** lát 2B-0 còn quyền bảng cấp thẳng cho `ket_app` | Chạy `python -m ket.admin ensure-cluster` một lần để thu hồi |
| 6 | Cụm cài bằng mã **trước** lát 2B-1b còn cấp `INSERT/UPDATE` trên `users` và `auth_sessions` cho nhóm `ket_control` (tức mọi vai trò dataset) | `ensure-cluster` thu hồi. Chạy nó sau **mọi** lần nâng cấp — đây là đường leo thang từ một lỗ tiêm SQL bất kỳ tới việc tự cấp phiên |
| 7 | Dọn `auth_sessions` hết hạn chạy **bằng tay**: `python -m ket.admin prune-sessions --retention-days 30` (mặc định giữ 30 ngày sau khi phiên chết) | Chạy theo quý là đủ ở quy mô mục tiêu. Lệnh dùng `ket_owner` vì vai trò runtime cố ý không có `DELETE`, và mỗi lần chạy để lại một dòng `control_audit_log`. Cùng nhịp đó chạy `python -m ket.admin prune-idempotency-keys` (khóa hết hạn, mọi dữ liệu kế toán, chạy bằng vai trò runtime). Xếp hàng qua hàng đợi cũng được: `system.maintenance.prune_idempotency_keys` (quyền `system.maintenance.create`) và `system.maintenance.prune_sessions` (quyền `system.installation.create` — cấp bản cài, đòi 2FA, **và** cần `KET_WORKER_OWNER_DATABASE_URL`). Chưa có bộ đặt lịch: chạy tay hoặc để lịch OS gọi `python -m ket.worker --once` |
| 8 | Hạn mức request đếm **trong tiến trình** (lát 2B-2a): `KET_RATE_LIMIT_PER_MINUTE` mặc định 600, `KET_RATE_LIMIT_AUTH_PER_MINUTE` mặc định 30, đặt `0` để tắt | Ngân sách neo theo **địa chỉ IP** (nhóm `auth`) và `(IP, token)` cho phần còn lại, kèm trần tổng theo IP — header `Authorization` do người gọi tự khai không mua được ngân sách mới. Chạy nhiều tiến trình API thì hạn mức thực tế nhân theo số tiến trình — chấp nhận ở quy mô LAN. Vẫn giữ **trần 4 lần băm Argon2id đồng thời** (`503 auth.throttled`) như lớp thứ hai |
