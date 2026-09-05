"""Dịch vụ chứng từ nghiệp vụ khác (FR-GLE-001) — chứng từ thử của posting engine.

Nhiều dòng Nợ/Có tự do, đa tiền tệ, đủ chiều — đúng vai "bài kiểm tra kiến
trúc trước khi nhân bản ra 13 module" của phase 4. Mọi hàm nhận `Session` đang
mở và không tự commit; Cất-đồng-thời-ghi-sổ (FR-SYS-061) vì thế là **một**
transaction thật sự: cấp số, ghi dòng, ghi sổ cùng sống cùng chết.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.config.catalog import MONEY_SCALE_KEY, SAVE_ALSO_POSTS_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    PostingValidationError,
    PostingViolation,
    VoucherBranchImmutableError,
)
from ket.kernel.money import convert_currency
from ket.kernel.numbering.models import ResetRule
from ket.kernel.numbering.service import NumberingRule
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.protocols import PROVIDERS, ArApSubledger
from ket.modules.general_ledger.journal import JOURNAL_DOCUMENT_TYPE
from ket.modules.general_ledger.journal.models import JournalLine, JournalSettlement
from ket.modules.general_ledger.journal.posting_mapper import to_posting_request
from ket.modules.general_ledger.journal.schemas import JournalLineIn, JournalVoucherIn
from ket.modules.general_ledger.journal.settlement_service import (
    apply_settlements,
    line_ids_by_no,
    price_settlements,
    revert_settlements,
    stored_settlements,
    subledger_entries,
    write_settlements,
)
from ket.posting.contracts import (
    PostingService,
    Voucher,
    VoucherDraft,
    VoucherService,
)

JOURNAL_NUMBERING_RULE = NumberingRule(
    document_type=JOURNAL_DOCUMENT_TYPE, prefix="GLE{YY}-", reset_rule=ResetRule.YEARLY
)
"""Quy tắc đánh số mặc định — `GLE26-00001`, quay về 1 mỗi năm dương lịch.

Trả nợ 4D ở lát 5D: 4D phải hạ về `NEVER` vì dãy reset năm sẽ cấp lại
`GLE00001` vào tháng 1 năm sau và chết ở `uq_vouchers_type_branch_no` (không
có chiều năm). Nay token `{YY}` đưa năm vào CHÍNH số chứng từ (bung lúc tạo
dòng bộ đếm của chu kỳ — `kernel.numbering.service._define`), nên hai năm
không bao giờ ghép ra cùng một số và ràng buộc cũ giữ nguyên.

