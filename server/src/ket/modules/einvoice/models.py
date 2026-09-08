"""Bảng của phân hệ hóa đơn điện tử (`docs/srs/07`) và quản lý hóa đơn (`docs/srs/08`).

Ba bảng, và hình dạng của cả ba đến từ cùng một câu hỏi: **thứ gì phải trở nên
không biểu diễn được** thay vì phải nhớ kiểm.

**`einvoices` không mang một con số tiền nào.** Phác thảo plan có `total_*` trên
hóa đơn để BR-EIV-07 ("tổng tiền trên hóa đơn khớp chứng từ bán hàng gốc") có
chỗ mà so. Nhưng hai chỗ giữ cùng một con số là hai chỗ để nó lệch, và cái giá
của lệch ở đây là một tờ hóa đơn đã gửi cơ quan thuế không khớp sổ. Hóa đơn ở
lát này **luôn** sinh từ một chứng từ gốc (`source_voucher_id` NOT NULL), nên
tổng tiền đọc thẳng từ chứng từ ấy: BR-EIV-07 thành đúng theo cấu trúc, không
phải theo một phép kiểm ai đó phải nhớ chạy. Bản XML đã ký của 7E mới là ảnh
chụp bất biến của những con số ấy, và nó là một tệp đính kèm content-hash
(quyết định user 2026-09-07) chứ không phải mấy cột `BYTEA`.

**`branch_id` có mặt dù suy được từ chứng từ gốc.** Trái với `SubledgerEntry`
(7A C-2, bỏ hẳn cột để dòng lệch chi nhánh không tồn tại nổi), ở đây cột phải
có: hóa đơn được truy vấn thẳng bởi màn danh sách HĐĐT, bảng kê, và báo cáo
tình hình sử dụng hóa đơn — không có cột thì `p_branch_scope` không canh được
và cổng `test_rls_policy_coverage` **không nhìn thấy bảng**, đúng lỗ mà 7A phải
đi vá cho `opening_balance_invoices`. Cái giá — hai chỗ giữ một sự thật — trả
bằng **khóa ngoại ghép** `(source_voucher_id, branch_id)` → `vouchers (id,
branch_id)`: lệch chi nhánh vẫn không biểu diễn được, mà bảng vẫn có cột.

**Số hóa đơn cấp đúng một lần và không bao giờ đổi.** Không phải "bất biến sau
khi phát hành" mà là bất biến kể từ lúc có giá trị — trigger canh (xem
`0032`). Đây mới là chỗ dãy số thủng: phát hành lỗi rồi phát hành lại **giữ
nguyên số cũ** (ADR-013), và một đường ghi cấp số lần hai sẽ để lại một số
không thuộc về hóa đơn nào.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.identifiers import uuid7
from ket.kernel.persistence.base import DatasetBase

EINVOICE_TABLE_NAME = "einvoices"
ERROR_NOTICE_TABLE_NAME = "einvoice_error_notices"
REGISTRATION_TABLE_NAME = "invoice_registrations"
OUTBOX_TABLE_NAME = "einvoice_outbox"
PROVIDER_PROFILE_TABLE_NAME = "einvoice_provider_profiles"

INVOICE_NO_MAX_LENGTH = 50
TAX_AUTHORITY_CODE_MAX_LENGTH = 100
LOOKUP_CODE_MAX_LENGTH = 100
NOTICE_NO_MAX_LENGTH = 50
REASON_CODE_MAX_LENGTH = 50
PROVIDER_CODE_MAX_LENGTH = 50
PROVIDER_REF_MAX_LENGTH = 100
BASE_URL_MAX_LENGTH = 200
USERNAME_MAX_LENGTH = 100
TAX_CODE_MAX_LENGTH = 20


class EInvoiceStatus(IntEnum):
    """Vòng đời hóa đơn điện tử (`docs/srs/07` §3).

    Ranh giới **bất biến** nằm ở `DA_PHAT_HANH`: từ giá trị này trở lên, nội
    dung hóa đơn đã ra khỏi phần mềm — nó đã mang số, đã ký, đã tới cơ quan
    thuế — nên mọi sửa đổi phải đi qua thay thế / điều chỉnh / hủy (BR-EIV-01).
    Vì vậy thứ tự các thành viên **không** tùy tiện: `ISSUED_FLOOR` và trigger
    DB đều so sánh bằng `>=`, và chèn một trạng thái mới vào giữa sẽ lặng lẽ
    dời ranh giới ấy.
    """

    CHUA_PHAT_HANH = 0
    """Hóa đơn đã lập, chưa có số. Sửa và xóa được."""
    DANG_PHAT_HANH = 1
    """Đã cấp số, đã đưa vào hàng đợi truyền tải — chưa biết cơ quan thuế nhận
    hay chưa. Số đã tiêu kể từ đây (RT-10: cấp số trong chính txn lật hàng đợi)."""
    PHAT_HANH_LOI = 2
    """Nhà cung cấp hoặc cơ quan thuế từ chối. **Giữ nguyên số đã cấp**
    (ADR-013) — phát hành lại dùng lại số ấy, bỏ hẳn thì phải lập biên bản hủy
    số. Không có đường nào đưa số này về cho hóa đơn khác."""
    DA_PHAT_HANH = 3
    DA_GUI = 4
    DA_THAY_THE = 5
    DA_DIEU_CHINH = 6
    DA_HUY = 7


ISSUED_FLOOR: EInvoiceStatus = EInvoiceStatus.DA_PHAT_HANH
"""Ngưỡng "đã ra khỏi phần mềm" của BR-EIV-01 — ràng buộc bảng, trigger DB và
`service` đều so `status >= ISSUED_FLOOR`.

