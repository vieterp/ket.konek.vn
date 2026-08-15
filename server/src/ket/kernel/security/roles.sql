-- Vai trò DB của Konek (RT-02, RT-04, RT-06, D3) — chạy MỘT LẦN cho mỗi cụm
-- PostgreSQL, bởi superuser, trước khi tạo dữ liệu kế toán đầu tiên.
--
--   ket_owner   : sở hữu schema/bảng, chạy DDL + migration. Sở hữu `audit_log`.
--   ket_app     : vai trò runtime của app server. KHÔNG sở hữu bảng nào.
--   ket_control : vai trò NHÓM giữ quyền trên bảng schema điều khiển.
--
-- Vì sao cần `ket_control` (D3): quyền bảng dataset không còn cấp cho `ket_app`
-- mà cấp cho vai trò riêng của từng dataset (`ds_<mã>_app`), và app chuyển sang
-- vai trò đó bằng `SET LOCAL ROLE` mỗi transaction. Sau khi chuyển vai trò,
-- `current_user` không còn là `ket_app` nên các bảng điều khiển (`users`,
-- `datasets`, `system_metadata`) cũng phải với tới được. Gom quyền đó vào một
-- vai trò nhóm để mỗi vai trò dataset kế thừa, thay vì cấp lại cho từng vai trò
-- mỗi lần thêm một bảng điều khiển — cấp lặp là cách chắc chắn để trôi lệch.
--
-- Vì sao phải tách: `REVOKE UPDATE ON audit_log` chỉ có tác dụng lên vai trò
-- **không sở hữu** bảng. Nếu app server đăng nhập bằng chính chủ sở hữu thì nó
-- tự bỏ qua REVOKE của mình và "nhật ký bất biến" chỉ là lời hứa suông. Cùng
-- lý do đó, `ket_app` không được `BYPASSRLS`, nếu không cô lập chi nhánh
-- (RT-04) cũng vô hiệu.
--
-- MẬT KHẨU: cố ý không đặt ở đây. Script này nằm trong repo; mật khẩu thật do
-- installer sinh và cất ở OS keystore (ADR-019, phase 11), rồi đặt bằng
-- `ALTER ROLE ... PASSWORD`. Trên máy lập trình dùng `trust`/`peer` cục bộ nên
-- vai trò không mật khẩu vẫn đăng nhập được.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ket_owner') THEN
        CREATE ROLE ket_owner LOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ket_app') THEN
        CREATE ROLE ket_app LOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ket_control') THEN
        CREATE ROLE ket_control NOLOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ket_worker') THEN
        CREATE ROLE ket_worker LOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
    END IF;
END
$$;

-- Chạy lại được: hạ đặc quyền kể cả khi vai trò đã tồn tại từ trước với cấu
-- hình khác (máy lập trình, bản cài nâng cấp từ phiên bản cũ).
ALTER ROLE ket_owner   NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE ket_control NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOLOGIN;

-- `NOINHERIT` là bản lề của cả cơ chế cô lập dataset, không phải một tùy chọn
-- cứng hóa thêm cho chắc. Để `SET ROLE ds_alpha_app` chạy được thì `ket_app`
-- phải là **thành viên** của vai trò đó; mà thành viên mặc định **kế thừa luôn**
-- quyền của vai trò cha. Kế thừa ở đây nghĩa là `ket_app` tự động có USAGE trên
-- schema của MỌI dataset — đúng trạng thái mà D3 sinh ra để gỡ bỏ. `NOINHERIT`
-- buộc phải chuyển vai trò tường minh mới có quyền, và chỉ có quyền của đúng
-- một dataset trong suốt transaction đó.
ALTER ROLE ket_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;

-- `ket_worker` — vai trò đăng nhập của tiến trình chạy tác vụ nền (quyết định
-- D2/F2). Nó KHÔNG phải một `ket_app` thứ hai: quyền của nó trong mỗi schema
-- dataset dừng ở **đúng bảng `jobs`** (SELECT/UPDATE), đủ để giành việc và báo
-- tiến độ, không đủ để đọc một dòng sổ nào.
--
-- Vì sao worker không `SET ROLE ds_<mã>_app` ngay để giành việc: sau `SET ROLE`,
-- `current_user` là vai trò dataset nên policy RLS dành cho worker (`TO
-- ket_worker`, thấy job của mọi chi nhánh) sẽ không bao giờ áp. Muốn worker vẫn
-- thấy toàn hàng đợi thì phải mở một lỗ do GUC điều khiển ngay trong policy chi
-- nhánh của `jobs` — tức là làm yếu RLS cho **mọi** truy vấn khác chỉ để phục vụ
-- một tiến trình. Thay vào đó: giành việc dưới danh nghĩa `ket_worker`, rồi mới
-- `SET LOCAL ROLE ds_<mã>_app` + đặt `ket.branch_ids` cho **thân** job.
--
-- `NOINHERIT` cùng lý do với `ket_app`: nó là thành viên của mọi `ds_*_app` để
-- chuyển vai trò được, và kế thừa tự động sẽ xóa sạch ranh giới vừa dựng.
ALTER ROLE ket_worker NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;

