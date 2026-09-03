"""Biến một lần chờ khóa vô hạn thành một câu trả lời tức thì.

Lượt CI 33658137670 chết ở trần 30 phút. Job không chạy chậm — nó **đứng hẳn**:
28 phút không thêm một dòng tiến độ nào. `faulthandler_timeout=300` nêu đúng
khung treo, `seeding.bind_seed_schema` đang chờ
`SELECT pg_advisory_xact_lock(hashtext(:key))`, nghĩa là một phiên khác giữ
đúng khóa ấy trong một transaction chưa đóng. Khóa cấp-transaction chỉ nhả khi
transaction kết thúc, nên đó là chờ **vô hạn**.

Không tái hiện được cục bộ (lượt chạy đúng lệnh CI: 596,7s, exit 0, 0 lần
`faulthandler` nổ), và không do nhánh nào gây ra — đây là đua có sẵn, chỉ nổ
trên runner chậm. Vì vậy module này **không đoán bản sửa**. Nó làm một việc
khiêm tốn hơn và chắc chắn có giá trị: lần sau nó nổ, log phải nêu đích danh
phiên đang chặn thay vì im lặng 28 phút rồi bị giết.

Hai nửa, cố ý tách rời:

1. `apply_lock_timeout` — đặt `lock_timeout` ở phạm vi DATABASE test. Chờ khóa
   hỏng NHANH kèm SQLSTATE 55P03 thay vì treo tới trần job.
2. `describe_blocking_sessions` — khi (1) nổ, mở một connection MỚI và đổ
   `pg_stat_activity` + `pg_locks`. Phiên đang lỗi không làm được việc này:
   transaction của nó đã abort.
"""

from __future__ import annotations

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.pool import NullPool

LOCK_TIMEOUT = "60s"
"""Ngưỡng chờ khóa của mọi phiên nối vào database test.

Chọn 60s vì nó nằm gọn giữa hai mốc đã đo: lần chờ khóa hợp lệ dài nhất trong
bộ test là hàng chục **mili**giây (`assign_branch` phẳng ở 56ms), còn
`faulthandler_timeout` của pyproject là 300s. Sáu mươi giây vừa rộng gấp ba
bậc so với mọi tranh chấp thật, vừa nổ sớm hơn hẳn faulthandler — nên khi cả
hai cùng có thể nổ, cái nổ trước là cái mang thông điệp cụ thể hơn.

**Không hạ xuống vài giây để "bắt sớm hơn".** Runner CI chậm có thật (PR #3:
một checkpoint 214,5s), và một ngưỡng sát sạt sẽ biến độ trễ hạ tầng thành
test đỏ giả — đúng loại nhiễu làm người ta mất niềm tin vào cổng.
"""

LOCK_TIMEOUT_SQLSTATE = "55P03"
"""`lock_not_available` — mã PostgreSQL trả khi `lock_timeout` hết giờ."""


def apply_lock_timeout(connection: Connection, database: str) -> None:
    """Đặt `lock_timeout` cho MỌI connection tới `database`, bất kể vai trò nào.

    Đặt ở phạm vi database chứ không ở từng `create_engine`: connection tới
    database test được tạo ở hàng chục chỗ, gồm cả mã **production**
    (`provision_dataset` tự dựng engine từ `Settings`). Vá từng nơi gọi sẽ sót,
    và sót đúng những đường ít ai nhớ — mà chính một trong số đó đã treo.

    Chỉ mã test gọi hàm này, nên bản cài khách hàng không thừa hưởng ngưỡng
    này: ở đó chờ khóa lâu có thể là hành vi đúng.
    """
    # `database` đến từ hằng số của conftest, không từ input người dùng; vẫn
    # trích dẫn để câu lệnh đúng với tên có ký tự đặc biệt.
    connection.exec_driver_sql(f"ALTER DATABASE \"{database}\" SET lock_timeout = '{LOCK_TIMEOUT}'")


def is_lock_timeout(error: BaseException | None) -> bool:
    """Ngoại lệ này có phải do `lock_timeout` hết giờ không?

    Đi hết chuỗi `__cause__`/`__context__` vì SQLAlchemy bọc lỗi của psycopg
    trong `OperationalError`, và pytest lại có thể bọc thêm một tầng nữa. So
    theo SQLSTATE chứ không theo văn bản thông điệp: thông điệp đổi theo
    ngôn ngữ (`lc_messages`) của cụm, SQLSTATE thì không.
    """
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if getattr(error, "sqlstate", None) == LOCK_TIMEOUT_SQLSTATE:
            return True
        # `.orig` trước `__cause__`: hôm nay SQLAlchemy đặt cả hai, nhưng một
        # `raise … from None` ở bất kỳ lớp bọc nào sẽ cắt đứt chuỗi ngoại lệ
        # trong khi `.orig` vẫn giữ nguyên lỗi psycopg mang SQLSTATE.
        error = getattr(error, "orig", None) or error.__cause__ or error.__context__
    return False


