"""Cô lập dữ liệu kế toán bằng **quyền**, không chỉ bằng `search_path` (D3, OQ#12).

`SET LOCAL search_path` chọn schema nào được tìm khi tên bảng viết trần. Nó
**không** cấm gì cả: một câu truy vấn ghi rõ `ds_beta.gl_postings` vẫn chạy nếu
vai trò đang dùng có quyền trên schema đó. Chừng nào mọi dataset dùng chung một
vai trò runtime, ranh giới giữa hai doanh nghiệp chỉ là một quy ước đặt tên.

Cơ chế ở đây đặt ranh giới đó xuống tầng quyền của PostgreSQL:

```
ket_app        LOGIN, NOINHERIT       -- không có USAGE trên schema dataset nào
ket_control    NOLOGIN (nhóm)         -- quyền trên bảng schema điều khiển
ds_<mã>_app    NOLOGIN, INHERIT       -- thành viên ket_control; giữ quyền bảng của schema mình
```

`GRANT ds_<mã>_app TO ket_app` để app **chuyển được** vai trò; `NOINHERIT` để nó
**không tự động có** quyền của các vai trò đó. Hai vế phải đi cùng nhau: thiếu vế
đầu thì `SET ROLE` bị từ chối, thiếu vế sau thì `ket_app` lại nhìn thấy mọi
dataset và toàn bộ cơ chế thành vô nghĩa.

`SET LOCAL ROLE` hết hiệu lực khi transaction kết thúc, cùng vòng đời với
`search_path` và GUC chi nhánh — điều kiện để nhiều người dùng dùng chung một
connection pool.

**Hướng hỏng khi quên gọi:** không đọc được **bảng dataset** nào (fail-closed),
giống hệt khi quên đặt GUC chi nhánh. Bảng điều khiển thì vẫn đọc được: `ket_app`
giữ quyền thẳng trên `public.users` cho luồng đăng nhập, theo thiết kế.

## Cơ chế này KHÔNG chặn được gì (đo trên PG 16.15 — đọc trước khi dựa vào nó)

Tiêm SQL **nhiều câu lệnh** vẫn xuyên sang dataset khác: `SET ROLE` xét tư cách
thành viên của `session_user` (`ket_app`), không xét vai trò đang có hiệu lực, mà
`ket_app` buộc phải là thành viên của mọi `ds_*_app`. Từ phiên đã bind `ds_alpha`,
`"; SET ROLE ds_beta_app; SELECT … FROM ds_beta.branches"` đọc **và ghi** được.

Cái đã mua được là chặn tiêm **một câu lệnh** — kể cả dạng
`set_config('role', …)` lồng trong subquery/CTE — vì PostgreSQL kiểm quyền schema
lúc lập kế hoạch, trước khi hàm nào chạy.

Hệ quả bắt buộc: mọi `text()` chạy chuỗi có phần do người dùng ảnh hưởng **phải**
kèm tham số ràng buộc; psycopg khi đó dùng extended protocol, và protocol đó
không cho nhiều câu lệnh trong một lần gửi. Đường siết triệt để (vai trò đăng
nhập riêng + pool riêng mỗi dataset) ghi ở `docs/adr/adr-017-schema-per-dataset.md`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from ket.kernel.datasets.naming import role_name_for_schema, validate_schema_name
from ket.kernel.security.grants import (
    APP_ROLE,
    APPEND_ONLY_TABLES,
    OWNER_ROLE,
    READ_ONLY_TABLES,
    grant_append_only,
    grant_read_only,
)

WORKER_ROLE: Final[str] = "ket_worker"
"""Vai trò **đăng nhập** của tiến trình chạy tác vụ nền (D2/F2).

