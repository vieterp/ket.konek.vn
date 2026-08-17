"""Tài khoản ngân hàng của đối tác — thao tác trên bảng con (FR-SYS-033).

Dịch vụ riêng chứ không nhét vào `MasterDataService`: bảng này không phải danh
mục (không cây, không mã duy nhất theo chi nhánh, không "ngừng theo dõi thay cho
xóa" như FR-SYS-012 mô tả), và nới dịch vụ dùng chung cho một hình dạng khác đi
là đúng lối "bẻ cong khung dùng chung" mà §Risk của phase 3 cảnh báo.

Dịch vụ **không** tự mở transaction — nhận `Session` của request, cùng hợp đồng
với mọi dịch vụ kernel khác.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.errors import MasterDataNotFoundError
from ket.kernel.master_data.models.partner_bank_account import (
    PARTNER_BANK_ACCOUNT_TABLE_NAME,
    PartnerBankAccount,
)
from ket.kernel.persistence.versioning import require_row_version


class PartnerBankAccountMergeHook:
    """Chuẩn bị bảng con trước khi gộp hai đối tác, và dọn sau khi gộp (FR-SYS-016).

    Vì sao cần: `merge_service` chuyển khóa ngoại bằng một câu `UPDATE` vô danh,
    còn bảng này mang **hai** ràng buộc duy nhất theo `partner_id`. Không có bước
    chuẩn bị thì gộp đổ ngay ở ca thường gặp nhất của chính yêu cầu sinh ra nó —
    hai bản ghi trùng thì thường **cả hai** đều đã khai tài khoản ngân hàng, và
    thông điệp người dùng nhận được là tên một chỉ mục nội bộ (review lát này,
    H1).

    Hai luật, và cả hai đều diễn đạt cùng một ý "hai bản ghi này là một":

    * **Cờ mặc định**: bản ghi được giữ lại là bản quyết định, nên mặc định của
      nó thắng. Tài khoản của nguồn chuyển sang dưới dạng tài khoản thường.
    * **Trùng số tài khoản ở cùng ngân hàng**: đó đúng là **một** tài khoản có
      thật được khai hai lần — bỏ dòng của nguồn, không tạo thêm bản sao. Xóa đi
      qua ORM nên có vết trong `audit_log`.

    Trùng số tài khoản mà **khác** ngân hàng thì không bỏ: hai tài khoản khác
    nhau tình cờ cùng số. Trường hợp đó ràng buộc duy nhất sẽ chặn, và
    `merge_service` dịch nó thành một câu nêu việc phải làm.
    """

    def before_move(self, session: Session, *, source_id: int, target_id: int) -> None:
        target_accounts = {
            (account.bank_id, account.account_number): account
            for account in session.execute(
                select(PartnerBankAccount).where(PartnerBankAccount.partner_id == target_id)
            )
            .scalars()
            .all()
        }
        source_accounts = (
            session.execute(
                select(PartnerBankAccount).where(PartnerBankAccount.partner_id == source_id)
            )
            .scalars()
            .all()
        )
        for account in source_accounts:
            if (account.bank_id, account.account_number) in target_accounts:
                session.delete(account)
                continue
            account.is_default = False
        session.flush()

    def after_move(self, session: Session, *, target_id: int) -> None:
        PartnerBankAccountService(session).ensure_one_default(target_id)


class PartnerBankAccountService:
    """Thêm, sửa, xóa tài khoản ngân hàng của một đối tác."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def entity_type(self) -> str:
        return PARTNER_BANK_ACCOUNT_TABLE_NAME

    def list_for(
        self, partner_id: int, *, include_inactive: bool = False
    ) -> Sequence[PartnerBankAccount]:
        """Tài khoản của một đối tác — mặc định trước, rồi tới số tài khoản.

        Ẩn tài khoản đã đóng theo mặc định: đây là nguồn dựng ô chọn trên ủy
        nhiệm chi, và một tài khoản đã đóng hiện ở đó thì việc đóng nó chẳng có
        tác dụng gì. Màn hình quản lý gọi kèm `include_inactive=true`.
        """
        statement = (
            select(PartnerBankAccount)
            .where(PartnerBankAccount.partner_id == partner_id)
            .order_by(PartnerBankAccount.is_default.desc(), PartnerBankAccount.account_number)
        )
        if not include_inactive:
            statement = statement.where(PartnerBankAccount.is_active)
        return self._session.execute(statement).scalars().all()

    def get(self, account_id: int, *, partner_id: int) -> PartnerBankAccount:
        """Một tài khoản, **kèm** điều kiện nó thuộc đúng đối tác đang mở.

        `partner_id` là tham số bắt buộc chứ không tùy chọn: đường dẫn HTTP mang
        cả hai id, và không đối chiếu chúng thì `/partners/7/bank-accounts/91`
        trả về tài khoản của đối tác 12 — một đường đọc vòng qua mọi phép kiểm
        đặt ở đối tác chủ.
        """
        account = self._session.get(PartnerBankAccount, account_id)
        if account is None or account.partner_id != partner_id:
            raise MasterDataNotFoundError(
                "Không tìm thấy tài khoản ngân hàng của đối tác",
                entity_type=self.entity_type,
                entity_id=account_id,
                partner_id=partner_id,
            )
        return account

    def add(
        self,
        *,
        partner_id: int,
        bank_id: int,
        account_number: str,
        account_holder: str,
        bank_branch: str | None = None,
        is_default: bool = False,
    ) -> PartnerBankAccount:
        """Thêm một tài khoản.

        Tài khoản **đầu tiên** của một đối tác luôn thành mặc định, dù người gọi
        không yêu cầu: một đối tác có đúng một tài khoản mà không tài khoản nào
        được đánh dấu là chỗ để ủy nhiệm chi ở phase 6 phải hỏi lại một câu chỉ
        có một câu trả lời.
        """
        if is_default:
            self._clear_default(partner_id)

        account = PartnerBankAccount(
            partner_id=partner_id,
            bank_id=bank_id,
            account_number=account_number,
            account_holder=account_holder,
            bank_branch=bank_branch,
            is_default=is_default,
        )
        self._session.add(account)
        self._session.flush()
        # Tài khoản đầu tiên thành mặc định **qua** bất biến chung chứ không qua
        # một nhánh `if` riêng ở đây: "còn tài khoản hoạt động thì phải có đúng
        # một mặc định" là một luật, và một luật cài ở hai chỗ là hai chỗ để
        # chúng lệch nhau — đúng chỗ review lát này bắt được (H2), khi `add` giữ
        # luật còn `delete` thì không.
        self.ensure_one_default(partner_id)
        return account

    def update(
        self,
        account_id: int,
        *,
        partner_id: int,
        expected_row_version: int,
        bank_id: int,
        account_number: str,
        account_holder: str,
        bank_branch: str | None,
        is_default: bool,
        is_active: bool,
    ) -> PartnerBankAccount:
        """Sửa một tài khoản — nhận **trọn** giá trị mới, không phải phần chênh lệch.

        Không có quy ước "`None` = giữ nguyên" như `MasterDataService.update`, và
        khác biệt đó là có lý do: `bank_branch` được phép **rỗng**, nên `None` ở
        đây phải nghĩa là "xóa trắng ô này". Một tham số vừa mang nghĩa "không
        đụng tới" vừa mang nghĩa "đặt thành rỗng" là một ô người dùng không bao
        giờ xóa được, và không có gì báo cho họ biết vì sao.

        Hai luật ràng nhau, và cả hai đều được giải **tại đây** thay vì để người
        dùng tự xoay:

        * Đặt tài khoản này làm mặc định thì tài khoản mặc định cũ thôi mặc định
          — nếu không, chỉ mục duy nhất có điều kiện sẽ từ chối lượt ghi và
          người dùng nhận một lỗi ràng buộc thay vì kết quả họ vừa yêu cầu.
        * Đóng một tài khoản đang là mặc định thì **bỏ luôn** cờ mặc định. Giữ
          lại cờ đó nghĩa là ủy nhiệm chi kỳ sau lấy sẵn một tài khoản đã đóng —
          sai lặng lẽ, và chỉ lộ ra khi ngân hàng trả lệnh về.
        """
        account = self.get(account_id, partner_id=partner_id)
        require_row_version(
            current=account.row_version,
            expected=expected_row_version,
            entity=self.entity_type,
        )
        account.bank_id = bank_id
        account.account_number = account_number
        account.account_holder = account_holder
        account.bank_branch = bank_branch
        account.is_active = is_active

        if is_default and is_active:
            self._clear_default(partner_id, except_id=account_id)
        account.is_default = is_default and is_active

        self._session.flush()
        self.ensure_one_default(partner_id)
        return account

    def delete(self, account_id: int, *, partner_id: int) -> None:
        """Xóa hẳn một tài khoản.

        Xóa thật chứ không "ngừng theo dõi": ở phase 6 một ủy nhiệm chi **chép**
        số tài khoản vào chứng từ của nó (số đó phải in đúng như lúc lập, kể cả
        sau này đối tác đổi tài khoản), nên dòng ở đây không phải thứ chứng từ cũ
        trỏ tới. Ai muốn giữ vết thì dùng `is_active = false`.
        """
        self._session.delete(self.get(account_id, partner_id=partner_id))
        self._session.flush()
        self.ensure_one_default(partner_id)

    def ensure_one_default(self, partner_id: int) -> None:
        """Còn tài khoản hoạt động thì phải có **đúng một** mặc định (FR-SYS-033).

        Chỉ mục có điều kiện dưới DB giữ vế "không quá một". Vế "ít nhất một" thì
        không ràng buộc nào diễn đạt được, nên nó phải là một bước ở đây — và
        phải chạy sau **mọi** đường ghi, không riêng đường thêm mới. Review lát
        này bắt đúng chỗ đó: xóa hay đóng tài khoản đang là mặc định thì đối tác
        rơi về trạng thái "có tài khoản nhưng không cái nào được chọn sẵn", tức
        ủy nhiệm chi ở phase 6 lại phải hỏi một câu chỉ có một câu trả lời.

        Đôn tài khoản có **số nhỏ nhất** lên, không phải "cái mới nhất": người
        dùng nhìn thấy danh sách sắp theo số tài khoản, nên quy tắc này đọc được
        từ chính màn hình. Không còn tài khoản hoạt động nào thì thôi — đối tác
        chỉ còn tài khoản đã đóng thì không có gì để chọn sẵn.
        """
        active = (
            self._session.execute(
                select(PartnerBankAccount)
                .where(PartnerBankAccount.partner_id == partner_id)
                .where(PartnerBankAccount.is_active)
                .order_by(PartnerBankAccount.account_number)
            )
            .scalars()
            .all()
        )
        if not active or any(account.is_default for account in active):
            return
        active[0].is_default = True
        self._session.flush()

    def _clear_default(self, partner_id: int, *, except_id: int | None = None) -> None:
        """Bỏ cờ mặc định của tài khoản đang giữ nó.

        Đi qua ORM chứ không một câu `UPDATE` thẳng: dòng bị đổi phải có vết
        trong `audit_log` (FR-NFR-012), và listener nhật ký chỉ thấy thay đổi đi
        qua session. Số dòng ở đây tối đa là một, nên cái giá bằng không.
        """
        statement = select(PartnerBankAccount).where(
            PartnerBankAccount.partner_id == partner_id, PartnerBankAccount.is_default
        )
        for current in self._session.execute(statement).scalars().all():
            if except_id is not None and current.id == except_id:
                continue
            current.is_default = False
        self._session.flush()