Một tên chứ không con số `3` rải ba chỗ: ngưỡng này là một **quyết định nghiệp
vụ** (hóa đơn bất biến kể từ khi có số và đã tới cơ quan thuế), và ba con số 3
rời nhau là ba chỗ để nó lệch khi vòng đời mọc thêm trạng thái.
"""


class ErrorNoticeKind(IntEnum):
    """Loại văn bản kèm hóa đơn.

    BR-EIV-04 đòi hủy hóa đơn phải có **cả hai**: thông báo gửi cơ quan thuế và
    biên bản thỏa thuận với người mua. Phác thảo plan để hai cột `cancel_notice_id`
    / `cancel_record_id` trên `einvoices`; lát này đảo chiều (quyết định user
    2026-09-07) — văn bản trỏ về hóa đơn, kèm loại, và một chỉ mục duy nhất
    `(einvoice_id, kind)`. Lý do: hai cột cùng trỏ một bảng thì **không có cách
    nào ở tầng SQL** bắt cột thứ nhất phải là thông báo còn cột thứ hai phải là
    biên bản — luật ấy tụt xuống mã service, tức thành thứ quên được. Đảo chiều
    thì gắn nhầm loại là không biểu diễn được, và câu hỏi của state machine gọn
    lại thành "đã có đủ hai loại chưa".
    """

    THONG_BAO_HUY = 0
    """Thông báo hủy hóa đơn gửi cơ quan thuế (FR-EIV-031)."""
    BIEN_BAN_HUY = 1
    """Biên bản hủy thỏa thuận với người mua (FR-EIV-032)."""


CANCELLATION_KINDS: frozenset[ErrorNoticeKind] = frozenset(
    {ErrorNoticeKind.THONG_BAO_HUY, ErrorNoticeKind.BIEN_BAN_HUY}
)
"""Bộ văn bản BR-EIV-04 đòi có đủ trước khi hủy.