Quyền của nó trong một schema dataset dừng ở đúng bảng `jobs`; mọi thứ khác
worker chạm được là nhờ `SET LOCAL ROLE ds_<mã>_app` sau khi đã giành việc — tức
là dưới đúng bộ quyền và đúng RLS mà một request HTTP phải đi qua."""

WORKER_JOB_TABLE: Final[str] = "jobs"
"""Bảng duy nhất `ket_worker` chạm được bằng danh nghĩa của chính nó.

Hằng số chứ không chuỗi rời: hai đường cấp quyền (migration lúc tạo bảng, đường
sửa chữa `ensure_cluster`) phải đọc **cùng một** tên, và bài học của
`APPEND_ONLY_TABLES` là hai bản chép sẽ trôi."""

CONTROL_GROUP_ROLE: Final[str] = "ket_control"
"""Vai trò nhóm giữ quyền trên bảng schema điều khiển.

Tồn tại vì `ket_app` là `NOINHERIT`: sau `SET ROLE ds_<mã>_app`, `current_user`
không còn là `ket_app` nên quyền cấp thẳng cho `ket_app` không còn hiệu lực.
Cấp cho nhóm, cho vai trò dataset kế thừa — thay vì cấp lại cho từng vai trò mỗi
lần thêm bảng điều khiển, thứ chắc chắn sẽ bị quên ở lần thứ ba."""


def create_dataset_role_statements(schema: str) -> tuple[str, ...]:
    """DDL tạo vai trò cho một schema dataset. Chạy bằng `ket_owner`, chạy lại được.

    Chỉ tạo và cấp; **thu hồi** quyền cũ của `ket_app` nằm ở
    `revoke_legacy_login_role_grants` và chỉ chạy trên schema đã có bảng (xem
    `datasets.bootstrap.ensure_dataset_roles`). Tách hai việc vì hàm này còn
    chạy trên schema vừa `CREATE SCHEMA`, lúc chưa có bảng nào để thu hồi.

    **Điều kiện chạy lại được:** vai trò phải do chính `ket_owner` tạo. PostgreSQL
    16 đòi ADMIN OPTION mới `ALTER`/`GRANT` được một vai trò, mà `ket_owner` chỉ
    có ADMIN với vai trò do nó tạo — vai trò do superuser dựng (khôi phục
    `pg_dumpall --globals-only`, cài lại owner) sẽ làm các lệnh dưới đây báo
    *permission denied to alter role*. `provisioning` kiểm điều kiện đó trước và
    báo lỗi rõ thay vì để lỗi PostgreSQL thô nổi lên.
    """
    validate_schema_name(schema)
    role = role_name_for_schema(schema)
    return (
        # `CREATE ROLE` không có `IF NOT EXISTS`, nên bọc trong khối điều kiện.
        #
        # S608 (ghép chuỗi vào SQL): tên vai trò là **identifier**, không tham số
        # hóa được — cùng ngoại lệ đã có với tên schema. `role_name_for_schema`
        # đi qua `validate_schema_name`, tức whitelist `[a-z_][a-z0-9_]*` + giới
        # hạn độ dài, nên không có ký tự nào thoát ra được khỏi identifier.
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN INHERIT;
            END IF;
        END
        $$
        """,  # noqa: S608
        # Chạy lại được: đưa vai trò đã tồn tại từ lần trước về đúng hình dạng.
        #
        # Chỉ nêu hai thuộc tính mà cơ chế này thật sự dựa vào — không đăng nhập
        # được, và kế thừa được nhóm `ket_control`. Cố tình **không** nêu
        # `NOSUPERUSER`/`NOBYPASSRLS`/`NOCREATEDB`: PostgreSQL bắt người chạy
        # phải **có** thuộc tính đó mới được đổi nó, kể cả khi chỉ tắt đi (PG16:
        # "Only roles with the CREATEDB attribute may change the CREATEDB
        # attribute"), nên `ket_owner` sẽ bị từ chối và việc tạo dữ liệu kế toán
        # hỏng ở đúng nơi cài đặt. Không mất mát gì: mặc định của `CREATE ROLE`
        # đã là tắt cả bốn, và `test_dataset_role_is_unprivileged_and_cannot_log_in`
        # kiểm **kết quả** trong `pg_roles` chứ không tin vào câu lệnh.
        f"ALTER ROLE {role} NOLOGIN INHERIT",
        f"GRANT {CONTROL_GROUP_ROLE} TO {role}",
        f"GRANT {role} TO {APP_ROLE}",
        # Worker cũng phải `SET ROLE` được sang vai trò dataset — không phải để
        # giành việc (việc đó làm dưới danh nghĩa `ket_worker`, xem `roles.sql`)
        # mà để chạy **thân** job dưới đúng bộ quyền và đúng RLS của dataset.
        # `ket_worker` là `NOINHERIT` nên tư cách thành viên này không tự cho nó
        # quyền gì; nó chỉ mở đường chuyển vai trò tường minh.
        f"GRANT {role} TO {WORKER_ROLE}",
        # Owner cũng phải là thành viên để `SET ROLE` được: đường kiểm phiên bản
        # schema lúc khởi động dùng **một** hàm cho cả hai engine, và một hàm
        # rẽ nhánh theo vai trò đang đăng nhập là chỗ để lọt lỗi. Không mất mát
        # bảo mật nào — owner vốn sở hữu toàn bộ schema.
        f"GRANT {role} TO {OWNER_ROLE}",
        f'GRANT USAGE ON SCHEMA "{schema}" TO {role}',
        # `USAGE` cho worker phải cấp **ở đây**, cùng lúc với việc tạo schema —
        # không phải trong migration cạnh `GRANT … ON jobs`. Quyền trên bảng mà
        # thiếu `USAGE` trên schema cho ra lỗi *relation "jobs" does not exist*,
        # một thông điệp không nhắc gì tới quyền và đủ để mất hàng giờ.
        f'GRANT USAGE ON SCHEMA "{schema}" TO {WORKER_ROLE}',
    )


