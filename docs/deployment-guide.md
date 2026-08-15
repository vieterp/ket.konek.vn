# Hướng dẫn triển khai — Konek Két

**Cập nhật:** 2026-08-15 · phản ánh **đúng thứ đang có trong repo** sau phase 2 lát 2B-0.

Phạm vi: dựng cụm PostgreSQL, khôi phục sau sao lưu, chạy test cục bộ, kênh phát hành
bộ cài. Chưa có bộ cài app server và chưa có CLI — hai thứ đó thuộc spike S4 / phase 11.

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

## 2. Khởi tạo cụm lần đầu

### 2.1 Ba vai trò nền (chạy bằng superuser, một lần cho mỗi cụm)

```bash
psql -p 5433 -d postgres -c 'CREATE DATABASE ket OWNER ket_owner'
psql -p 5433 -d ket -f server/src/ket/kernel/security/roles.sql
```

> `roles.sql` tạo cả ba vai trò, nên lệnh `CREATE DATABASE … OWNER ket_owner` ở trên chỉ
> chạy được khi vai trò đã có từ một cụm trước. Lần đầu tuyệt đối: chạy `roles.sql` trên
> database `postgres` trước, rồi mới `CREATE DATABASE`.

`roles.sql` tạo — **không vai trò nào là superuser**:

| Vai trò | Thuộc tính | Việc |
| --- | --- | --- |
| `ket_owner` | LOGIN, INHERIT, **CREATEROLE**, NOSUPERUSER | Sở hữu schema/bảng, chạy DDL + migration, tạo vai trò dataset |
| `ket_app` | LOGIN, **NOINHERIT**, NOSUPERUSER | Vai trò runtime. Không sở hữu bảng nào, không có quyền trên bảng dataset nào |
| `ket_control` | NOLOGIN, INHERIT (nhóm) | Giữ quyền trên 3 bảng schema điều khiển, cho vai trò dataset kế thừa |

`ket_owner` cần `CREATEROLE` vì mỗi dữ liệu kế toán sinh thêm một vai trò `ds_<mã>_app`.
Đây không phải nới quyền thực chất — owner vốn sở hữu toàn bộ dữ liệu — và `CREATEROLE`
vẫn không cấp được `SUPERUSER` hay `BYPASSRLS`, nên nhật ký bất biến và RLS không bị chạm.

### 2.2 Schema điều khiển + vai trò dataset

Chưa có CLI; gọi trực tiếp bằng Python (bộ test dùng đúng đường này):

```python
from sqlalchemy import create_engine
from ket.kernel.datasets.bootstrap import ensure_cluster

owner = create_engine("postgresql+psycopg://ket_owner@localhost:5433/ket")
ensure_cluster(owner)   # bảng điều khiển + vai trò cho mọi dataset đã đăng ký
```

`ensure_cluster` = `ensure_control_schema` + `ensure_dataset_roles`. Chạy lại được.

### 2.3 Tạo một dữ liệu kế toán

```python
from ket.kernel.datasets.provisioning import provision_dataset

provision_dataset(owner, code="kt2026", name="Công ty A", scheme="TT200")
```

Tạo schema `ds_kt2026`, vai trò `ds_kt2026_app`, chạy toàn bộ migration lên schema đó.

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
# 1. Database rỗng thuộc ket_owner + ba vai trò nền (superuser)
psql -p 5433 -d postgres -c 'CREATE DATABASE ket OWNER ket_owner'
psql -p 5433 -d ket -f server/src/ket/kernel/security/roles.sql

# 2. Dữ liệu — chạy BẰNG ket_owner, bỏ phần phân quyền (bước 3 cấp lại)
pg_restore -p 5433 -U ket_owner -d ket --no-privileges ket.dump

# 3. Dựng lại vai trò của từng dataset + cấp lại quyền  ← KHÔNG BỎ QUA
python -c "
from sqlalchemy import create_engine
from ket.kernel.datasets.bootstrap import ensure_cluster
ensure_cluster(create_engine('postgresql+psycopg://ket_owner@localhost:5433/ket'))
"

# 4. Khởi động app
cd server && uv run uvicorn ket.main:app --host 127.0.0.1 --port 5443
```

Bỏ bước 3 thì app chết lúc khởi động với `role "ds_<mã>_app" does not exist`, vì đường
kiểm phiên bản schema chuyển sang vai trò dataset trước khi đọc `alembic_version`.

**Vì sao `--no-privileges`.** Bản dump chứa `GRANT … TO ds_<mã>_app`, mà bước 1 mới chỉ
dựng ba vai trò nền — vai trò dataset chưa tồn tại. Không có cờ này, `pg_restore` in một
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

---

## 7. Đã biết là chưa xong

| # | Việc | Hệ quả |
| --- | --- | --- |
| 1 | **Đường build Windows chưa chạy lần nào** — máy phát triển là macOS | `.msi`/NSIS và gói updater Windows chỉ xác minh được ở lần chạy `release.yml` đầu tiên |
| 2 | **Chưa ký chứng thư hệ điều hành** | macOS Gatekeeper và Windows SmartScreen sẽ cảnh báo. Cần Apple Developer ID + chứng thư Authenticode (phase 11) |
| 3 | **Plugin updater runtime chưa dựng** | Bộ cài chưa tự kiểm tra bản mới. Endpoint là địa chỉ app server của từng bản cài nên không biết lúc build — thuộc lát 2C / phase 11 |
| 4 | **Chưa có bộ cài app server** | Server chạy từ mã nguồn (`uv run uvicorn`). Đóng gói Python + native deps là spike S4 |
| 5 | Cụm dev đã tạo dataset bằng mã **trước** lát 2B-0 còn quyền bảng cấp thẳng cho `ket_app` | Chạy `ensure_cluster(owner)` một lần để thu hồi |