-- Tên database chỉ biết lúc chạy nên phải dựng câu lệnh động. Dùng
-- `quote_ident` + nối chuỗi chứ KHÔNG dùng `format()` với đặc tả identifier:
-- psycopg quét cả tệp này (kể cả phần chú thích) và coi dấu phần trăm là
-- placeholder tham số, nên nó từ chối chạy. `quote_ident` chống tiêm y hệt.
DO $$
BEGIN
    EXECUTE 'GRANT CONNECT, CREATE ON DATABASE '
            || quote_ident(current_database()) || ' TO ket_owner';
    EXECUTE 'GRANT CONNECT ON DATABASE '
            || quote_ident(current_database()) || ' TO ket_app';
    EXECUTE 'GRANT CONNECT ON DATABASE '
            || quote_ident(current_database()) || ' TO ket_worker';
END
$$;

-- Schema điều khiển: `ket_owner` tạo bảng ở đó, `ket_app` chỉ dùng.
-- Quyền trên từng bảng do migration/bootstrap cấp — xem `security/grants.py`.
--
-- Cấp USAGE cho `ket_app` (đường trước khi chọn dataset: đăng nhập, liệt kê dữ
-- liệu kế toán) **và** cho `ket_control` (đường sau `SET ROLE`, vai trò dataset
-- kế thừa từ nhóm này). `ket_app` NOINHERIT nên hai lệnh này không thừa nhau:
-- nó không lấy được USAGE qua đường nhóm.
GRANT USAGE ON SCHEMA public TO ket_app;
GRANT USAGE ON SCHEMA public TO ket_control;
GRANT USAGE, CREATE ON SCHEMA public TO ket_owner;

-- Worker cần `public` để đọc sổ đăng ký dữ liệu kế toán (`datasets`) — nó phải
-- biết có những schema nào để quét hàng đợi. Quyền trên từng bảng điều khiển do
-- `bootstrap.worker_login_table_grants` cấp và danh sách đó dừng ở SELECT.
GRANT USAGE ON SCHEMA public TO ket_worker;

-- Tạo dữ liệu kế toán mới = tạo thêm một vai trò `ds_<mã>_app`, và việc đó chạy
-- bằng `ket_owner` (xem `datasets/provisioning.py`). Vì vậy owner cần
-- `CREATEROLE`.
--
-- Đây KHÔNG phải nới quyền thực chất: `ket_owner` đã sở hữu toàn bộ schema và
-- dữ liệu, nên "leo" sang quyền của `ket_app` không cho nó thêm gì. `CREATEROLE`
-- vẫn **không** cấp được `SUPERUSER` hay `BYPASSRLS` — chỉ superuser làm được
-- hai việc đó, nên hai cơ chế bảo vệ trung tâm (nhật ký bất biến, RLS) không bị
-- chạm tới. Vai trò runtime `ket_app` thì tuyệt đối không có `CREATEROLE`.
ALTER ROLE ket_owner CREATEROLE;

-- Owner phải là quản trị của nhóm `ket_control` thì mới `GRANT ket_control TO
-- ds_<mã>_app` được lúc tạo dataset. Owner kế thừa luôn quyền của nhóm — vô
-- hại, nó vốn đã sở hữu chính những bảng đó.
GRANT ket_control TO ket_owner WITH ADMIN OPTION;

-- PostgreSQL 15 trở xuống cấp sẵn CREATE trên `public` cho PUBLIC. Trong một
-- cụm dùng chung, bất kỳ vai trò nào cũng tạo được bảng cạnh bảng điều khiển.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Quyền tạo bảng TẠM cũng mặc định thuộc PUBLIC, và nó đủ để vô hiệu hóa nhật
-- ký bất biến mà không cần sửa hay xóa dòng nào: `pg_temp` được tìm TRƯỚC mọi
-- schema khác, nên một `CREATE TEMP TABLE audit_log (…)` khiến các dòng nhật ký
-- tiếp theo rơi vào bảng tạm rồi biến mất khi phiên kết thúc.
-- `persistence/session.py` đã nêu `pg_temp` ở cuối `search_path` (phòng thủ lớp
-- một); lệnh này bỏ hẳn khả năng tạo bảng tạm của vai trò runtime (lớp hai).
DO $$
BEGIN
    EXECUTE 'REVOKE TEMPORARY ON DATABASE '
            || quote_ident(current_database()) || ' FROM PUBLIC';
    EXECUTE 'REVOKE TEMPORARY ON DATABASE '
            || quote_ident(current_database()) || ' FROM ket_app';
    -- Cùng thủ thuật che bảng áp được cho worker: nó chạy thân job dưới vai trò
    -- dataset, nên một `CREATE TEMP TABLE audit_log (…)` trong tiến trình worker
    -- vô hiệu hóa nhật ký y hệt như trong tiến trình API.
    EXECUTE 'REVOKE TEMPORARY ON DATABASE '
            || quote_ident(current_database()) || ' FROM ket_worker';
END
$$;