WORKER_WRITABLE_COLUMNS: Final[tuple[str, ...]] = (
    "status",
    "progress",
    "message",
    "result",
    "started_at",
    "finished_at",
    "heartbeat_at",
    "lease_expires_at",
    "attempt",
)
"""Cột mà một lượt chạy được phép ghi về **chính nó** (H4).

Nguồn sự thật duy nhất cho quyền `UPDATE` theo cột của `ket_worker`. Thêm cột cơ
chế mới vào `jobs` thì thêm ở đây; thêm một cột **mô tả việc** (loại job, tham
số, người yêu cầu, chi nhánh) thì **không** — đó chính là ranh giới danh sách
này dựng lên."""


def worker_table_grants(schema: str | None = None) -> tuple[str, ...]:
    """Quyền của `ket_worker` trong **một** schema dataset (F2).

    Đúng hai thứ, và danh sách này là nơi duy nhất khai chúng:

    * `USAGE` trên schema — không có thì không nhìn thấy bảng nào, kể cả bảng
      được cấp quyền.
    * `SELECT` + `UPDATE` **theo cột** trên `jobs` — giành việc (`SELECT … FOR
      UPDATE SKIP LOCKED` rồi đổi trạng thái), báo tiến độ, gia hạn lease, ghi
      kết quả. Danh sách cột: `WORKER_WRITABLE_COLUMNS`.

    Cố ý **không** `INSERT`: xếp hàng là việc của người dùng qua API, và một
    worker tự sinh job cho chính mình là vòng lặp không ai duyệt. Cũng không
    `DELETE`: dòng job là lịch sử "đã chạy gì lúc nào".

    `schema=None` cho migration (đã có `search_path` đúng), tên đủ điều kiện cho
    đường sửa chữa chạy ngoài request — cùng khuôn với `grants._qualify`.
    """
    if schema is None:
        qualified = WORKER_JOB_TABLE
        schema_grant: tuple[str, ...] = ()
    else:
        validate_schema_name(schema)
        qualified = f'"{schema}".{WORKER_JOB_TABLE}'
        schema_grant = (f'GRANT USAGE ON SCHEMA "{schema}" TO {WORKER_ROLE}',)
    return (
        *schema_grant,
        f"REVOKE ALL ON {qualified} FROM {WORKER_ROLE}",
        f"GRANT SELECT ON {qualified} TO {WORKER_ROLE}",
        # `UPDATE` **theo cột**, không phải toàn bảng. Đo được trước khi siết:
        # với `GRANT UPDATE` trần, một câu lệnh dưới danh nghĩa `ket_worker` đổi
        # được `type`, `params`, `requested_by`, `branch_id` của một job đang
        # chờ — và worker không kiểm lại quyền lúc chạy, nên dòng job bị viết
        # lại sẽ chạy như thật, kể cả loại job cần kết nối đặc quyền. Danh sách
        # dưới đây là **cột cơ chế**: thứ một lượt chạy được phép ghi về chính
        # nó. Mọi cột mô tả *việc phải làm* (`type`, `params`) và *ai yêu cầu*
        # (`requested_by`, `branch_id`, `cancel_requested`) nằm ngoài tầm với.
        f"GRANT UPDATE ({', '.join(WORKER_WRITABLE_COLUMNS)}) ON {qualified} TO {WORKER_ROLE}",
    )


