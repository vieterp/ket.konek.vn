"""Cấp số chứng từ — không trùng, và với hóa đơn thì không đứt quãng.

FR-NFR-006 nói ngắn gọn: "số chứng từ/hóa đơn không trùng khi nhiều người thao
tác đồng thời". Cách sai kinh điển là `SELECT MAX(number) + 1`: hai người cùng
đọc 122, cả hai cùng ghi 123. Nó chạy đúng suốt buổi thử nghiệm một người dùng
và hỏng vào ngày thứ hai của tháng cao điểm.

Cách ở đây: bộ đếm là **một dòng** trong `number_sequences`, và cấp số là
`SELECT … FOR UPDATE` trên đúng dòng đó. PostgreSQL nối tiếp hai người vào cùng
một dòng; người thứ hai chờ tới khi người thứ nhất commit hoặc rollback, rồi đọc
giá trị đã cập nhật. Không có khe hở giữa lúc đọc và lúc ghi vì cả hai nằm trong
một khóa dòng.

Hệ quả có chủ đích: khóa được giữ tới **cuối transaction của người gọi**, nên
cấp số phải là bước gần cuối của việc ghi chứng từ, không phải bước đầu. Giữ
khóa qua một lượt gọi dịch vụ hóa đơn điện tử là cách làm cả phòng kế toán ngồi
chờ nhau.

**Rollback trả lại số.** Bộ đếm nhích lên trong chính transaction của người gọi,
nên chứng từ ghi hỏng thì số cũng lùi lại — đó là điều kiện để dãy không thủng
(BR-EIV-02). Đây cũng là lý do dịch vụ này **không** tự mở transaction: một
transaction riêng đã commit sẽ tiêu mất số của một chứng từ chưa từng tồn tại.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.errors import NumberSequenceNotFoundError
from ket.kernel.numbering.models import AllocatedNumber, NumberSequence, ResetRule

SCOPE_SEPARATOR = "|"
"""Ngăn cách các phần của `scope_key`. Ký tự này **không** xuất hiện trong mã
loại chứng từ hay mã chi nhánh, nên khóa phân tách ngược lại được khi cần chẩn
đoán — và hai phạm vi khác nhau không bao giờ ghép ra cùng một chuỗi."""


def _has_year_token(text: str) -> bool:
    return "{YYYY}" in text or "{YY}" in text


def _expand_year_tokens(text: str, *, on_date: date) -> str:
    """`{YYYY}`/`{YY}` → năm dương lịch của chu kỳ (docstring `ResetRule.YEARLY`
    giải thích vì sao là năm dương lịch, không phải niên độ)."""
    return text.replace("{YYYY}", f"{on_date.year:04d}").replace(
        "{YY}", f"{on_date.year % 100:02d}"
    )


@dataclass(frozen=True, slots=True)
class NumberingRule:
    """Quy tắc đánh số của một loại chứng từ (FR-SYS-063).

    Là **giá trị truyền vào**, không phải thứ dịch vụ tự tra: quy tắc thuộc về
    định nghĩa loại chứng từ, mà định nghĩa đó là dữ liệu cấu hình của gói
    TT99/TT133 (phase 5). Nhận từ ngoài vào giữ cho `kernel.numbering` không
    phải biết loại chứng từ nào tồn tại — đúng luật phụ thuộc #5.

    `prefix`/`suffix` nhận token năm `{YYYY}`/`{YY}` (lát 5D, trả nợ 4D): dãy
    reset theo năm mà số KHÔNG mang năm sẽ cấp lại `GLE00001` vào tháng 1 năm
    sau và chết ở `uq_vouchers_type_branch_no` (duy nhất theo loại+chi nhánh+số,
    không có chiều năm). Token bung lúc TẠO DÒNG BỘ ĐẾM của chu kỳ (xem
    `_define`) — mỗi chu kỳ một dòng, định dạng đóng băng cùng dòng đó.
    """

    document_type: str
    prefix: str = ""
    suffix: str = ""
    padding: int = 5
    allow_gaps: bool = True
    reset_rule: ResetRule = ResetRule.YEARLY
    per_branch: bool = True

    def __post_init__(self) -> None:
        # Token năm trong một dãy KHÔNG reset theo năm là định dạng nói dối:
        # số "GLE26-00123" cấp năm 2027 vì dãy đóng băng năm lúc tạo. Chặn ở
        # chỗ khai quy tắc — nơi duy nhất sửa được.
        if self.reset_rule is not ResetRule.YEARLY and _has_year_token(self.prefix + self.suffix):
            raise ValueError(
                "Token {YYYY}/{YY} chỉ dùng được với reset_rule YEARLY "
                f"(quy tắc của {self.document_type} đang là {self.reset_rule.value})"
            )

    def scope_key(self, *, branch_id: int | None, on_date: date) -> str:
        """Khóa phạm vi của dãy số áp dụng cho chứng từ này.

        Chu kỳ reset nằm trong khóa chứ không thành một nhánh `if` lúc cấp số:
        sang tháng mới là sang một dòng bộ đếm mới, bắt đầu từ 1, không cần ai
        chạy một tác vụ "reset" mà quên chạy thì số nhảy sai cả tháng.
        """
        parts = [self.document_type]
        if self.per_branch:
            parts.append(f"BRANCH:{branch_id if branch_id is not None else 'ALL'}")
        if self.reset_rule is ResetRule.YEARLY:
            parts.append(f"{on_date.year:04d}")
        elif self.reset_rule is ResetRule.MONTHLY:
            parts.append(f"{on_date.year:04d}-{on_date.month:02d}")
        return SCOPE_SEPARATOR.join(parts)


class NumberingService:
    """Cấp số trong transaction đang mở của người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def allocate(
        self,
        rule: NumberingRule,
        *,
        branch_id: int | None,
        on_date: date,
        document_id: UUID,
    ) -> str:
        """Cấp số kế tiếp cho một chứng từ, tạo dãy nếu chu kỳ này chưa có.

        Tự tạo dòng bộ đếm là **an toàn ở đây** và không mâu thuẫn với
        `NumberSequenceNotFoundError`: cấu hình (tiền tố, độ dài, chu kỳ) đến từ
        `rule` mà người gọi đưa vào, nên không có giá trị nào bị đoán. Cái bị
        chặn là đường ngược lại — cấp số khi **không** biết quy tắc, tức
        `allocate_by_scope_key`.
        """
        scope_key = rule.scope_key(branch_id=branch_id, on_date=on_date)
        sequence = self._lock_sequence(scope_key) or self._define_or_relock(
            rule, scope_key, on_date=on_date
        )
        return self._issue(sequence, document_id=document_id)

    def allocate_by_scope_key(self, scope_key: str, *, document_id: UUID) -> str:
        """Cấp số từ một dãy **đã khai sẵn** — không có quy tắc thì từ chối.

        Đường dùng cho công cụ quản trị và cho việc cấp lại số của một dãy đang
        chạy. Không tự tạo dãy: tiền tố và độ dài số chứng từ là thứ doanh nghiệp
        đã đăng ký và in trên giấy, nên một dãy sinh ngầm với giá trị mặc định
        tạo ra những số chứng từ không ai nhận ra là của mình.
        """
        sequence = self._lock_sequence(scope_key)
        if sequence is None:
            raise NumberSequenceNotFoundError(
                "Chưa khai quy tắc đánh số cho phạm vi này", scope_key=scope_key
            )
        return self._issue(sequence, document_id=document_id)

    def define(
        self, rule: NumberingRule, *, branch_id: int | None, on_date: date
    ) -> NumberSequence:
        """Khai trước dãy số cho một chu kỳ — dùng khi gieo dữ liệu và khi cấu hình."""
        return self._define(
            rule, scope_key=rule.scope_key(branch_id=branch_id, on_date=on_date), on_date=on_date
        )

    def peek(self, scope_key: str) -> int | None:
        """Giá trị kế tiếp **không** khóa dòng — chỉ để hiển thị.

        Tách hẳn khỏi `allocate` vì đây là chỗ dễ dùng nhầm nhất: một màn hình
        hiện "số tiếp theo: 000124" rồi lưu chính con số đó là quay lại đúng lỗi
        `SELECT MAX + 1`. Số thật chỉ có sau khi ghi.
        """
        return self._session.execute(
            select(NumberSequence.next_value).where(NumberSequence.scope_key == scope_key)
        ).scalar_one_or_none()

    def _issue(self, sequence: NumberSequence, *, document_id: UUID) -> str:
        """Nhích bộ đếm và ghép số — đường chung của cả hai lối cấp số.

        Định dạng đọc từ **dòng bộ đếm**, không từ `NumberingRule`, kể cả khi
        người gọi có rule trong tay: dòng bộ đếm là thứ đang thật sự chạy. Hai
        nguồn định dạng cho cùng một dãy nghĩa là đổi tiền tố trong cấu hình sẽ
        cho ra hai kiểu số cùng nằm trong một dãy liên tục — và dãy đó không còn
        giải trình được nữa.
        """
        value = sequence.next_value
        sequence.next_value = value + 1
        number = f"{sequence.prefix}{value:0{sequence.padding}d}{sequence.suffix}"

        if not sequence.allow_gaps:
            self._session.add(
                AllocatedNumber(
                    scope_key=sequence.scope_key, number=number, document_id=document_id
                )
            )
        self._session.flush()
        return number

    def _define_or_relock(
        self, rule: NumberingRule, scope_key: str, *, on_date: date
    ) -> NumberSequence:
        """Tạo dòng bộ đếm cho chu kỳ mới, chịu được hai người cùng tạo.

        Cuộc đua này có thật và xảy ra vào đúng lúc đông người nhất: sáng ngày
        đầu tháng, chứng từ đầu tiên của mỗi người là chứng từ đầu tiên của chu
        kỳ, nên nhiều phiên cùng thấy "chưa có dòng nào" và cùng `INSERT`. Khóa
        duy nhất trên `scope_key` cho đúng một người thắng; người thua **không**
        được báo lỗi ra ngoài mà phải đọc lại dòng vừa được tạo và đi tiếp — với
        họ thì việc cấp số vẫn thành công, chỉ là số thứ hai thay vì số thứ nhất.

        Savepoint chứ không `try` trần, cùng lý do như `attachments/service.py`:
        một `IntegrityError` làm hỏng cả transaction ở PostgreSQL, nên bắt nó mà
        không có điểm quay lui thì mọi lệnh sau đó — kể cả lệnh ghi chứng từ —
        đều đổ theo.
        """
        try:
            with self._session.begin_nested():
                return self._define(rule, scope_key=scope_key, on_date=on_date)
        except IntegrityError:
            sequence = self._lock_sequence(scope_key)
            if sequence is None:
                raise
            return sequence

    def _lock_sequence(self, scope_key: str) -> NumberSequence | None:
        """Đọc dòng bộ đếm và **giữ khóa ghi** tới cuối transaction.

        `with_for_update()` là toàn bộ cơ chế chống trùng số. Bỏ nó đi thì mã
        vẫn chạy, test một luồng vẫn xanh, và hai hóa đơn cùng số chỉ xuất hiện
        khi có hai người nhập cùng lúc — xem `tests/integration/
        test_concurrent_numbering.py`.
        """
        return (
            self._session.execute(
                select(NumberSequence)
                .where(NumberSequence.scope_key == scope_key)
                .with_for_update()
            )
            .scalars()
            .first()
        )

    def _define(self, rule: NumberingRule, *, scope_key: str, on_date: date) -> NumberSequence:
        # Token năm bung Ở ĐÂY — định dạng đóng băng cùng dòng bộ đếm của chu
        # kỳ, đúng nguyên tắc "định dạng đọc từ dòng bộ đếm" của `_issue`.
        sequence = NumberSequence(
            scope_key=scope_key,
            document_type=rule.document_type,
            prefix=_expand_year_tokens(rule.prefix, on_date=on_date),
            suffix=_expand_year_tokens(rule.suffix, on_date=on_date),
            padding=rule.padding,
            allow_gaps=rule.allow_gaps,
            reset_rule=rule.reset_rule.value,
            next_value=1,
        )
        self._session.add(sequence)
        self._session.flush()
        return sequence