_BLOCKING_SESSIONS_SQL = """
SELECT a.pid,
       a.usename,
       a.state,
       pg_blocking_pids(a.pid) AS blocked_by,
       a.wait_event_type,
       a.wait_event,
       date_trunc('second', now() - a.state_change) AS in_state,
       date_trunc('second', now() - a.xact_start)   AS xact_age,
       left(regexp_replace(a.query, '\\s+', ' ', 'g'), 160) AS query
FROM pg_stat_activity a
WHERE a.datname = :database
ORDER BY a.xact_start NULLS LAST, a.pid
"""

_ADVISORY_LOCKS_SQL = """
SELECT l.pid, l.objid, l.granted
FROM pg_locks l
JOIN pg_database d ON d.oid = l.database
WHERE l.locktype = 'advisory' AND d.datname = :database
ORDER BY l.granted, l.pid
"""


def describe_blocking_sessions(admin_dsn: str, database: str) -> str:
    """Đổ trạng thái mọi phiên trên `database` thành văn bản đọc được trong log.

    Mở connection MỚI qua `admin_dsn` (superuser, database `postgres`): phiên
    vừa dính `lock_timeout` có transaction đã abort, nó không chạy thêm được
    câu lệnh nào.

    Chữ ký cần tìm trong bản đổ này là một phiên `idle in transaction` với
    `xact_age` lớn đang giữ một khóa cố vấn `granted = true` — đó là phía không
    bao giờ nhả, và `blocked_by` của phiên đang chờ sẽ trỏ thẳng vào pid của nó.

    **Không bao giờ ném ngoại lệ.** Hàm này chạy trong đường xử lý lỗi; một lỗi
    ở đây sẽ che mất chính lỗi mà nó đang giải thích.
    """
    try:
        # `connect_timeout`: ngưỡng `SET lock_timeout` bên dưới chỉ canh lần chờ
        # KHÓA. Nếu cụm kẹt chứ không phải khóa bị giữ, chính lời `connect()`
        # này sẽ đứng theo timeout TCP của hệ điều hành — tức bộ chẩn đoán treo
        # trong đường xử lý lỗi, đúng thứ nó sinh ra để ngăn.
        engine = create_engine(
            admin_dsn, poolclass=NullPool, connect_args={"connect_timeout": 5}
        ).execution_options(isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                # Ngưỡng riêng cho bản đổ: nếu đến lượt nó cũng chờ khóa thì
                # bỏ cuộc ngay, đừng biến chẩn đoán thành lần treo thứ hai.
                connection.exec_driver_sql("SET lock_timeout = '5s'")
                sessions = (
                    connection.execute(text(_BLOCKING_SESSIONS_SQL), {"database": database})
                    .mappings()
                    .all()
                )
                advisory = (
                    connection.execute(text(_ADVISORY_LOCKS_SQL), {"database": database})
                    .mappings()
                    .all()
                )
        finally:
            engine.dispose()
    except Exception as error:  # xem docstring: hàm này không được ném
        return f"Không đổ được trạng thái khóa từ {admin_dsn}: {error!r}"

    if not sessions:
        # Không phải công cụ hỏng. Bản đổ chạy sau khi thân bài đã tháo, nên nếu
        # phía giữ khóa là một connection do chính bài ấy mở thì nó đã đóng theo
        # — và lời giải nằm ở stack trace phía trên, không ở đây. Bản đổ này chỉ
        # có gì để nói khi phía giữ khóa SỐNG LÂU HƠN bài, đúng như lượt CI treo.
        return (
            f"Chờ khóa quá hạn `lock_timeout` trên database `{database}`, nhưng lúc đổ trạng "
            "thái thì không còn phiên nào nối vào nó. Phía giữ khóa đã đóng cùng bài test — "
            "đọc stack trace phía trên; đây không phải cảnh treo do phiên khác giữ khóa."
        )

    lines = [
        f"Chờ khóa quá hạn `lock_timeout` (mặc định của database: {LOCK_TIMEOUT}) trên "
        f"`{database}`. Trạng thái phiên tại thời điểm hỏng — chữ ký cần tìm là một phiên "
        "`idle in transaction` giữ khóa `granted = True`:",
        "",
        f"pg_stat_activity ({len(sessions)} phiên):",
    ]
    lines.extend(f"  {dict(row)}" for row in sessions)
    lines.append("")
    lines.append(f"pg_locks locktype='advisory' ({len(advisory)} khóa):")
    lines.extend(f"  {dict(row)}" for row in advisory)
    return "\n".join(lines)