def revoke_worker_grants(schema: str) -> tuple[str, ...]:
    """Thu hồi mọi quyền `ket_worker` từng có trong một schema dataset.

    Chạy ở đường sửa chữa ngay **trước** khi cấp lại, cùng lý do với
    `control_group_revokes`: danh sách trong mã phải là toàn bộ quyền của vai
    trò, không phải phần thêm vào những gì cụm đang có. Một bảng mà phiên bản
    trước lỡ cấp cho worker sẽ nằm lại mãi nếu không có bước này.
    """
    validate_schema_name(schema)
    return (
        f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM {WORKER_ROLE}',
        f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "{schema}" FROM {WORKER_ROLE}',
    )


def repair_dataset_privileges_statements(
    schema: str, *, present_tables: Collection[str]
) -> tuple[str, ...]:
    """Cấp lại toàn bộ quyền bảng cho vai trò của một schema **đã có dữ liệu**.

    Dùng cho đường sửa chữa (`datasets.bootstrap.ensure_dataset_roles`), không
    dùng lúc provision — lúc đó migration cấp quyền ngay cạnh từng `create_table`,
    cách duy nhất không bỏ sót bảng của migration sau.

    Ở đây `ON ALL TABLES` là đúng vì tình huống ngược lại: schema đã đủ bảng, thứ
    thiếu là vai trò. Nhưng nó cấp **đồng loạt** read-write, nên ngay sau đó phải
    hạ lại đúng những bảng không được phép read-write — và danh sách đó đọc từ
    `grants.APPEND_ONLY_TABLES` / `grants.READ_ONLY_TABLES`, **cùng nguồn** với
    migration.

    Đây từng là một lỗi thật, không phải phòng xa: danh sách chép làm hai bản, nên
    một bảng chỉ-thêm thêm ở phase sau bị `ensure_cluster` nâng lên read-write mà
    không test nào đỏ — nhật ký bất biến bị vô hiệu bởi chính đường sửa chữa.
    Gọi lại `grant_append_only`/`grant_read_only` thay vì viết tay câu lệnh giữ
    cho hai đường không thể lệch nhau nữa.

    `present_tables` là các bảng **thật sự có** trong schema (người gọi đọc từ
    `pg_class`). Cần vì hàm chạy cho mọi dataset đã đăng ký, mà một schema đang
    khôi phục dở — hoặc một schema tạo trước khi bảng đó ra đời — sẽ làm câu lệnh
    hạ quyền đổ với "relation does not exist" và kéo theo cả vòng lặp sửa chữa.
    """
    validate_schema_name(schema)
    role = role_name_for_schema(schema)
    statements: list[str] = [
        f'GRANT USAGE ON SCHEMA "{schema}" TO {role}',
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" TO {role}',
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO {role}',
    ]
    for table in sorted(APPEND_ONLY_TABLES & set(present_tables)):
        statements.extend(grant_append_only(table, grantee=role, schema=schema))
    for table in sorted(READ_ONLY_TABLES & set(present_tables)):
        statements.extend(grant_read_only(table, grantee=role, schema=schema))
    # Quyền của worker cũng phải dựng lại ở đây: `ON ALL TABLES` phía trên chỉ
    # cấp cho vai trò dataset, còn `ket_worker` là vai trò riêng — một cụm vừa
    # khôi phục sẽ có đủ bảng, đủ vai trò, mà tiến trình worker vẫn không giành
    # được việc nào và không có gì báo ngoài dòng log "permission denied".
    statements.extend(revoke_worker_grants(schema))
    if WORKER_JOB_TABLE in present_tables:
        statements.extend(worker_table_grants(schema))
    return tuple(statements)


