-- Hai vai trò DB của Konek (RT-02, RT-06) — chạy MỘT LẦN cho mỗi cụm PostgreSQL,
-- bởi superuser, trước khi tạo dữ liệu kế toán đầu tiên.
--
--   konek_owner : sở hữu schema/bảng, chạy DDL + migration. Sở hữu `audit_log`.
--   konek_app   : vai trò runtime của app server. KHÔNG sở hữu bảng nào.
--
-- Vì sao phải tách: `REVOKE UPDATE ON audit_log` chỉ có tác dụng lên vai trò
-- **không sở hữu** bảng. Nếu app server đăng nhập bằng chính chủ sở hữu thì nó
-- tự bỏ qua REVOKE của mình và "nhật ký bất biến" chỉ là lời hứa suông. Cùng
-- lý do đó, `konek_app` không được `BYPASSRLS`, nếu không cô lập chi nhánh
-- (RT-04) cũng vô hiệu.
--
-- MẬT KHẨU: cố ý không đặt ở đây. Script này nằm trong repo; mật khẩu thật do
-- installer sinh và cất ở OS keystore (ADR-019, phase 11), rồi đặt bằng
-- `ALTER ROLE ... PASSWORD`. Trên máy lập trình dùng `trust`/`peer` cục bộ nên
-- vai trò không mật khẩu vẫn đăng nhập được.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'konek_owner') THEN
        CREATE ROLE konek_owner LOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'konek_app') THEN
        CREATE ROLE konek_app LOGIN
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;
    END IF;
END
$$;

-- Chạy lại được: hạ đặc quyền kể cả khi vai trò đã tồn tại từ trước với cấu
-- hình khác (máy lập trình, bản cài nâng cấp từ phiên bản cũ).
ALTER ROLE konek_owner NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE konek_app   NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

-- Tên database chỉ biết lúc chạy nên phải dựng câu lệnh động. Dùng
-- `quote_ident` + nối chuỗi chứ KHÔNG dùng `format()` với đặc tả identifier:
-- psycopg quét cả tệp này (kể cả phần chú thích) và coi dấu phần trăm là
-- placeholder tham số, nên nó từ chối chạy. `quote_ident` chống tiêm y hệt.
DO $$
BEGIN
    EXECUTE 'GRANT CONNECT, CREATE ON DATABASE '
            || quote_ident(current_database()) || ' TO konek_owner';
    EXECUTE 'GRANT CONNECT ON DATABASE '
            || quote_ident(current_database()) || ' TO konek_app';
END
$$;

-- Schema điều khiển: `konek_owner` tạo bảng ở đó, `konek_app` chỉ dùng.
-- Quyền trên từng bảng do migration/bootstrap cấp — xem `security/grants.py`.
GRANT USAGE ON SCHEMA public TO konek_app;
GRANT USAGE, CREATE ON SCHEMA public TO konek_owner;

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
            || quote_ident(current_database()) || ' FROM konek_app';
END
$$;