Vẫn là hằng mã nguồn: quy tắc đánh số thành dữ liệu cấu hình (FR-SYS-063) đi
cùng định nghĩa loại chứng từ của gói khi phase 6 nhân ra phiếu thu/chi —
chữ ký `create` đã nhận rule từ ngoài nên chỗ đổi khu trú ở đây."""


class JournalVoucherService:
    """CRUD + ghi sổ cho chứng từ nghiệp vụ khác, trong transaction người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._vouchers = VoucherService(session)
        self._posting = PostingService(session)

    def create(
        self, payload: JournalVoucherIn, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Cất; nếu tùy chọn FR-SYS-061 bật thì ghi sổ luôn trong cùng transaction.

        `acknowledged_warnings` đi tiếp vào lượt ghi sổ đi kèm đó (nợ 6A): từ
        khi guard đầu tiên đăng ký (CashBalanceGuard soi cả chứng từ GLE chạm
        111x), đường "Cất đồng thời ghi sổ" cũng phải mang được xác nhận của
        người dùng — không thì cảnh báo mức "Cảnh báo" chặn vĩnh viễn đường này.
        """
        self._verify_client_amounts(payload, user_id=user_id)
        voucher = self._vouchers.create(
            VoucherDraft(
                document_type=JOURNAL_DOCUMENT_TYPE,
                branch_id=payload.branch_id,
                document_date=payload.document_date,
                posting_date=payload.posting_date,
                currency_code=payload.currency_code,
                exchange_rate=payload.exchange_rate,
                description=payload.description,
                cashflow_activity=payload.cashflow_activity,
                entry_kind=payload.entry_kind,
            ),
            rule=JOURNAL_NUMBERING_RULE,
            user_id=user_id,
        )
        self._write_lines(voucher, payload)
        self._write_settlements(voucher, payload, user_id=user_id)

        save_also_posts = value_of(self._session, key=SAVE_ALSO_POSTS_KEY, user_id=user_id)
        if save_also_posts is True:
            self.post(voucher.id, user_id=user_id, acknowledged_warnings=acknowledged_warnings)
        return voucher

    def update(
        self,
        voucher_id: UUID,
        payload: JournalVoucherIn,
        *,
        expected_row_version: int,
        user_id: int,
    ) -> Voucher:
        """Sửa chứng từ Đã cất: thay trọn bộ dòng, tra lại kỳ nếu đổi ngày.

        Thay trọn bộ chứ không diff từng dòng: form gửi cả lưới, và một phép
        diff chỉ để giữ lại `id` dòng cũ sẽ phức tạp hơn chính dữ liệu nó giữ.
        Trạng thái và số chứng từ **không** đổi qua đường này.
        """
        voucher = self._vouchers.require(voucher_id)
        self._vouchers.ensure_editable(voucher)
        require_row_version(
            current=voucher.row_version,
            expected=expected_row_version,
            entity=Voucher.__tablename__,
        )
        self._verify_client_amounts(payload, user_id=user_id)

        # Số chứng từ thuộc dãy của chi nhánh — đổi chi nhánh phá tính duy nhất
        # hoặc chiếm chỗ số chưa cấp của chi nhánh đích (review 4A, H1).
        if payload.branch_id != voucher.branch_id:
            raise VoucherBranchImmutableError(
                "Chứng từ đã cất không đổi được chi nhánh — xóa rồi lập lại ở chi nhánh đúng",
                voucher_no=voucher.voucher_no,
                current_branch=voucher.branch_id,
                requested_branch=payload.branch_id,
            )
        voucher.document_date = payload.document_date
        if payload.posting_date != voucher.posting_date:
            self._vouchers.move_to_date(voucher, posting_date=payload.posting_date)
        voucher.currency_code = payload.currency_code
        voucher.exchange_rate = payload.exchange_rate
        voucher.description = payload.description
        voucher.cashflow_activity = payload.cashflow_activity
        # Sửa được vì chứng từ phải ở trạng thái Đã cất mới vào tới đây
        # (`ensure_editable`) — chưa có dòng `gl_postings` nào mang bản sao cờ,
        # nên không có gì để lệch. Đổi cờ trên chứng từ ĐÃ ghi sổ thì phải bỏ
        # ghi sổ trước, đúng đường của mọi sửa đổi khác.
        voucher.entry_kind = payload.entry_kind

        for line in self._lines_of(voucher.id):
            self._session.delete(line)
        self._session.flush()
        self._write_lines(voucher, payload)
        self._write_settlements(voucher, payload, user_id=user_id)
        return voucher

    def post(
        self, voucher_id: UUID, *, user_id: int, acknowledged_warnings: bool = False
    ) -> Voucher:
        """Ghi sổ + ghi sổ phụ công nợ + cộng số đã đối trừ, một transaction.

        Cùng mã với hook `after_post` trong registry — đường module và đường
        hành động chung không được cho hai kết quả khác nhau (cùng luật với
        `purchase`/`sales`).
        """
        lines = self._lines_of(voucher_id)
        voucher = self._posting.post(
            to_posting_request(self._session, voucher_id, lines),
            user_id=user_id,
            acknowledged_warnings=acknowledged_warnings,
        )
        self.sync_after_post(voucher_id, user_id=user_id)
        return voucher

    def unpost(self, voucher_id: UUID, *, user_id: int) -> Voucher:
        voucher = self._posting.unpost(voucher_id, user_id=user_id)
        self.clear_after_unpost(voucher_id)
        return voucher

    def delete(self, voucher_id: UUID) -> None:
        """Xóa chứng từ Đã cất — dòng chi tiết đi theo `ON DELETE CASCADE`."""
        self._vouchers.delete(voucher_id)

    def get(self, voucher_id: UUID) -> tuple[Voucher, list[JournalLine], list[JournalSettlement]]:
        voucher = self._vouchers.require(voucher_id)
        return (
            voucher,
            self._lines_of(voucher_id),
            list(stored_settlements(self._session, voucher_id)),
        )

    def sync_after_post(self, voucher_id: UUID, *, user_id: int) -> None:
        """Việc sau ghi sổ — hook `after_post`.

        Hai việc, theo đúng thứ tự ấy: ghi khoản nợ MỚI mà chứng từ sinh ra
        vào sổ phụ, rồi cộng số đã trả vào các khoản mà nó ghi GIẢM. Không có
        đường nào để dòng đối trừ trỏ vào chính khoản vừa sinh — đích được
        định giá lúc cất, khi những khoản ấy còn chưa tồn tại.
        """
        voucher = self._vouchers.require(voucher_id)
        lines = self._lines_of(voucher_id)
        # Gọi vô điều kiện, kể cả khi chứng từ không chạm công nợ: `record`
        # thay TRỌN theo `voucher_id`, nên nó cũng là lượt dọn dòng cũ của một
        # chứng từ vừa được sửa từ "có công nợ" thành "không". Bỏ qua khi rỗng
        # thì đúng những dòng ấy ở lại.
        self._require_subledger().record(
            self._session,
            voucher_id=voucher_id,
            entries=subledger_entries(
                self._session, voucher, lines, scale=self._money_scale(user_id)
            ),
        )
        apply_settlements(self._session, voucher_id=voucher_id)

    def clear_after_unpost(self, voucher_id: UUID) -> None:
        """Việc sau bỏ ghi sổ — hook `after_unpost`: gỡ đúng thứ đã ghi.

        Gỡ ngược thứ tự: trả lại số đã đối trừ TRƯỚC, rồi mới xóa dòng sổ phụ
        của chính chứng từ. Làm ngược lại thì guard "khoản đã có người trả thì
        không xóa" đọc trạng thái nửa vời.
        """
        revert_settlements(self._session, voucher_id=voucher_id)
        self._require_subledger().remove(self._session, voucher_id=voucher_id)

    def _require_subledger(self) -> ArApSubledger:
        """Cửa ghi sổ phụ công nợ — `receivables` cài, module này chỉ gọi (C3)."""
        subledger = PROVIDERS.ar_ap_subledger()
        if subledger is None:  # pragma: no cover - `model_registry` luôn nạp bản cài
            raise RuntimeError("Chưa có bản cài sổ phụ công nợ (ArApSubledger)")
        return subledger

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    def _write_settlements(
        self, voucher: Voucher, payload: JournalVoucherIn, *, user_id: int
    ) -> None:
        """Định giá rồi lưu dòng đối trừ — chạy SAU `_write_lines`.

        Sau chứ không trước: phép định giá đọc dòng đã lưu (xem
        `settlement_service.classify`), và dòng đối trừ nối vào `id` của chúng.
        """
        lines = self._lines_of(voucher.id)
        priced = price_settlements(
            self._session,
            lines=lines,
            settlements=payload.settlements,
            branch_id=payload.branch_id,
            scale=self._money_scale(user_id),
        )
        write_settlements(
            self._session,
            voucher_id=voucher.id,
            priced_by_line=priced,
            line_ids_by_no=line_ids_by_no(self._session, voucher.id),
        )

    def _lines_of(self, voucher_id: UUID) -> list[JournalLine]:
        return list(
            self._session.execute(
                select(JournalLine)
                .where(JournalLine.voucher_id == voucher_id)
                .order_by(JournalLine.line_no)
            )
            .scalars()
            .all()
        )

    def _write_lines(self, voucher: Voucher, payload: JournalVoucherIn) -> None:
        for index, line in enumerate(payload.lines, start=1):
            self._session.add(
                JournalLine(
                    voucher_id=voucher.id,
                    line_no=index,
                    account_id=line.account_id,
                    corresponding_account_id=line.corresponding_account_id,
                    currency_code=line.currency_code or payload.currency_code,
                    exchange_rate=(
                        line.exchange_rate
                        if line.exchange_rate is not None
                        else payload.exchange_rate
                    ),
                    debit_fc=line.debit_fc,
                    credit_fc=line.credit_fc,
                    partner_id=line.partner_id,
                    partner_kind=(
                        line.partner_kind.value if line.partner_kind is not None else None
                    ),
                    cost_object_id=line.cost_object_id,
                    project_id=line.project_id,
                    order_id=line.order_id,
                    contract_id=line.contract_id,
                    expense_item_id=line.expense_item_id,
                    item_id=line.item_id,
                    warehouse_id=line.warehouse_id,
                    bank_account_id=line.bank_account_id,
                    extended_dimensions=(
                        {str(value.dimension_id): value.value_id for value in line.extended} or None
                    ),
                    description=line.description,
                )
            )
        self._session.flush()

    def _verify_client_amounts(self, payload: JournalVoucherIn, *, user_id: int) -> None:
        """Validator #9 (phase-04): số quy đổi client gửi phải khớp `round_money`.

        Server luôn tự tính số ghi sổ; phép kiểm này tồn tại để một client tính
        theo luật làm tròn khác bị phát hiện **ngay lúc lưu**, chứ không phải
        lúc người dùng thấy màn hình hiện một số còn sổ mang số khác.
        """
        scale = self._money_scale(user_id)

        violations: list[PostingViolation] = []
        for index, line in enumerate(payload.lines, start=1):
            rate = line.exchange_rate if line.exchange_rate is not None else payload.exchange_rate
            for side, sent, fc_amount in (
                ("debit", line.debit, line.debit_fc),
                ("credit", line.credit, line.credit_fc),
            ):
                if sent is None:
                    continue
                expected = convert_currency(fc_amount, rate, scale)
                if sent != expected:
                    violations.append(
                        PostingViolation(
                            "posting.conversion_mismatch",
                            "Số quy đổi client gửi lệch với phép tính của server",
                            line_no=index,
                            side=side,
                            sent=str(sent),
                            expected=str(expected),
                        )
                    )
        if violations:
            raise PostingValidationError(
                "Số quy đổi trên chứng từ không khớp luật làm tròn của hệ thống",
                violations=violations,
            )


__all__ = [
    "JOURNAL_DOCUMENT_TYPE",
    "JOURNAL_NUMBERING_RULE",
    "JournalLineIn",
    "JournalVoucherService",
]