Một hằng số chứ không hai phép so rời trong `service`: 7F thêm thông báo sai sót
(FR-EIV-030/033/034) vào cùng bảng, và lúc ấy "mọi loại văn bản" khác hẳn "bộ
văn bản của lượt hủy". Tách sẵn tên cho hai khái niệm để lát sau không phải đoán
xem một phép so `kind IN (0, 1)` nào đó có ý gì.
"""


class NoticeStatus(IntEnum):
    """Văn bản đã nộp cơ quan thuế chưa."""

    NHAP = 0
    DA_NOP = 1


class RegistrationStatus(IntEnum):
    """Trạng thái hồ sơ đăng ký / thông báo phát hành (SRS 08)."""

    NHAP = 0
    DA_NOP = 1
    """Đã gửi cơ quan thuế, chờ chấp nhận."""
    HIEU_LUC = 2
    """Đang có hiệu lực — **chỉ** hồ sơ ở trạng thái này mới cho phát hành hóa
    đơn (BR-EIV-03)."""
    NGUNG = 3


class EInvoice(DatasetBase, Audited):
    """Một hóa đơn điện tử, từ lúc lập tới lúc hủy."""

    __tablename__ = EINVOICE_TABLE_NAME
    __table_args__ = (
        CheckConstraint(
            f"status BETWEEN {EInvoiceStatus.CHUA_PHAT_HANH} AND {EInvoiceStatus.DA_HUY}",
            name="status_known",
        ),
        CheckConstraint(
            "tax_authority_status IS NULL OR tax_authority_status BETWEEN 0 AND 3",
            name="tax_authority_status_known",
        ),
        # **Có số thì phải có ngày** — một chiều, không còn song điều kiện.
        #
        # Chiều "có số ⇒ có ngày" giữ nguyên lý do cũ: một hóa đơn mang số mà
        # không mang ngày thì không tra cứu được ở đâu cả. Chiều ngược lại phải
        # bỏ ở 7E-2 (quyết định user 2026-09-08): với ký hiệu khai nhà cung cấp,
        # **nhà cung cấp cấp số**, mà ngày hóa đơn thì ta chọn và gửi đi trong
        # chính bản XML (`ArisingDate`). Ngày vì thế có trước số, và khoảng giữa
        # ấy là một trạng thái thật chứ không phải dữ liệu hỏng.
        CheckConstraint(
            "invoice_no IS NULL OR invoice_date IS NOT NULL", name="number_needs_a_date"
        ),
        # **Từ `DA_PHAT_HANH` trở lên thì phải có số.** Nới ở 7E-2 (quyết định
        # user 2026-09-08) từ luật cũ "chỉ bản nháp mới được thiếu số".
        #
        # Luật cũ đúng khi hệ thống là nơi duy nhất cấp số. Với ký hiệu khai nhà
        # cung cấp thì **nhà cung cấp cấp số** — nó nằm trong câu trả lời của
        # `issueInvoices`, tức chỉ tới nơi sau khi tờ hóa đơn đã rời phần mềm.
        # `DANG_PHAT_HANH` và `PHAT_HANH_LOI` vì thế là hai trạng thái hợp lệ mà
        # chưa có số: "đang gửi, chưa nghe trả lời" và "gửi rồi, bị từ chối".
        #
        # Ngưỡng dời tới `DA_PHAT_HANH` chứ không bỏ hẳn: một tờ hóa đơn cơ quan
        # thuế đã nhận mà phần mềm không biết số của nó là dữ liệu vô dụng ở
        # đúng chỗ nó quan trọng nhất. Trigger `einvoices_immutable_after_issue`
        # canh chiều còn lại — số điền từ `NULL` được, đổi thì không.
        CheckConstraint(
            f"status < {EInvoiceStatus.DA_PHAT_HANH} OR invoice_no IS NOT NULL",
            name="issued_invoice_has_a_number",
        ),
        CheckConstraint("replaces_invoice_id <> id", name="does_not_replace_itself"),
        CheckConstraint("adjusts_invoice_id <> id", name="does_not_adjust_itself"),
        # BR-EIV-02 ở tầng bảng. Dãy `number_sequences` gap-free là thứ **sinh**
        # ra số liên tục; ràng buộc này là thứ chặn một đường ghi thứ hai gán
        # trùng số — hai cơ chế cho hai loại sai sót khác nhau.
        UniqueConstraint("invoice_form_id", "invoice_no", name="uq_einvoices_form_number"),
        # **Một chứng từ, một hóa đơn còn hiệu lực** (review 7D H-1). Hóa đơn cố
        # ý không mang số tiền — nó đọc từ chứng từ gốc — nên hai tờ hóa đơn của
        # cùng một chứng từ đều "khớp chứng từ gốc" từng đồng, và doanh thu khai
        # gấp đôi mà không phép kiểm nào thấy. Lập luận "BR-EIV-07 đúng theo cấu
        # trúc" đóng chiều LỆCH; ràng buộc này đóng chiều NHÂN ĐÔI.
        #
        # Riêng phần theo ba trạng thái cuối: hóa đơn thay thế của 7F trỏ về
        # cùng chứng từ trong khi tờ cũ chuyển sang `DA_THAY_THE` — hình dạng
        # ĐÚNG mà một ràng buộc toàn phần sẽ chặn nhầm.
        Index(
            "uq_einvoices_live_source_voucher",
            "source_voucher_id",
            unique=True,
            # Số trần viết thẳng, không nội suy: `test_no_sql_string_interpolation`
            # cấm mọi `text()` nhận chuỗi dựng động — và cấm đúng, vì đó là
            # đường một biến của người dùng đi lọt vào SQL. 5/6/7 =
            # `DA_THAY_THE`/`DA_DIEU_CHINH`/`DA_HUY`, và
            # `test_einvoice_state_machine` ghim đúng ba con số ấy nên đổi enum
            # mà quên sửa đây thì CI đỏ.
            postgresql_where=text("status NOT IN (5, 6, 7)"),
        ),
        ForeignKeyConstraint(
            ["source_voucher_id", "branch_id"],
            ["vouchers.id", "vouchers.branch_id"],
            name="fk_einvoices_source_voucher",
            ondelete="RESTRICT",
        ),
        # Đích cho khóa ngoại ghép của `einvoice_outbox` (7E-1), cùng khuôn với
        # `uq_vouchers_id_branch` mà chính bảng này trỏ vào. Không thêm bảo đảm
        # nào — `id` đã là khóa chính nên cặp ấy vốn duy nhất — chỉ nói ra để
        # PostgreSQL chấp nhận một `REFERENCES einvoices (id, branch_id)`.
        UniqueConstraint("id", "branch_id", name="uq_einvoices_id_branch"),
        Index("ix_einvoices_branch_status", "branch_id", "status"),
        Index("ix_einvoices_source_voucher", "source_voucher_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    """Chi nhánh của hóa đơn — **luôn** bằng chi nhánh chứng từ gốc, và khóa
    ngoại ghép ở `__table_args__` là thứ bảo đảm điều đó."""

    source_voucher_id: Mapped[UUID] = mapped_column(nullable=False)
    """Chứng từ sinh ra hóa đơn (FR-EIV-010, nửa "sinh từ chứng từ bán hàng").

    `RESTRICT` chứ không `CASCADE`: một hóa đơn đã phát hành sống lâu hơn quyền
    xóa chứng từ của người dùng, và FR-EIV-035 chặn đường xóa ấy từ trước —
    khóa ngoại là lớp chót cho đường ghi nào lách được guard.

    NOT NULL ở lát này: đường lập hóa đơn **trực tiếp** (không từ chứng từ) đòi
    bảng dòng hóa đơn của riêng nó, và đó là việc của 7F. Nới cột này thành
    nullable lúc ấy là một `ALTER`; đoán trước hình dạng bảng dòng thì không.
    """

    invoice_form_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_forms.id", ondelete="RESTRICT"), nullable=False
    )
    """Ký hiệu hóa đơn (kèm mẫu số của nó) — phạm vi của dãy số, BR-EIV-02."""

    invoice_no: Mapped[str | None] = mapped_column(String(INVOICE_NO_MAX_LENGTH), nullable=True)
    """Số hóa đơn, cấp lúc phát hành. **Gán một lần, không bao giờ đổi** —
    trigger canh."""

    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[EInvoiceStatus] = mapped_column(
        SmallInteger, nullable=False, default=EInvoiceStatus.CHUA_PHAT_HANH
    )

    tax_authority_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    """0 chưa gửi · 1 chờ CQT · 2 đã cấp mã · 3 bị từ chối (U3)."""

    tax_authority_code: Mapped[str | None] = mapped_column(
        String(TAX_AUTHORITY_CODE_MAX_LENGTH), nullable=True
    )
    tax_authority_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Lý do từ chối, hiện thẳng lên cột gộp trạng thái của UI (U3)."""

    lookup_code: Mapped[str | None] = mapped_column(String(LOOKUP_CODE_MAX_LENGTH), nullable=True)
    """Mã tra cứu cho người mua (FR-EIV-024)."""

    replaces_invoice_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(f"{EINVOICE_TABLE_NAME}.id", ondelete="RESTRICT"), nullable=True
    )
    """Hóa đơn bị thay thế (FR-EIV-030, BR-EIV-05).

    Cột và khóa ngoại dựng ở lát này, **đường ghi ở 7F**: ràng buộc "không tự
    thay thế mình" đã có, còn phép kiểm chuỗi không vòng đi cùng đường ghi —
    viết một phép kiểm cho một đường chưa tồn tại là viết một phép kiểm không
    ai chạy được.
    """

    adjusts_invoice_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(f"{EINVOICE_TABLE_NAME}.id", ondelete="RESTRICT"), nullable=True
    )
    """Hóa đơn bị điều chỉnh (FR-EIV-033) — cùng ghi chú với `replaces_invoice_id`."""

    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Thời điểm cấp số. Khác `invoice_date` (ngày ghi trên hóa đơn) đúng như
    `posting_date` khác `document_date` của chứng từ."""


class EInvoiceErrorNotice(DatasetBase, Audited):
    """Văn bản kèm một hóa đơn: thông báo hủy, biên bản hủy (7F: thông báo sai sót)."""

    __tablename__ = ERROR_NOTICE_TABLE_NAME
    __table_args__ = (
        CheckConstraint("kind BETWEEN 0 AND 1", name="kind_known"),
        CheckConstraint("status BETWEEN 0 AND 1", name="status_known"),
        CheckConstraint("notice_no <> ''", name="notice_no_not_blank"),
        # Cùng lập luận `posted_stamp_complete` của chứng từ: "đã nộp nhưng
        # không biết nộp lúc nào" là nửa dữ liệu chỉ lộ ra đúng lúc cần giải trình.
        CheckConstraint(
            f"(status = {NoticeStatus.DA_NOP}) = (submitted_at IS NOT NULL)",
            name="submitted_stamp_complete",
        ),
        # Đây là toàn bộ chỗ dựa của BR-EIV-04: mỗi hóa đơn tối đa **một** văn
        # bản mỗi loại, nên câu hỏi "đã đủ hai văn bản chưa" là một phép đếm
        # chứ không phải một vòng lặp phân loại.
        UniqueConstraint("einvoice_id", "kind", name="uq_einvoice_error_notices_kind"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)

    einvoice_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{EINVOICE_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`: văn bản hủy của một hóa đơn không có nghĩa gì khi hóa đơn ấy
    không còn. Xóa hóa đơn chỉ xảy ra ở trạng thái nháp (state machine canh), mà
    hóa đơn nháp thì chưa hủy được — nên đường cascade này trên thực tế không
    bao giờ xóa một văn bản đã nộp."""

    kind: Mapped[ErrorNoticeKind] = mapped_column(SmallInteger, nullable=False)
    notice_no: Mapped[str] = mapped_column(String(NOTICE_NO_MAX_LENGTH), nullable=False)
    notice_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(REASON_CODE_MAX_LENGTH), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[NoticeStatus] = mapped_column(
        SmallInteger, nullable=False, default=NoticeStatus.NHAP
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InvoiceRegistration(DatasetBase, Audited):
    """Hồ sơ đăng ký sử dụng HĐĐT / thông báo phát hành hóa đơn giấy (SRS 08).

    Một bảng cho cả hai vì chúng trả lời cùng một câu hỏi — "ký hiệu này được
    dùng từ ngày nào, với dải số nào, ở chi nhánh nào" — và BR-EIV-03 hỏi đúng
    câu ấy bất kể hóa đơn là giấy hay điện tử. Khác biệt duy nhất nằm ở dải số:
    hóa đơn đặt in có dải hữu hạn in sẵn, HĐĐT thì không.
    """

    __tablename__ = REGISTRATION_TABLE_NAME
    __table_args__ = (
        CheckConstraint("status BETWEEN 0 AND 3", name="status_known"),
        CheckConstraint("notice_no IS NULL OR notice_no <> ''", name="notice_no_not_blank"),
        CheckConstraint("(range_from IS NULL) = (range_to IS NULL)", name="range_bounds_together"),
        CheckConstraint("range_from IS NULL OR range_to >= range_from", name="range_is_ordered"),
        CheckConstraint("range_from IS NULL OR range_from >= 1", name="range_starts_at_one"),
        Index("ix_invoice_registrations_form", "invoice_form_id", "branch_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    """Chi nhánh được phép dùng dải số này (FR-INV-008 phân bổ theo chi nhánh).

    NOT NULL, khác `branch_id` của danh mục: một thông báo phát hành nộp cho cơ
    quan thuế **của một đơn vị cụ thể**, không có bản "dùng chung mọi chi nhánh"
    — và nếu có thì mỗi chi nhánh vẫn phải nộp bản của mình."""

    invoice_form_id: Mapped[int] = mapped_column(
        ForeignKey("invoice_forms.id", ondelete="RESTRICT"), nullable=False
    )

    notice_no: Mapped[str | None] = mapped_column(String(NOTICE_NO_MAX_LENGTH), nullable=True)
    notice_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    """Ngày bắt đầu sử dụng — vế phải của BR-EIV-03 (`invoice_date >= start_date`).

    NOT NULL: một hồ sơ không nói được từ ngày nào thì không trả lời được câu
    hỏi duy nhất mà bất biến ấy đặt ra."""

    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Số lượng hóa đơn đã thông báo phát hành (FR-INV-001) — chỉ hóa đơn giấy."""

    range_from: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    range_to: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Dải số được phép dùng, tính cả hai đầu (FR-INV-002). `NULL` = không giới
    hạn dải — đúng với HĐĐT, nơi số cấp liên tục không có trần đăng ký trước."""

    status: Mapped[RegistrationStatus] = mapped_column(
        SmallInteger, nullable=False, default=RegistrationStatus.NHAP
    )


class OutboxStatus(StrEnum):
    """Chặng của một dòng hàng đợi truyền tải (RT-10).

    Chuỗi ký tự chứ không `IntEnum` như `EInvoiceStatus`: trạng thái hóa đơn đi
    vào ràng buộc bảng, trigger và một cột `SMALLINT` mà pháp luật đọc, còn
    trạng thái hàng đợi chỉ có người vận hành đọc — trong `psql` lúc một lượt
    phát hành đang treo, đúng lúc `2` không nói được gì.

    `NEEDS_RECONCILE` là thành viên mang toàn bộ lý do bảng này tồn tại: nó
    **không** phải "lỗi", mà là "không biết". Provider có thể đã nhận và đã cấp
    mã cơ quan thuế trong khi câu trả lời rơi mất trên đường về. Gửi lại một
    dòng như thế mà chưa hỏi là cách tạo tờ hóa đơn thứ hai cho cùng chứng từ.

    **Không có `pending`.** Phác thảo plan có, và bỏ nó là một quyết định chứ
    không phải cắt cho gọn: một dòng "chờ tới lượt" không phân biệt được với một
    dòng đã gửi rồi mà lượt ghi kết luận bị rollback, nên nó là đúng cái trạng
    thái mà từ đó một lượt gửi lại mù trở nên hợp lệ. Dòng sinh ra đã `in_flight`
    kèm lease (§Pipeline của plan viết đúng như vậy), và mọi câu hỏi an toàn về
    sau đọc lease — xem `outbox.py`.
    """

    IN_FLIGHT = "in_flight"
    """Đang có người cầm: hoặc vừa xếp hàng, hoặc một worker vừa giành.
    `in_flight_since` là lease — hết hạn nghĩa là người cầm nó đã chết."""
    DONE = "done"
    """Provider đã nhận. Chặng cuối vui vẻ."""
    FAILED = "failed"
    """Provider **từ chối** — một câu trả lời rõ ràng, không phải mất tín hiệu.
    Số hóa đơn vẫn giữ (ADR-013); đường ra là phát hành lại hoặc lập biên bản
    hủy số."""
    NEEDS_RECONCILE = "needs_reconcile"
    """Không rõ kết quả. Chỉ giải bằng `query_status(client_ref)`, xem
    `reconcile.py`."""


class OutboxOperation(StrEnum):
    """Việc mà dòng hàng đợi mang đi.

    Lát 7E-1 chỉ ghi `ISSUE`. Các thành viên còn lại của vòng đời (gửi cho người
    mua, hủy, thay thế, điều chỉnh) **không** khai trước ở đây: khác với
    `EInvoiceAction` — nơi bảng chuyển là đặc tả vòng đời nên cạnh chưa cài vẫn
    phải có mặt — bảng này là một hàng đợi công việc, và một giá trị không đường
    ghi nào sinh ra cũng không đường đọc nào xử lý chỉ là một nhánh chết mà
    `CHECK` của migration phải nới ra để đón.
    """

    ISSUE = "issue"


class EInvoiceOutbox(DatasetBase):
    """Hàng đợi truyền tải hóa đơn tới nhà cung cấp (RT-10, LD-10).

    **Không `Audited`.** Ba bảng của 7D đều ghi vết vì chúng mang lời khai với
    cơ quan thuế; bảng này mang *tiến trình kỹ thuật* của một lượt gửi, và mỗi
    lượt thử sẽ đẻ một dòng nhật ký kiểm toán về việc `attempt_count` tăng từ 1
    lên 2. Thứ đáng ghi vết là kết quả — nó nằm trên `einvoices` và đã được ghi.

    **`client_ref` là khóa chống trùng phía nhà cung cấp**, sinh đúng một lần
    lúc xếp hàng và không bao giờ đổi. Mọi lượt gửi lại của cùng một dòng dùng
    lại đúng giá trị ấy: đó là thứ duy nhất cho phép provider trả lời "tôi nhận
    rồi" thay vì lập tờ thứ hai. `UNIQUE` ở tầng bảng, vì một khóa chống trùng
    trùng nhau thì không chống được gì.

    **Một dòng còn mở cho mỗi (hóa đơn, việc)** — chỉ mục bán phần ở
    `__table_args__`. Không có nó, hai lượt bấm "Phát hành" trên cùng tờ hóa đơn
    xếp hai dòng với hai `client_ref` khác nhau, và hai `client_ref` khác nhau
    là **hai hóa đơn** dưới mắt provider — đúng cái mà toàn bộ đường
    `needs_reconcile` bên dưới dựng ra để tránh.

    **`branch_id` + khóa ngoại ghép** `(einvoice_id, branch_id)` → `einvoices
    (id, branch_id)`. Cùng lập luận với `einvoices` ở 7D: bảng có cửa đọc thẳng
    (panel hàng đợi) nên phải có cột để RLS canh và để `test_rls_policy_coverage`
    nhìn thấy; cái giá là khóa ngoại ghép, đổi lại dòng lệch chi nhánh không
    biểu diễn được.
    """

    __tablename__ = OUTBOX_TABLE_NAME
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_flight', 'done', 'failed', 'needs_reconcile')",
            name="status_known",
        ),
        CheckConstraint("operation IN ('issue')", name="operation_known"),
        CheckConstraint("attempt_count >= 0", name="attempt_count_not_negative"),
        # Lease chỉ có nghĩa khi đang bay, và đang bay mà không có lease là dòng
        # không reaper nào đòi lại được — treo vĩnh viễn đúng như dòng `jobs`
        # kẹt "đang chạy" mà `kernel.jobs.reaper` sinh ra để chặn.
        CheckConstraint(
            "(status = 'in_flight') = (in_flight_since IS NOT NULL)",
            name="lease_only_while_in_flight",
        ),
        UniqueConstraint("client_ref", name="uq_einvoice_outbox_client_ref"),
        Index(
            "uq_einvoice_outbox_open_operation",
            "einvoice_id",
            "operation",
            unique=True,
            postgresql_where=text("status <> 'done' AND status <> 'failed'"),
        ),
        # Cửa của bộ bơm: lấy dòng tới hạn theo thứ tự tới hạn. Bán phần vì
        # `done` sẽ là đại đa số bảng sau vài tháng, và không dòng `done` nào
        # được bơm nhìn tới nữa.
        Index(
            "ix_einvoice_outbox_due",
            "next_attempt_at",
            postgresql_where=text("status IN ('in_flight', 'needs_reconcile')"),
        ),
        ForeignKeyConstraint(
            ["einvoice_id", "branch_id"],
            [f"{EINVOICE_TABLE_NAME}.id", f"{EINVOICE_TABLE_NAME}.branch_id"],
            name="fk_einvoice_outbox_einvoice",
            ondelete="CASCADE",
        ),
        Index("ix_einvoice_outbox_einvoice", "einvoice_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    einvoice_id: Mapped[UUID] = mapped_column(nullable=False)
    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    provider_code: Mapped[str] = mapped_column(String(PROVIDER_CODE_MAX_LENGTH), nullable=False)
    """Nhà cung cấp **đã chốt lúc xếp hàng**, không tra lại lúc gửi.

    Đổi cấu hình nhà cung cấp giữa lúc một dòng còn treo là chuyện bình thường;
    gửi dòng ấy đi cho nhà mới thì `client_ref` của nhà cũ mất ý nghĩa và lượt
    hỏi lại `query_status` hỏi nhầm người."""

    operation: Mapped[OutboxOperation] = mapped_column(String(20), nullable=False)
    client_ref: Mapped[UUID] = mapped_column(nullable=False, default=uuid7)
    provider_ref: Mapped[str | None] = mapped_column(String(PROVIDER_REF_MAX_LENGTH), nullable=True)
    """Mã tờ hóa đơn phía provider, khi họ trả về một mã riêng khác `client_ref`."""

    status: Mapped[OutboxStatus] = mapped_column(
        String(20), nullable=False, default=OutboxStatus.IN_FLIGHT
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    in_flight_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EInvoiceProviderProfile(DatasetBase, Audited):
    """Thông tin đăng nhập một nhà cung cấp hóa đơn điện tử (FR-EIV-001).

    **Một dòng cho mỗi `provider_code`.** Tài khoản là của *doanh nghiệp* với
    nhà cung cấp ấy, mà một dữ liệu kế toán là một doanh nghiệp — nên không có
    trục chi nhánh ở đây. Ký hiệu nào dùng nhà cung cấp nào thì
    `invoice_forms.provider_code` nói (đặt ở `0032`), và đó cũng là chỗ duy nhất
    quyết định.

    **Mật khẩu là `bytea`, không phải `text`.** Nó đi qua `SecretBox` (Fernet),
    đúng cơ chế đang giữ bí mật TOTP; kiểu cột là lớp phòng thủ thứ hai — một
    lệnh `UPDATE ... SET password_enc = 'matkhau'` viết nhầm giá trị dạng rõ bị
    PostgreSQL từ chối thay vì lặng lẽ nhận (xem `kernel/security/keystore.py`).

    **Không có cột nào giữ mã số thuế người bán ngoài `tax_code`**: nhà cung cấp
    xác thực theo mã số thuế, nên nó vừa là danh tính đăng nhập vừa là lời khai
    người bán trên tờ hóa đơn. Hai cột cho một sự thật là hai chỗ để lệch, và
    lệch ở đây nghĩa là hóa đơn phát hành dưới tên một pháp nhân khác.
    """

    __tablename__ = PROVIDER_PROFILE_TABLE_NAME
    __table_args__ = (
        UniqueConstraint("provider_code", name="uq_einvoice_provider_profiles_code"),
        CheckConstraint("base_url <> ''", name="base_url_not_blank"),
        CheckConstraint("username <> ''", name="username_not_blank"),
        CheckConstraint("tax_code <> ''", name="tax_code_not_blank"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_code: Mapped[str] = mapped_column(String(PROVIDER_CODE_MAX_LENGTH), nullable=False)
    base_url: Mapped[str] = mapped_column(String(BASE_URL_MAX_LENGTH), nullable=False)
    username: Mapped[str] = mapped_column(String(USERNAME_MAX_LENGTH), nullable=False)
    password_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    tax_code: Mapped[str] = mapped_column(String(TAX_CODE_MAX_LENGTH), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