def revoke_legacy_login_role_grants(schema: str) -> tuple[str, ...]:
    """Thu hồi quyền mà **phiên bản trước D3** đã cấp thẳng cho `ket_app`.

    Cần vì hai đường:

    * Cụm đã tạo dữ liệu kế toán bằng mã trước D3 giữ nguyên `GRANT … TO ket_app`
      ở tầng **bảng** — migration `0001` sửa tại chỗ nên không có `0002` nào gỡ
      chúng ra. Trên cụm đó, `ket_app` trần vẫn đọc được sổ của mọi dataset và
      toàn bộ D3 không có tác dụng, trong khi mọi test đều xanh (test chạy trên
      cụm dựng mới).
    * Ai đó cấp tay để gỡ rối rồi quên thu hồi.

    `ON ALL TABLES/SEQUENCES` an toàn ở đây — khác với lúc **cấp** quyền, nơi nó
    bỏ sót bảng của migration sau: thu hồi sót thì lần chạy sau thu hồi tiếp,
    còn cấp sót thì hỏng âm thầm.
    """
    validate_schema_name(schema)
    return (
        f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM {APP_ROLE}',
        f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "{schema}" FROM {APP_ROLE}',
        f'REVOKE ALL ON SCHEMA "{schema}" FROM {APP_ROLE}',
    )


def drop_dataset_role_statements(schema: str) -> tuple[str, ...]:
    """DDL xóa vai trò của một dataset. Gọi **sau** khi `DROP SCHEMA` đã xong.

    `DROP OWNED BY` trước `DROP ROLE`: vai trò không sở hữu bảng nào (bảng thuộc
    `ket_owner`) nhưng vẫn còn bản ghi quyền trên các đối tượng khác, và
    `DROP ROLE` từ chối chừng nào còn bản ghi đó.
    """
    validate_schema_name(schema)
    role = role_name_for_schema(schema)
    return (
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                EXECUTE 'DROP OWNED BY {role}';
                EXECUTE 'DROP ROLE {role}';
            END IF;
        END
        $$
        """,  # noqa: S608 — identifier đã qua whitelist, xem chú thích ở hàm trên
    )


def set_local_role_statement(schema: str) -> str:
    """`SET LOCAL ROLE` cho transaction đang mở.

    Đặt **trước** `search_path`: hai lệnh độc lập về mặt kỹ thuật, nhưng thứ tự
    "quyền trước, tầm nhìn sau" giữ cho mọi câu lệnh của transaction — kể cả
    lệnh đầu tiên — chạy dưới đúng vai trò dataset.
    """
    return f"SET LOCAL ROLE {role_name_for_schema(schema)}"
