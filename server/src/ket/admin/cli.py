"""Lệnh quản trị chạy tại máy chủ: `python -m ket.admin <lệnh>`.

Vì sao tồn tại: trước lát này, quy trình khôi phục cụm là một **đoạn Python chép
trong `docs/deployment-guide.md`**. Tài liệu không được kiểm bởi test nào, nên
nó sẽ trôi khỏi mã — và nó trôi đúng vào lúc người ta cần nó nhất, giữa một lần
khôi phục sau sự cố. Một điểm vào có thật thì `mypy`, `ruff` và test canh được.

Những việc chỉ làm được **tại máy chủ**, không qua HTTP, và đó là chủ đích:

* `ensure-cluster` — dựng/nâng cấp bảng điều khiển + vai trò từng dataset + đồng
  bộ mã quyền và vai trò `admin` cho mọi dataset. Chạy bằng `ket_owner`; API
  không bao giờ được mang quyền đó.
* `create-user` / `reset-password` / `reset-totp` — đường phá-kính. Tài khoản
  **đầu tiên** phải tạo được khi chưa có ai đăng nhập; một quản trị viên tự khóa
  mình ra ngoài — quên mật khẩu, hoặc mất điện thoại sinh mã 2FA — phải mở lại
  được mà không cần một tài khoản khác. Ai chạm được máy chủ thì đã chạm được
  DB, nên đây không phải một lỗ hổng mới, mà là thừa nhận thực tế.
* `grant-role` / `grant-branch` — cùng lý do phá-kính: chưa ai có
  `system.role.edit` thì không ai gán được vai trò qua HTTP, và một dữ liệu kế
  toán vừa tạo là cái hộp không ai mở được.
* `generate-app-key` — ghi khóa mã hóa vào OS keystore (ADR-019).

`argparse` chứ không Typer/Click: bảy lệnh không đáng thêm một phụ thuộc, và
`ket.admin` phải chạy được trong bản đóng gói PyInstaller (S4) nơi mỗi phụ thuộc
là một thứ phải kiểm lại.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from ket.kernel.datasets.bootstrap import ensure_cluster
from ket.kernel.datasets.service import resolve_dataset
from ket.kernel.errors import DomainError
from ket.kernel.persistence.session import control_session, create_session_factory
from ket.kernel.security import account_service, auth_service, role_service
from ket.kernel.security.keystore import generate_app_key, store_app_key
from ket.settings import Settings, get_settings

# S105 báo nhầm: chuỗi nhắc nhập, không phải mật khẩu.
PASSWORD_PROMPT = "Mật khẩu mới: "  # noqa: S105
PASSWORD_CONFIRM_PROMPT = "Nhập lại: "  # noqa: S105


def _read_password(from_stdin: bool) -> str:
    """Mật khẩu từ stdin (script) hoặc hỏi hai lần không hiện (người gõ tay).

    `--password-stdin` tồn tại để installer và script cài đặt không phải nhét
    mật khẩu vào **tham số dòng lệnh** — tham số nằm trong `ps` và trong lịch sử
    shell, hai chỗ ai cũng đọc được.
    """
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(PASSWORD_PROMPT)
    if first != getpass.getpass(PASSWORD_CONFIRM_PROMPT):
        raise SystemExit("Hai lần nhập không khớp.")
    return first


def _owner_engine(settings: Settings) -> Engine:
    """Kết nối đặc quyền `ket_owner` — DDL, cấp quyền, tạo dataset."""
    return create_engine(settings.owner_database_url)


def _app_engine(settings: Settings) -> Engine:
    """Kết nối runtime `ket_app`.

    Lệnh tài khoản dùng vai trò **runtime** chứ không phải owner, có chủ đích:
    nó chứng minh rằng đường tạo/đặt lại tài khoản chạy được với đúng bộ quyền
    mà app server có. Nếu một ngày nó cần owner mới chạy được, đó là dấu hiệu
    quyền đã bị nới ở đâu đó.
    """
    return create_engine(settings.database_url)


def command_ensure_cluster(_args: argparse.Namespace, settings: Settings) -> None:
    """Dựng/nâng cấp bảng điều khiển và vai trò DB của mọi dataset. Chạy lại được."""
    engine = _owner_engine(settings)
    try:
        ensure_cluster(engine)
    finally:
        engine.dispose()
    print("Đã dựng/cập nhật bảng điều khiển và vai trò dataset.")  # noqa: T201


def command_create_user(args: argparse.Namespace, settings: Settings) -> None:
    """Tạo danh tính đăng nhập toàn cục."""
    password = _read_password(args.password_stdin)
    engine = _app_engine(settings)
    try:
        factory = create_session_factory(engine)
        with control_session(factory) as session:
            user = account_service.create_user(
                session,
                username=args.username,
                password=password,
                email=args.email,
                must_change_password=not args.no_force_change,
                client_info="ket.admin",
            )
            user_id = user.id
    finally:
        engine.dispose()
    print(f"Đã tạo người dùng {args.username} (id={user_id}).")  # noqa: T201


def command_reset_password(args: argparse.Namespace, settings: Settings) -> None:
    """Đặt lại mật khẩu mà không cần mật khẩu cũ (FR-SYS-075, đường offline).

    **Thu hồi luôn mọi phiên đang mở.** Lệnh này được gọi đúng vào lúc nghi tài
    khoản bị chiếm; để token cũ sống thêm 12 giờ nữa thì việc đặt lại mật khẩu
    không đuổi được ai. Đường HTTP `change-password` cũng thu hồi — hai đường
    cùng nghĩa phải hành xử như nhau.
    """
    password = _read_password(args.password_stdin)
    engine = _app_engine(settings)
    try:
        factory = create_session_factory(engine)
        with control_session(factory) as session:
            user = account_service.find_user(session, args.username)
            account_service.reset_password(
                session, user=user, new_password=password, client_info="ket.admin"
            )
            revoked = auth_service.revoke_other_sessions(
                session,
                user_id=user.id,
                keep_session_id=None,
                actor_user_id=user.id,
                client_info="ket.admin",
            )
    finally:
        engine.dispose()
    print(  # noqa: T201
        f"Đã đặt lại mật khẩu cho {args.username} và thu hồi {revoked} phiên đang mở. "
        "Người dùng phải đổi mật khẩu ở lần đăng nhập tới."
    )


def command_reset_totp(args: argparse.Namespace, settings: Settings) -> None:
    """Gỡ xác thực hai lớp của một người dùng — đường thoát khi mất thiết bị sinh mã.

    Xóa cả bí mật lẫn **cờ bắt buộc**, và cờ mới là phần quan trọng: một tài
    khoản có `totp_required` mà chưa đăng ký thiết bị thì không đăng nhập được,
    mà không đăng nhập được thì cũng không gọi được `/auth/totp/enroll` để đăng
    ký — tự khóa mình ra ngoài, không có đường quay lại qua HTTP.

    Lát 2B-1b đặt lại cờ này khi đồng bộ vai trò, nên đây là đường **tạm** cho
    người dùng đăng ký lại thiết bị, không phải cách gỡ 2FA vĩnh viễn.
    """
    engine = _app_engine(settings)
    try:
        factory = create_session_factory(engine)
        with control_session(factory) as session:
            user = account_service.find_user(session, args.username)
            account_service.disable_totp(session, user=user, client_info="ket.admin")
            user.totp_required = False
    finally:
        engine.dispose()
    print(  # noqa: T201
        f"Đã gỡ xác thực hai lớp của {args.username}. Người dùng đăng nhập bằng mật khẩu "
        "rồi đăng ký lại thiết bị sinh mã."
    )


def _resolve_actor_and_target(
    session: Session, *, username: str, actor_username: str | None
) -> tuple[int, int]:
    """`(user_id, actor_user_id)` cho một lệnh phân quyền chạy tại máy chủ.

    Thao tác qua HTTP luôn có người bấm nút; lệnh tại máy chủ thì không. Mặc
    định ghi chính người bị tác động làm người thực hiện — vì tài khoản **đầu
    tiên** của một bản cài được cấp quyền khi chưa có ai khác tồn tại — còn
    `--actor` để quản trị viên đã có tài khoản ghi đúng tên mình. Cả hai đường
    đều kèm `client_info="ket.admin"`, và đó mới là thứ phân biệt "chạy tại máy
    chủ" với "bấm trên màn hình".
    """
    user = account_service.find_user(session, username)
    if actor_username is None:
        return user.id, user.id
    return user.id, account_service.find_user(session, actor_username).id


def command_grant_role(args: argparse.Namespace, settings: Settings) -> None:
    """Gán vai trò cho người dùng trong một dữ liệu kế toán (FR-SYS-071).

    Đường phá-kính cho **lần đầu**: chưa ai có `system.role.edit` thì không ai
    gán được vai trò qua HTTP, và dữ liệu kế toán vừa tạo là cái hộp không ai
    mở được. Vai trò `admin` do provisioning gieo sẵn.
    """
    engine = _app_engine(settings)
    try:
        factory = create_session_factory(engine)
        dataset = resolve_dataset(engine, args.dataset)
        with control_session(factory) as session:
            user_id, actor_user_id = _resolve_actor_and_target(
                session, username=args.username, actor_username=args.actor
            )
        changed = role_service.grant_role(
            factory,
            dataset_schema=dataset.schema_name,
            user_id=user_id,
            role_code=args.role,
            actor_user_id=actor_user_id,
            client_info="ket.admin",
        )
    finally:
        engine.dispose()
    if changed:
        print(f"Đã gán vai trò {args.role} cho {args.username} tại {args.dataset}.")  # noqa: T201
    else:
        print(f"{args.username} đã có vai trò {args.role} tại {args.dataset}.")  # noqa: T201


def command_grant_branch(args: argparse.Namespace, settings: Settings) -> None:
    """Cho người dùng thấy một chi nhánh (FR-SYS-072).

    Không gán chi nhánh nào = **không thấy dòng nào** có `branch_id`, chứ không
    phải "thấy tất". Đó là hướng hỏng đã chọn cho toàn bộ cơ chế RLS, nên lệnh
    này là bước bắt buộc sau khi tạo tài khoản, không phải tùy chọn.

    `actor_branch_ids=None`: đường HTTP chỉ gán được chi nhánh mà chính người
    thực hiện đang thấy (chống tự nới phạm vi), nhưng lúc dựng bản cài thì chưa
    ai thấy chi nhánh nào — đòi hỏi đó sẽ khóa mọi người ra ngoài. Đây là lý do
    lệnh tồn tại, và nó chỉ chạy được bởi người đã chạm được máy chủ.
    """
    engine = _app_engine(settings)
    try:
        factory = create_session_factory(engine)
        dataset = resolve_dataset(engine, args.dataset)
        with control_session(factory) as session:
            user_id, actor_user_id = _resolve_actor_and_target(
                session, username=args.username, actor_username=args.actor
            )
        changed = role_service.assign_branch(
            factory,
            dataset_schema=dataset.schema_name,
            user_id=user_id,
            branch_code=args.branch,
            actor_user_id=actor_user_id,
            actor_branch_ids=None,
        )
    finally:
        engine.dispose()
    if changed:
        print(f"Đã gán chi nhánh {args.branch} cho {args.username} tại {args.dataset}.")  # noqa: T201
    else:
        print(f"{args.username} đã thấy chi nhánh {args.branch} tại {args.dataset}.")  # noqa: T201


def command_generate_app_key(_args: argparse.Namespace, settings: Settings) -> None:
    """Sinh khóa mã hóa và ghi vào OS keystore (ADR-019).

    **Ghi đè khóa cũ nếu có** — và đó là thao tác một chiều: mọi `totp_secret`
    đã mã hóa bằng khóa cũ sẽ không mở lại được. Cảnh báo in ra chứ không hỏi
    lại, vì lệnh này cũng chạy từ installer nơi không có ai trả lời.
    """
    key = generate_app_key()
    store_app_key(key, service=settings.keyring_service)
    print(  # noqa: T201
        f"Đã ghi khóa mã hóa vào OS keystore (service={settings.keyring_service}).\n"
        "Nếu trước đó đã có khóa: bí mật mã hóa bằng khóa cũ không mở lại được — "
        "người dùng đã bật 2FA phải đăng ký lại thiết bị sinh mã."
    )


def build_parser() -> argparse.ArgumentParser:
    """Bộ phân tích tham số. Tách khỏi `main` để test gọi thẳng được."""
    parser = argparse.ArgumentParser(
        prog="python -m ket.admin",
        description="Lệnh quản trị Konek Két chạy tại máy chủ.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser(
        "ensure-cluster",
        help="Dựng/nâng cấp bảng điều khiển và vai trò DB của mọi dataset",
    )
    ensure.set_defaults(handler=command_ensure_cluster)

    create = subparsers.add_parser("create-user", help="Tạo danh tính đăng nhập")
    create.add_argument("username")
    create.add_argument("--email", default=None)
    create.add_argument(
        "--password-stdin",
        action="store_true",
        help="Đọc mật khẩu từ stdin thay vì hỏi (dùng cho script cài đặt)",
    )
    create.add_argument(
        "--no-force-change",
        action="store_true",
        help="Không bắt đổi mật khẩu ở lần đăng nhập đầu (chỉ dùng cho tài khoản test)",
    )
    create.set_defaults(handler=command_create_user)

    reset = subparsers.add_parser(
        "reset-password", help="Đặt lại mật khẩu người dùng và thu hồi mọi phiên đang mở"
    )
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true")
    reset.set_defaults(handler=command_reset_password)

    reset_totp = subparsers.add_parser(
        "reset-totp", help="Gỡ xác thực hai lớp (mất thiết bị sinh mã) để đăng ký lại"
    )
    reset_totp.add_argument("username")
    reset_totp.set_defaults(handler=command_reset_totp)

    grant_role = subparsers.add_parser(
        "grant-role", help="Gán vai trò cho người dùng trong một dữ liệu kế toán"
    )
    grant_role.add_argument("username")
    grant_role.add_argument("--dataset", required=True, help="Mã dữ liệu kế toán")
    grant_role.add_argument("--role", required=True, help="Mã vai trò, ví dụ 'admin'")
    grant_role.add_argument("--actor", default=None, help="Người thực hiện (mặc định: chính họ)")
    grant_role.set_defaults(handler=command_grant_role)

    grant_branch = subparsers.add_parser(
        "grant-branch", help="Cho người dùng thấy một chi nhánh trong dữ liệu kế toán"
    )
    grant_branch.add_argument("username")
    grant_branch.add_argument("--dataset", required=True, help="Mã dữ liệu kế toán")
    grant_branch.add_argument("--branch", required=True, help="Mã chi nhánh")
    grant_branch.add_argument("--actor", default=None, help="Người thực hiện (mặc định: chính họ)")
    grant_branch.set_defaults(handler=command_grant_branch)

    app_key = subparsers.add_parser(
        "generate-app-key", help="Sinh khóa mã hóa ứng dụng và ghi vào OS keystore"
    )
    app_key.set_defaults(handler=command_generate_app_key)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Điểm vào. Trả mã thoát: 0 thành công, 1 lỗi nghiệp vụ.

    `DomainError` in thành một dòng chứ không traceback: người đọc là quản trị
    viên máy chủ, và "Tên đăng nhập đã tồn tại" hữu ích hơn 30 dòng stack.
    """
    args = build_parser().parse_args(argv)
    settings = get_settings()
    try:
        args.handler(args, settings)
    except DomainError as exc:
        print(f"Lỗi [{exc.error_code}]: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    return 0
