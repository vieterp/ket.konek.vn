"""Đường ghi duy nhất vào `ar_ap_ledger` — bản cài `ArApSubledger` (ADR-021).

Bảng sổ phụ này là dữ liệu suy ra được, và thứ giữ cho nó không lệch sổ cái là
**số cửa ghi bằng một**. Mọi lượt sinh/gỡ dòng đi qua đây; module `purchase` và
`sales` gọi qua Protocol ở kernel chứ không import module này (C3).

Ba đường vào, ba luật khác nhau:

* `record` — chứng từ mua/bán vừa ghi sổ. **Thay trọn theo `voucher_id`**: ghi
  sổ → bỏ ghi sổ → sửa → ghi sổ lại là đường thường ngày, cộng dồn thì lượt
  thứ hai nhân đôi công nợ và chỉ lộ ra ở số dư 131/331 nhiều kỳ sau.
* `remove` — chứng từ bỏ ghi sổ. Từ chối khi có dòng đã bị đối trừ: xóa nó là
  bỏ lại phiếu thu/chi trỏ vào hư không.
* `apply`/`revert` (trong `settlement_source`) — phiếu thu/chi cộng/trừ số đã
  đối trừ, khóa dòng trước khi đọc số dư (RT-16).

**Đối trừ vượt trần chống bằng ba lớp**, không phải một: `FOR UPDATE` xếp hai
phiếu đồng thời thành hàng, phép kiểm `settled + delta <= amount` trả 422 cho
người dùng, và `CHECK (settled <= amount)` ở DB biến mọi đường lọt còn lại
thành lỗi ồn ào thay vì một khoản nợ âm.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ket.kernel.auditing.listener import record_action
from ket.kernel.auditing.models import AuditAction
from ket.kernel.errors import PostingValidationError, PostingViolation
from ket.kernel.protocols import SubledgerEntry
from ket.modules.receivables.models import ArApLedgerEntry
from ket.posting.documents.models import Voucher

SUBLEDGER_SETTLED_CODE = "receivables.entry_already_settled"
"""Bỏ ghi sổ / xóa một chứng từ mà khoản nợ của nó đã được trả một phần."""


class ArApLedgerService:
    """Bản cài `ArApSubledger`, cộng phần đọc dùng chung trong module.

    Nhận `Session` đang mở và **không tự commit** — cùng luật với mọi service
    nghiệp vụ khác: `record`/`remove` chạy trong chính transaction ghi sổ của
    chứng từ gốc, nên sổ phụ và bút toán hoặc cùng vào hoặc cùng không.
    """

    def record(
        self, session: Session, *, voucher_id: UUID, entries: Sequence[SubledgerEntry]
    ) -> None:
        # Chi nhánh VÀ trạng thái đọc từ CHỨNG TỪ, không nhận từ người gọi.
        # Sổ phụ phải đối chiếu được với `gl_postings`, mà bên ấy `branch_id`
        # luôn là `vouchers.branch_id` (posting engine không có chiều chi nhánh
        # theo dòng). Một lượt nạp phục vụ cả hai luật.
        voucher = self._voucher_of(session, voucher_id)

        # Ghi đè một khoản đã có người trả vào là làm mất dấu lượt trả đó.
        # Đường đúng: bỏ ghi sổ trước (bị chặn vì cùng lý do), gỡ phiếu thu
        # ra, rồi mới sửa chứng từ.
        self.remove_guard(session, voucher_id=voucher_id)

        branch_id = voucher.branch_id
        self._delete_rows(session, voucher_id, branch_id=branch_id)
        for entry in entries:
            session.add(
                ArApLedgerEntry(
                    target_kind=entry.target_kind.value,
                    partner_kind=entry.partner_kind.value,
                    partner_id=entry.partner_id,
                    branch_id=branch_id,
                    ledger=entry.ledger,
                    account_id=entry.account_id,
                    document_id=voucher_id,
                    document_no=entry.document_no,
                    document_date=entry.document_date,
                    due_date=entry.due_date,
                    currency_code=entry.currency_code,
                    exchange_rate=entry.exchange_rate,
                    amount_fc=entry.amount_fc,
                    amount=entry.amount,
                    description=entry.description,
                )
            )
        session.flush()

    def remove(self, session: Session, *, voucher_id: UUID) -> None:
        # Nạp chứng từ trước cả guard: gọi `remove` dưới một phạm vi KHÔNG thấy
        # chứng từ thì guard thấy 0 dòng và cho qua, `DELETE` xóa 0 dòng và
        # báo thành công — chiều nguy hiểm (bỏ lại dòng sổ phụ) lại đúng là
        # chiều không được canh. Nạp trước biến nó thành lỗi ồn ào.
        voucher = self._voucher_of(session, voucher_id)
        self.remove_guard(session, voucher_id=voucher_id)
        self._delete_rows(session, voucher_id, branch_id=voucher.branch_id)
        session.flush()

    def _voucher_of(self, session: Session, voucher_id: UUID) -> Voucher:
        """Chứng từ gốc của khoản công nợ — nguồn của chi nhánh.

        KHÔNG kiểm `status` ở đây (quyết định user 2026-09-01). Bất biến "dòng
        sổ phụ chỉ tồn tại khi chứng từ đã ghi sổ" là **nghĩa vụ của 7B/7C**
        qua `after_post`/`after_unpost`, đúng như ADR-021 §Decision 5 viết. Ép
        ở đây buộc mọi bài test sổ phụ phải ghi sổ một chứng từ thật, và điều
        đó cột chúng vào trạng thái KHÓA KỲ — thứ mà tệp test khác đổi được
        trên dataset dùng chung. Đổi một lỗ có thật lấy một bộ test giòn là
        phép đổi tồi.

        `None` nghĩa là người gọi không thấy chứng từ (RLS) hoặc nó không tồn
        tại. Ghi/gỡ tiếp là làm mù, nên dừng ồn ào: đây là lỗi lập trình của
        phân hệ gọi, không phải dữ liệu người dùng nhập sai.
        """
        voucher = session.execute(
            select(Voucher).where(Voucher.id == voucher_id)
        ).scalar_one_or_none()
        if voucher is None:
            raise RuntimeError(f"Không tìm thấy chứng từ {voucher_id} cho sổ phụ công nợ")
        return voucher

    def _delete_rows(self, session: Session, voucher_id: UUID, *, branch_id: int) -> None:
        """Xóa hàng loạt + tự ghi vết.

        `delete()` hàng loạt KHÔNG nạp đối tượng vào session nên listener của
        `Audited` không thấy nó (`kernel/auditing/listener.py` nêu đúng giới
        hạn này) — bảng có vết lúc SINH mà không có vết lúc XÓA là nửa nhật
        ký, và nửa mất đi đúng là nửa kiểm toán viên cần khi 131/331 lệch.
        Cùng lối `opening_balances/clear_job.py`.
        """
        result = session.execute(
            delete(ArApLedgerEntry).where(ArApLedgerEntry.document_id == voucher_id)
        )
        deleted = cast("CursorResult[Any]", result).rowcount
        if deleted:
            record_action(
                session,
                entity_type=ArApLedgerEntry.__tablename__,
                entity_id=str(voucher_id),
                action=AuditAction.DELETED,
                # Chi nhánh của DÒNG BỊ XÓA, không phải chi nhánh đang thao
                # tác: `audit_log` có RLS, nên nộp nhầm phạm vi là giấu vết xóa
                # khỏi đúng kiểm toán viên của chi nhánh ấy — nửa nhật ký, đúng
                # thứ lượt ghi vết này sinh ra để vá.
                branch_id=branch_id,
                new_values={"deleted_rows": deleted, "voucher_id": str(voucher_id)},
            )

    def remove_guard(self, session: Session, *, voucher_id: UUID) -> None:
        """Phần KIỂM của `remove`, tách ra để bộ guard dùng chung gọi lại.

        Tách chứ không chép: hai bản kiểm sẽ lệch nhau ở lần sửa luật đầu
        tiên, và bản lệch nằm ở đường ít người đi hơn.
        """
        existing = self._entries_of(session, voucher_id)
        settled_rows = [row for row in existing if row.settled_fc > 0 or row.settled > 0]
        if settled_rows:
            raise self._settled_error(voucher_id, settled_rows)

    def _entries_of(self, session: Session, voucher_id: UUID) -> Sequence[ArApLedgerEntry]:
        """Khóa luôn các dòng của chứng từ (`FOR UPDATE`).

        Không khóa thì phép kiểm "đã có ai trả chưa" đọc một ảnh cũ: một phiếu
        thu đang đối trừ dở có thể commit ngay sau lượt đọc, và lượt bỏ ghi sổ
        vẫn xóa mất dòng nó vừa trả vào.
        """
        return tuple(
            session.execute(
                select(ArApLedgerEntry)
                .where(ArApLedgerEntry.document_id == voucher_id)
                .with_for_update()
            )
            .scalars()
            .all()
        )

    def _settled_error(
        self, voucher_id: UUID, settled_rows: Sequence[ArApLedgerEntry]
    ) -> PostingValidationError:
        return PostingValidationError(
            "Khoản công nợ của chứng từ này đã được thanh toán một phần",
            violations=[
                PostingViolation(
                    SUBLEDGER_SETTLED_CODE,
                    "Gỡ phiếu thu/chi đã đối trừ vào chứng từ này trước, rồi mới sửa "
                    "hoặc bỏ ghi sổ nó",
                    voucher_id=str(voucher_id),
                    document_no=row.document_no,
                    settled_fc=str(row.settled_fc),
                )
                for row in settled_rows
            ],
        )


SERVICE = ArApLedgerService()


def ensure_not_settled(session: Session, *, voucher_id: UUID) -> None:
    """Ném khi chứng từ có khoản công nợ đã được đối trừ một phần.

    Đăng ký vào `REFERENCE_GUARDS` (xem `__init__`) nên nó chạy trước **mọi
    lượt bỏ ghi sổ**, không riêng lượt nào nhớ gọi `remove`. Bộ guard ấy KHÔNG
    chạy ở đường xóa chứng từ — xem lập luận trong `__init__`.
    """
    SERVICE.remove_guard(session, voucher_id=voucher_id)
