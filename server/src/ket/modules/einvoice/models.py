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
from decimal import Decimal
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
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ket.kernel.auditing.listener import Audited
from ket.kernel.currency.models import CURRENCY_CODE_LENGTH, RATE_PRECISION
from ket.kernel.identifiers import uuid7
from ket.kernel.money import (
    RATE_SCALE_DEFAULT,
    UNIT_PRICE_PRECISION,
    UNIT_PRICE_SCALE,
    VAT_RATE_PRECISION,
    VAT_RATE_SCALE,
)
from ket.kernel.persistence.base import DatasetBase
from ket.kernel.quantity import QUANTITY_PRECISION, QUANTITY_SCALE
from ket.posting.contracts import AMOUNT_PRECISION, AMOUNT_SCALE

EINVOICE_TABLE_NAME = "einvoices"
ERROR_NOTICE_TABLE_NAME = "einvoice_error_notices"
REGISTRATION_TABLE_NAME = "invoice_registrations"
OUTBOX_TABLE_NAME = "einvoice_outbox"
PROVIDER_PROFILE_TABLE_NAME = "einvoice_provider_profiles"
REPRESENTATION_TABLE_NAME = "einvoice_representations"
ERROR_FLOW_TABLE_NAME = "einvoice_error_flows"
INBOUND_TABLE_NAME = "inbound_einvoices"
INBOUND_LINE_TABLE_NAME = "inbound_einvoice_lines"

IDENTITY_CONSTRAINT = "uq_inbound_einvoices_identity"
"""Tên khóa nhận dạng tờ hóa đơn đầu vào.

Một hằng số chứ hai chuỗi giống nhau: đường ghi phân biệt lượt trùng với mọi
ràng buộc khác **theo tên**, nên tên gõ sai ở một trong hai chỗ sẽ làm lượt
trùng thật báo sai loại lỗi — và đó là loại lỗi chỉ lộ ra ở bản cài."""

INVOICE_NO_MAX_LENGTH = 50
TAX_AUTHORITY_CODE_MAX_LENGTH = 100
LOOKUP_CODE_MAX_LENGTH = 100
NOTICE_NO_MAX_LENGTH = 50
REASON_CODE_MAX_LENGTH = 50
FLOW_CODE_MAX_LENGTH = 50
LEGAL_BASIS_MAX_LENGTH = 200
PROVIDER_CODE_MAX_LENGTH = 50
PROVIDER_REF_MAX_LENGTH = 100
BASE_URL_MAX_LENGTH = 200
USERNAME_MAX_LENGTH = 100
TAX_CODE_MAX_LENGTH = 20
SENT_TO_MAX_LENGTH = 500
"""Trần cho ô "đã gửi cho ai" — đủ khoảng mười địa chỉ thư ngăn bằng `;`
(FR-EIV-022). Có trần chứ không `Text`: đây là danh sách người nhận, không phải
chỗ ghi chép tự do, và một cột không trần là một cột client ghi được megabyte."""


PDF_KIND = "pdf"
XML_KIND = "xml"
"""Hai giá trị của `einvoice_representations.kind`.

Chuỗi trần chứ không import `RepresentationKind`: `models` là tầng dưới của
`providers`, và `CheckConstraint` cần một hằng số nội suy được lúc dựng bảng.
`test_representation_kinds_match_the_protocol` ghim hai bộ giá trị bằng nhau."""

PARTY_NAME_MAX_LENGTH = 400
PARTY_ADDRESS_MAX_LENGTH = 500
INVOICE_FORM_MAX_LENGTH = 20
INVOICE_SERIAL_MAX_LENGTH = 20
LINE_DESCRIPTION_MAX_LENGTH = 500
"""Trần mô tả một dòng hàng của hóa đơn đầu vào — **cùng con số** với
`purchase_invoice_lines.description`.

Bằng nhau có chủ đích: dòng lưu ở đây tồn tại để thành dòng chứng từ mua, nên
một trần rộng hơn chỉ dời chỗ hỏng từ lượt nạp sang lượt lập chứng từ — nơi
người dùng đã tưởng tờ hóa đơn vào sổ rồi. Mô tả dài hơn thì lượt nạp nói ngay."""

LINE_UNIT_MAX_LENGTH = 50
VAT_RATE_TEXT_MAX_LENGTH = 50

MEDIA_TYPE_MAX_LENGTH = 150
FILE_NAME_MAX_LENGTH = 255
SHA256_HEX_LENGTH = 64


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


SUPERSEDED_STATUSES: frozenset[EInvoiceStatus] = frozenset(
    {EInvoiceStatus.DA_THAY_THE, EInvoiceStatus.DA_DIEU_CHINH, EInvoiceStatus.DA_HUY}
)
"""Ba trạng thái cuối — tờ hóa đơn đã **xử lý xong sai sót** và thôi còn hiệu lực.

Chúng đi cùng nhau ở ba chỗ và phải đi cùng nhau ở cả ba: bảng chuyển không cho
chúng có cạnh ra, chỉ mục riêng phần `uq_einvoices_live_source_voucher` loại
chúng khỏi luật "một chứng từ một hóa đơn", và FR-EIV-035 **nhả** chứng từ gốc ở
chúng (xem `service.issued_for_voucher`). Một tên chứ không ba con số rời, cùng
lập luận `ISSUED_FLOOR`.
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
    THONG_BAO_SAI_SOT = 2
    """Thông báo hóa đơn có sai sót gửi cơ quan thuế (Mẫu 04/SS-HĐĐT, TT78).

    Kèm **mọi** cách xử lý sai sót, kể cả hủy — nó khai với cơ quan thuế rằng tờ
    hóa đơn kia có sai sót, còn `THONG_BAO_HUY` khai riêng việc hủy. Vì vậy nó
    **không** thuộc `CANCELLATION_KINDS`: một tờ có đủ thông báo sai sót và biên
    bản hủy vẫn chưa đủ điều kiện BR-EIV-04.
    """


CANCELLATION_KINDS: frozenset[ErrorNoticeKind] = frozenset(
    {ErrorNoticeKind.THONG_BAO_HUY, ErrorNoticeKind.BIEN_BAN_HUY}
)
"""Bộ văn bản BR-EIV-04 đòi có đủ trước khi hủy.

Một hằng số chứ không hai phép so rời trong `service`: 7F thêm thông báo sai sót
(FR-EIV-030/033/034) vào cùng bảng, và lúc ấy "mọi loại văn bản" khác hẳn "bộ
văn bản của lượt hủy". Tách sẵn tên cho hai khái niệm để lát sau không phải đoán
xem một phép so `kind IN (0, 1)` nào đó có ý gì.
"""


class ErrorKind(IntEnum):
    """Câu hỏi thứ nhất của wizard: "hóa đơn sai chỗ nào?" (`docs/srs/07` §4.4)."""

    SAI_THONG_TIN = 0
    """Sai thông tin **không** làm đổi số tiền (tên, địa chỉ, mã số thuế)."""
    SAI_SO_TIEN = 1
    """Sai số tiền, số lượng hoặc thuế suất."""
    KHONG_PHAT_SINH = 2
    """Không phát sinh giao dịch — tờ hóa đơn lẽ ra không tồn tại."""


class Remedy(IntEnum):
    """Cách xử lý sai sót mà bảng quyết định trả về (FR-EIV-030/031/033/034)."""

    THAY_THE = 0
    """Lập hóa đơn thay thế trên chính chứng từ cũ (FR-EIV-030, BR-EIV-05)."""
    DIEU_CHINH_THONG_TIN = 1
    """Điều chỉnh thông tin, không đổi số tiền (FR-EIV-034)."""
    DIEU_CHINH_TIEN = 2
    """Điều chỉnh tăng/giảm — hóa đơn mang **phần chênh** (FR-EIV-033)."""
    HUY = 3
    """Hủy, kèm thông báo hủy và biên bản hủy (FR-EIV-031/032, BR-EIV-04)."""


DELTA_VOUCHER_REMEDIES: frozenset[Remedy] = frozenset({Remedy.DIEU_CHINH_TIEN})
"""Cách xử lý đòi người gọi đưa kèm một chứng từ bán mang **phần chênh**.

Hóa đơn cố ý không mang cột tiền — nó đọc tổng từ chứng từ gốc, và đó chính là
cách BR-EIV-07 thành đúng theo cấu trúc (7D). Một tờ hóa đơn điều chỉnh tăng
hoặc giảm chỉ khai phần chênh, nên nó **không** trỏ được vào chứng từ gốc mang
số tiền đầy đủ: nó cần chứng từ của riêng nó, và `sales` mang hai `kind` cho
đúng việc ấy (`SalesInvoiceKind.ADJUSTMENT_INCREASE`/`ADJUSTMENT_DECREASE`).

Ba cách xử lý còn lại **không** nhận chứng từ nào, và đó là một luật hai chiều
— đưa kèm chứng từ ở nhánh không cần là một lỗi, không phải một tham số thừa bị
bỏ qua. Điều chỉnh THÔNG TIN nằm ở vế không cần: không có đồng nào đổi, nên
tổng của tờ điều chỉnh **đúng bằng** tổng tờ cũ và BR-EIV-07 giữ nguyên trên
chính chứng từ gốc.
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
        # **Chưa phát hành thì chưa gửi được cho ai.** Một chiều, không phải
        # song điều kiện: `DA_THAY_THE`/`DA_DIEU_CHINH`/`DA_HUY` tới được thẳng
        # từ `DA_PHAT_HANH` mà chưa từng gửi ai, nên "trạng thái cao ⇒ có dấu
        # gửi" là sai trên dữ liệu đúng.
        CheckConstraint(
            f"sent_at IS NULL OR status >= {EInvoiceStatus.DA_PHAT_HANH}",
            name="sent_after_issue",
        ),
        # Ba cột của một lời khai đi cùng nhau — cùng lập luận
        # `submitted_stamp_complete` của thông báo hủy: "đã gửi nhưng không biết
        # gửi cho ai" là nửa dữ liệu chỉ lộ ra đúng lúc cần giải trình.
        CheckConstraint(
            "(sent_at IS NULL) = (sent_to IS NULL) AND (sent_at IS NULL) = (sent_by IS NULL)",
            name="sent_stamp_complete",
        ),
        CheckConstraint("sent_to <> ''", name="sent_to_not_blank"),
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

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Thời điểm bản thể hiện tới tay người mua (FR-EIV-020), do người dùng khai.

    **Người dùng khai chứ không phải đồng hồ máy chủ**: lượt gửi xảy ra NGOÀI
    phần mềm (quyết định user 2026-09-08, phương án A3) — qua hộp thư của kế
    toán, hoặc do chính nhà cung cấp gửi theo cấu hình bên họ. Dấu ở đây là lời
    khai về một việc đã xảy ra, nên nó nhận được ngày lùi; `service.mark_sent`
    chặn hai đầu (không sau hôm nay, không trước `issued_at`).
    """

    sent_to: Mapped[str | None] = mapped_column(String(SENT_TO_MAX_LENGTH), nullable=True)
    """Đã gửi cho ai — địa chỉ thư, hoặc mô tả nếu giao tay.

    **Bắt buộc không rỗng khi đánh dấu đã gửi.** Không có nó, `DA_GUI` là một
    lời khai không nói được gì, và người kiểm tra về sau không có cách nào đối
    chiếu. Đây là thứ thay cho việc theo dõi trạng thái gửi tự động (FR-EIV-023)
    mà lát này chưa có hạ tầng để làm thật.
    """

    sent_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Người đánh dấu — `user_id` trần như mọi tham chiếu `public.users`
    (`persistence/base.py`; tiền lệ `print_log.printed_by`).

    `Audited` đã ghi vết lượt sửa, nhưng cột này đứng cạnh hai cột kia để một
    câu truy vấn trả lời trọn "ai khai đã gửi cho ai, lúc nào" mà không phải mở
    nhật ký."""


class EInvoiceErrorNotice(DatasetBase, Audited):
    """Văn bản kèm một hóa đơn: thông báo hủy, biên bản hủy (7F: thông báo sai sót)."""

    __tablename__ = ERROR_NOTICE_TABLE_NAME
    __table_args__ = (
        CheckConstraint(
            f"kind BETWEEN {ErrorNoticeKind.THONG_BAO_HUY} AND {ErrorNoticeKind.THONG_BAO_SAI_SOT}",
            name="kind_known",
        ),
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


class EInvoiceErrorFlow(DatasetBase, Audited):
    """Bảng quyết định của wizard xử lý sai sót — **dữ liệu, không phải `if/elif`**.

    Quy định về xử lý hóa đơn sai sót (NĐ123 điều 19, TT78 điều 7) đổi thì sửa
    dòng ở đây, không phát hành lại bản mới (FR-NFR-055). Đó là toàn bộ lý do
    bảng này là bảng chứ không phải một `dict` trong mã.

    **Không có `branch_id`**: đây là quy định của nhà nước, chung cho cả dataset
    — cùng hình dạng `item_discount_tiers`/`price_list_lines`, nên cổng
    `test_rls_policy_coverage` không đòi policy cho nó.

    **Không thuộc gói cấu hình** (khác `auto_posting_rules` của 6A, dù cùng lối
    "cấu hình là dữ liệu"): gói cấu hình tồn tại để TT99 và TT133 gán **tài
    khoản khác nhau** cho cùng một nghiệp vụ. Cách xử lý hóa đơn sai sót không
    phụ thuộc chế độ kế toán — TT78 áp như nhau cho cả hai — nên phần phạm vi
    theo gói ở đây là máy móc không ai dùng.
    """

    __tablename__ = ERROR_FLOW_TABLE_NAME
    __table_args__ = (
        CheckConstraint("code <> ''", name="code_not_blank"),
        CheckConstraint(
            f"error_kind BETWEEN {ErrorKind.SAI_THONG_TIN} AND {ErrorKind.KHONG_PHAT_SINH}",
            name="error_kind_known",
        ),
        CheckConstraint(
            f"remedy BETWEEN {Remedy.THAY_THE} AND {Remedy.HUY}",
            name="remedy_known",
        ),
        UniqueConstraint("code", name="uq_einvoice_error_flows_code"),
        # **`NULLS NOT DISTINCT` là phần mang bất biến, không phải trang trí.**
        # `buyer_declared IS NULL` nghĩa là "câu hỏi thứ hai không áp dụng", và
        # `UNIQUE` mặc định của PostgreSQL coi hai `NULL` là **khác nhau** — tức
        # hai dòng `(KHONG_PHAT_SINH, NULL)` mâu thuẫn nhau lọt được vào bảng, và
        # phép tra sẽ trả về dòng nào tùy tâm trạng của planner. Với `NULLS NOT
        # DISTINCT` thì "một bộ câu trả lời ra đúng một cách xử lý" là điều
        # không biểu diễn ngược lại được.
        UniqueConstraint(
            "error_kind",
            "buyer_declared",
            name="uq_einvoice_error_flows_answers",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    code: Mapped[str] = mapped_column(String(FLOW_CODE_MAX_LENGTH), nullable=False)
    """Tên hằng của một nhánh, để test và thông điệp lỗi nhắc tới nó mà không
    phải gõ lại cặp câu trả lời."""

    error_kind: Mapped[ErrorKind] = mapped_column(SmallInteger, nullable=False)

    buyer_declared: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """Câu hỏi thứ hai: "khách đã kê khai thuế chưa?".

    `NULL` = **không hỏi** ở nhánh này, không phải "chưa biết": tờ hóa đơn không
    phát sinh giao dịch thì hủy bất kể khách đã kê khai hay chưa. `next_question`
    đọc đúng cột này để biết còn phải hỏi gì (xem `error_flow.py`)."""

    remedy: Mapped[Remedy] = mapped_column(SmallInteger, nullable=False)

    legal_basis: Mapped[str] = mapped_column(String(LEGAL_BASIS_MAX_LENGTH), nullable=False)
    """Căn cứ pháp lý của nhánh — hiện lên wizard để kế toán đối chiếu, và là
    thứ người sửa bảng phải cập nhật cùng lúc khi quy định đổi."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """Tắt một nhánh khi quy định bỏ nó, thay vì xóa dòng: hóa đơn đã xử lý theo
    nhánh ấy vẫn phải giải thích được về sau."""


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
        # **Bắt buộc `https`.** Header xác thực của nhà cung cấp mang mật khẩu
        # dạng rõ theo đúng đặc tả của họ (xem `providers/easyinvoice/auth.py`),
        # nên một địa chỉ `http://` để lộ nó trên đường truyền. Ràng buộc ở tầng
        # bảng chứ không chỉ ở validator: hồ sơ này còn khai được bằng lệnh SQL
        # lúc dựng bản cài.
        CheckConstraint("base_url LIKE 'https://%'", name="base_url_is_https"),
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


class EInvoiceRepresentation(DatasetBase, Audited):
    """Bản thể hiện PDF / tệp XML đã lưu trữ của một tờ hóa đơn (FR-EIV-026).

    **Bảng riêng, không dùng `attachments` chung — và đó là bản sửa của một lỗ
    CRITICAL.** Bản đầu của lát 7E-3 cất tệp vào bảng đính kèm dùng chung rồi
    nhận dạng nó bằng `(entity_type='einvoices', media_type)`. Cửa
    `POST /api/v1/attachments` cho phép đính **bất cứ tệp nào** vào **bất cứ
    `entity_id` nào** mà không hỏi bản ghi chủ thuộc loại gì, nên một tệp PDF
    tùy ý đính vào id hóa đơn sẽ chiếm chỗ vĩnh viễn: đường tải thấy "đã có" nên
    không bao giờ đi lấy bản thật về nữa. Đo được bằng một lượt gọi HTTP.

    Bảng riêng đóng bốn thứ cùng lúc, và đó là lý do nó đáng tồn tại thay vì một
    phép lọc chặt hơn:

    * **không ai ghi được vào đây ngoài đường phát hành** — không có cửa HTTP
      nào nhận tệp cho bảng này;
    * `UNIQUE (einvoice_id, kind)` **dựng thật** cái bất biến "mỗi tờ hóa đơn
      nhiều nhất một tệp mỗi loại", thứ mà phép lọc theo `media_type` chỉ *khai*
      chứ không bảo đảm;
    * nội dung hóa đơn (người mua, từng dòng hàng, số tiền) không còn đọc được
      qua `GET /api/v1/attachments/…` bằng một mã quyền của phân hệ khác;
    * `kind` là cột riêng nên nó không còn phụ thuộc `media_type` — bảng đính
      kèm chống trùng theo `content_hash` **không kể** kiểu nội dung, và hai
      luật khác nhau trên cùng một khóa là chỗ một lượt tải hợp lệ kẹt 409.

    **Tệp vẫn nằm ở kho content-hash của phase 2** (`kernel/attachments/storage`
    — nó không biết gì về DB nên dùng thẳng được), nên quyết định 7D giữ nguyên:
    `einvoices` không có cột `BYTEA` nào, và `pg_dump` hằng đêm không phải đọc
    lại vài chục GB tệp. Hai bảng dùng chung kho là **có lợi**: cùng nội dung
    thì cùng một tệp trên đĩa.

    **Nợ chuyển phase 11:** lượt quét đối chiếu dọn tệp mồ côi mà
    `kernel/attachments/__init__` hứa hẹn phải đọc **cả hai** bảng. Quét mình
    `attachments` sẽ xóa đúng những tệp lưu trữ mười năm này.
    """

    __tablename__ = REPRESENTATION_TABLE_NAME
    __table_args__ = (
        # Bất biến trung tâm của bảng: một tờ, một tệp mỗi loại. Toàn phần chứ
        # không bán phần — không có trạng thái nào của hóa đơn làm bản lưu trữ
        # cũ hết giá trị, kể cả lượt hủy.
        UniqueConstraint("einvoice_id", "kind", name="uq_einvoice_representations_kind"),
        CheckConstraint(f"kind IN ('{PDF_KIND}', '{XML_KIND}')", name="kind_known"),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("file_name <> ''", name="file_name_not_blank"),
        CheckConstraint(
            f"char_length(content_hash) = {SHA256_HEX_LENGTH}", name="content_hash_is_sha256"
        ),
        # `CASCADE` chứ không `RESTRICT`, ngược `einvoices.source_voucher_id`:
        # bản thể hiện là *dẫn xuất* của tờ hóa đơn, không phải một lời khai
        # đứng riêng, nên nó không có lý do sống lâu hơn dòng nó thuộc về. Mà
        # hóa đơn đã cấp số thì trigger `einvoices_numbered_rows_are_permanent`
        # đã cấm xóa, nên đường duy nhất chạm tới đây là xóa một bản nháp — thứ
        # chưa bao giờ có bản thể hiện nào.
        ForeignKeyConstraint(
            ["einvoice_id", "branch_id"],
            [f"{EINVOICE_TABLE_NAME}.id", f"{EINVOICE_TABLE_NAME}.branch_id"],
            name="fk_einvoice_representations_einvoice",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    einvoice_id: Mapped[UUID] = mapped_column(nullable=False)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    """Chi nhánh **của hóa đơn**, không phải chi nhánh đang thao tác của người
    tải: tệp phải nhìn thấy được đúng bởi những người nhìn thấy tờ hóa đơn, và
    hai phạm vi ấy lệch nhau ngay khi một người dùng đa chi nhánh bấm tải về
    trong lúc đang đứng ở chi nhánh khác. Khóa ngoại ghép giữ hai vế không lệch."""

    kind: Mapped[str] = mapped_column(String(3), nullable=False)
    """`pdf` hoặc `xml` — giá trị của `RepresentationKind`."""

    content_hash: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(MEDIA_TYPE_MAX_LENGTH), nullable=False)
    file_name: Mapped[str] = mapped_column(String(FILE_NAME_MAX_LENGTH), nullable=False)
    """Tên hiển thị lúc tải về. Kho định địa chỉ theo nội dung nên nó **không**
    là tên tệp trên đĩa — đường ghi lọc nó trước khi cất (xem `representation`)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    """`user_id` trần như mọi tham chiếu `public.users`."""


class InboundEInvoice(DatasetBase, Audited):
    """Một tờ hóa đơn điện tử **đầu vào** đã nạp (FR-EIV-040).

    **Vì sao là một bảng chứ không chỉ một payload điền sẵn.** Đường rẻ hơn là
    phân giải tệp XML rồi trả về ngay một thân chứng từ mua để người dùng lưu.
    Nó hỏng ở ba chỗ, và chỗ thứ nhất là chỗ không sửa được về sau:

    * **Không khử trùng được.** Cùng một tờ hóa đơn thường đến hai lần — bản
      nhà cung cấp gửi thư và bản tải từ cổng tra cứu — và không có một dòng
      nào mang `(MST người bán, mẫu số, ký hiệu, số)` thì lần thứ hai đi thẳng
      thành một chứng từ mua thứ hai. Hệ quả là khấu trừ thuế GTGT đầu vào hai
      lần trên cùng một tờ hóa đơn.
    * **Tờ chưa lập chứng từ thì biến mất**, nên không có danh sách việc cần làm
      — thứ duy nhất trả lời được "tháng này còn hóa đơn nào chưa vào sổ".
    * **Nghĩa vụ lưu trữ mười năm** (FR-NFR-023) đứng độc lập với chứng từ kế
      toán: tờ hóa đơn phải còn đọc được kể cả khi chứng từ bị xóa và lập lại.

    **`voucher_id` là một cột, và đó chính là luật "một tờ, một chứng từ".**
    Không có bảng nối, không có trạng thái: `NULL` nghĩa là chưa lập chứng từ,
    và một cột thì không mang được hai giá trị. Cột `status` riêng sẽ là nguồn
    sự thật thứ hai cho cùng một câu hỏi — đúng thứ 7D đã tránh khi quyết định
    hóa đơn không mang cột tiền.

    **Không có cột `vendor_id`.** Người bán nhận ra bằng `seller_tax_code`,
    tra vào danh mục **lúc lập chứng từ** chứ không lúc nạp. Một ảnh chụp lưu
    sẵn sẽ lạc hậu ngay lần đầu ai đó sửa mã số thuế của đối tác, và nó không
    mua lại được gì: lượt lập chứng từ dù sao cũng phải đọc danh mục để biết
    điều khoản thanh toán và tài khoản phải trả.

    **Đơn vị tiền và tỷ giá ghi nguyên như tờ hóa đơn khai**, không quy đổi:
    quy đổi ở đây rồi để chứng từ nhân ngược lại là hai lần làm tròn, và hai
    lần làm tròn là hai con số — cùng lập luận đã ghi ở `SubledgerEntry` và ở
    bộ dựng XML chiều gửi.
    """

    __tablename__ = INBOUND_TABLE_NAME
    __table_args__ = (
        # Danh tính pháp lý của một tờ hóa đơn — bốn thứ, không thứ nào do phần
        # mềm này cấp. Không có `branch_id` trong khóa có chủ đích: cùng một tờ
        # hóa đơn không được vào sổ hai lần dù hai lượt nạp đứng ở hai chi
        # nhánh khác nhau, vì sổ thì chỉ có một.
        #
        # RLS vì thế phải được tính tới ở đường ghi: người đứng ở chi nhánh B
        # **không thấy** dòng chi nhánh A, nên phép tra trước lượt ghi sẽ trượt
        # và ràng buộc này mới là thứ chặn thật. Xem `inbound.record`.
        #
        # **Khóa so trên dạng CHUẨN HÓA, không trên chuỗi thô**, và đó là phần
        # quyết định xem nó có chặn thật hay không. Bản gửi qua thư và bản tải
        # từ cổng tra cứu của **cùng một tờ** hay khác nhau đúng ở cách viết:
        # số hóa đơn `00004994` với `4994` (số hóa đơn là một *số*, phần đệm chỉ
        # để in), ký hiệu viết hoa ở nhà cung cấp này và viết thường ở nhà cung
        # cấp kia, mã số thuế có khoảng trắng. So chuỗi thô thì cả hai lọt, và
        # thuế đầu vào được khấu trừ hai lần trên một tờ hóa đơn.
        #
        # Chuẩn hóa nằm trong **biểu thức của chỉ mục** chứ không ở cột thứ hai:
        # cột thô vẫn giữ nguyên chữ người bán viết (thứ phải in ra và đối chiếu
        # với cơ quan thuế), và không có nguồn sự thật thứ hai nào để lệch.
        Index(
            IDENTITY_CONSTRAINT,
            text("upper(btrim(seller_tax_code))"),
            text("upper(btrim(invoice_form))"),
            text("upper(btrim(invoice_serial))"),
            text("coalesce(nullif(ltrim(btrim(invoice_no), '0'), ''), '0')"),
            unique=True,
        ),
        CheckConstraint("seller_tax_code <> ''", name="seller_tax_code_not_blank"),
        CheckConstraint("buyer_tax_code <> ''", name="buyer_tax_code_not_blank"),
        CheckConstraint("seller_name <> ''", name="seller_name_not_blank"),
        CheckConstraint("invoice_form <> ''", name="invoice_form_not_blank"),
        CheckConstraint("invoice_serial <> ''", name="invoice_serial_not_blank"),
        CheckConstraint("invoice_no <> ''", name="invoice_no_not_blank"),
        CheckConstraint("exchange_rate > 0", name="exchange_rate_positive"),
        CheckConstraint(
            "total_before_tax >= 0 AND total_vat >= 0 AND total_amount >= 0",
            name="totals_not_negative",
        ),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("file_name <> ''", name="file_name_not_blank"),
        CheckConstraint(
            f"char_length(content_hash) = {SHA256_HEX_LENGTH}", name="content_hash_is_sha256"
        ),
        CheckConstraint("nature IN (1, 2, 3, 9)", name="nature_known"),
        # Hai vế của cùng một sự thật: tờ hóa đơn gốc không có gì để trỏ ngược,
        # và một tờ mang đường trỏ ngược thì không phải tờ gốc. Viết thành ràng
        # buộc bảng để `nature = 1` mang nghĩa "không liên quan tới tờ nào" thay
        # vì "chưa ai điền" — cùng lối `adjustment_link_matches_kind` của
        # `sales_invoices`.
        CheckConstraint(
            "nature = 1 OR (related_serial IS NOT NULL AND related_no IS NOT NULL) OR nature = 9",
            name="related_only_when_not_original",
        ),
        CheckConstraint(
            "nature <> 1 OR (related_form IS NULL AND related_serial IS NULL "
            "AND related_no IS NULL AND related_date IS NULL)",
            name="original_has_no_related_invoice",
        ),
        # Khóa ngoại **ghép** như `einvoices.source_voucher_id`: chứng từ lập ra
        # từ tờ hóa đơn phải thuộc đúng chi nhánh đã nạp nó, và lệch chi nhánh
        # thì không biểu diễn được thay vì phải nhớ kiểm.
        #
        # `SET NULL` **trên đúng một cột**, chứ không `RESTRICT` và cũng không
        # `SET NULL` trơn. Ba lựa chọn, và hai cái kia đều hỏng:
        #
        # * `RESTRICT` biến mọi chứng từ mua lập từ hóa đơn đầu vào thành chứng
        #   từ **không xóa được** — `REFERENCE_GUARDS` không chạy ở
        #   `VoucherService.delete`, nên người dùng sẽ nhận một lỗi khóa ngoại
        #   trần từ cơ sở dữ liệu. Đó là một hồi quy đặt lên phân hệ mua, và gỡ
        #   nó đòi `einvoice` móc vào vòng đời chứng từ của `purchase` — đúng
        #   thứ luật C3 cấm.
        # * `SET NULL` trơn nhắm vào **cả hai** cột của khóa, mà `branch_id`
        #   `NOT NULL` — lượt xóa sẽ đổ ngay tại ràng buộc ấy.
        #
        # `ON DELETE SET NULL (voucher_id)` (PostgreSQL 15+) trả tờ hóa đơn về
        # đúng trạng thái đúng của nó — *chưa lập chứng từ*, vì chứng từ không
        # còn nữa — mà không đụng tới chi nhánh. Lượt xóa chứng từ vẫn nằm
        # nguyên trong nhật ký, nên không có gì bị giấu.
        ForeignKeyConstraint(
            ["voucher_id", "branch_id"],
            ["vouchers.id", "vouchers.branch_id"],
            name="fk_inbound_einvoices_voucher",
            ondelete="SET NULL (voucher_id)",
        ),
        Index("ix_inbound_einvoices_branch_seller", "branch_id", "seller_tax_code"),
        # Danh sách "còn tờ nào chưa vào sổ" — câu hỏi mở màn hình này tồn tại.
        Index(
            "ix_inbound_einvoices_pending",
            "branch_id",
            "invoice_date",
            postgresql_where=text("voucher_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)

    branch_id: Mapped[int] = mapped_column(
        ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False
    )
    """Chi nhánh **nhận** tờ hóa đơn — chi nhánh đang thao tác của người nạp.
    Nó quyết định ai còn nhìn thấy tờ này, và nó là vế mà MST người mua trên
    tờ hóa đơn phải khớp (xem `InboundInvoiceBuyerMismatchError`)."""

    seller_tax_code: Mapped[str] = mapped_column(String(TAX_CODE_MAX_LENGTH), nullable=False)
    seller_name: Mapped[str] = mapped_column(String(PARTY_NAME_MAX_LENGTH), nullable=False)
    seller_address: Mapped[str | None] = mapped_column(
        String(PARTY_ADDRESS_MAX_LENGTH), nullable=True
    )

    buyer_tax_code: Mapped[str] = mapped_column(String(TAX_CODE_MAX_LENGTH), nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(String(PARTY_NAME_MAX_LENGTH), nullable=True)
    buyer_address: Mapped[str | None] = mapped_column(
        String(PARTY_ADDRESS_MAX_LENGTH), nullable=True
    )

    invoice_form: Mapped[str] = mapped_column(String(INVOICE_FORM_MAX_LENGTH), nullable=False)
    """`KHMSHDon` — mẫu số hóa đơn. `NOT NULL` **vì nó nằm trong khóa nhận
    dạng**: một cột `NULL` được trong khóa duy nhất là một khóa không chặn gì
    (PostgreSQL coi hai `NULL` là khác nhau), nên tờ hóa đơn thiếu mẫu số sẽ
    nạp lại được vô hạn lần."""

    invoice_serial: Mapped[str] = mapped_column(String(INVOICE_SERIAL_MAX_LENGTH), nullable=False)
    """`KHHDon` — ký hiệu hóa đơn, ví dụ `C25TSV`."""

    invoice_no: Mapped[str] = mapped_column(String(INVOICE_NO_MAX_LENGTH), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    tax_authority_code: Mapped[str | None] = mapped_column(
        String(TAX_AUTHORITY_CODE_MAX_LENGTH), nullable=True
    )
    """`MCCQT` — mã cơ quan thuế. Rỗng ở hóa đơn **không có mã**, một loại hợp
    lệ của NĐ123, nên nó không bắt buộc."""

    currency_code: Mapped[str] = mapped_column(String(CURRENCY_CODE_LENGTH), nullable=False)
    exchange_rate: Mapped[Decimal] = mapped_column(
        Numeric(RATE_PRECISION, RATE_SCALE_DEFAULT), nullable=False
    )

    total_before_tax: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    total_vat: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
    """Ba con số **tờ hóa đơn tự khai**, giữ nguyên. Chúng là vế đối chứng của
    lượt lập chứng từ: chứng từ dựng từ các dòng phải cộng ra đúng chúng, nếu
    không thì có thứ gì đó chưa đọc được (xem `InboundInvoiceTotalsMismatchError`)."""

    nature: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """Tính chất tờ hóa đơn: 1 gốc · 2 thay thế · 3 điều chỉnh · 9 liên quan tới
    tờ khác nhưng không khai kiểu (`inbound_parser.INVOICE_NATURE_*`).

    Cột này là ranh giới giữa "một khoản mua" và "một lượt sửa khoản mua
    trước". Thiếu nó, một tờ **điều chỉnh giảm** — nhất quán từng đồng, nên lọt
    mọi phép kiểm số học — thành một chứng từ mua làm **tăng** chi phí và thuế
    đầu vào đúng phần đáng lẽ phải giảm."""

    related_form: Mapped[str | None] = mapped_column(String(INVOICE_FORM_MAX_LENGTH), nullable=True)
    related_serial: Mapped[str | None] = mapped_column(
        String(INVOICE_SERIAL_MAX_LENGTH), nullable=True
    )
    related_no: Mapped[str | None] = mapped_column(String(INVOICE_NO_MAX_LENGTH), nullable=True)
    related_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Danh tính tờ hóa đơn **bị** thay thế hoặc điều chỉnh (`TTHDLQuan`).

    Bốn trường chứ một chuỗi hiển thị: người dùng phải **tra được** tờ gốc
    trong sổ, và một chuỗi ghép sẵn thì không tra được. Rỗng ở hóa đơn gốc —
    ràng buộc `related_only_when_not_original` giữ hai vế không lệch nhau, nên
    `nature = 1` kèm một đường trỏ ngược là trạng thái không biểu diễn được."""

    voucher_id: Mapped[UUID | None] = mapped_column(nullable=True)
    """Chứng từ mua đã lập từ tờ hóa đơn này; `NULL` = chưa lập. Xem docstring
    đầu lớp về việc vì sao đây là **cột** duy nhất nói lên điều đó."""

    content_hash: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_name: Mapped[str] = mapped_column(String(FILE_NAME_MAX_LENGTH), nullable=False)
    """Tệp XML gốc nằm ở **kho định địa chỉ theo nội dung** của phase 2, cùng
    kho với bản thể hiện hóa đơn đầu ra — nên `pg_dump` hằng đêm không phải đọc
    lại chúng, và hai tệp cùng nội dung là một tệp trên đĩa. Không có cột
    `media_type`: bảng này chỉ nhận XML."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)


class InboundEInvoiceLine(DatasetBase):
    """Một dòng hàng của tờ hóa đơn đầu vào.

    **`vat_amount` được lưu, dù tờ hóa đơn không khai tiền thuế theo dòng.**
    Chuẩn TCT cộng thuế theo **nhóm thuế suất** (`THTTLTSuat`), nên tiền thuế
    từng dòng là kết quả một phép chia ngược — và phép chia ấy chạy **một lần**,
    lúc nạp, chứ không mỗi lần dựng chứng từ. Lý do: chia lại ở mỗi lượt gọi là
    hai đường sinh ra cùng một con số, và chúng sẽ lệch đúng vào lần ai đó sửa
    luật làm tròn ở một đường. Cách chia ghi ở `inbound.py`.

    **Không `Audited`.** Dòng là nội dung của tờ hóa đơn, mà tờ hóa đơn thì
    không sửa được — nó chỉ được nạp và bị xóa, và cả hai lượt ấy nhật ký của
    bảng cha đã ghi. Cùng lối `purchase_invoice_lines`.
    """

    __tablename__ = INBOUND_LINE_TABLE_NAME
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no", name="uq_inbound_einvoice_lines_no"),
        CheckConstraint("line_no > 0", name="line_no_positive"),
        CheckConstraint("description <> ''", name="description_not_blank"),
        CheckConstraint("amount >= 0 AND vat_amount >= 0", name="amounts_not_negative"),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="unit_price_not_negative"),
        CheckConstraint("vat_rate IS NULL OR vat_rate >= 0", name="vat_rate_not_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id: Mapped[UUID] = mapped_column(
        ForeignKey(f"{INBOUND_TABLE_NAME}.id", ondelete="CASCADE"), nullable=False
    )
    """`CASCADE`: dòng là dẫn xuất của tờ hóa đơn, không phải một lời khai đứng
    riêng — cùng lập luận `einvoice_representations`."""

    line_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    """`STT` trên tờ hóa đơn. Có thể đứt quãng: dòng ghi chú (`TChat` = 4)
    không mang tiền nên không được lưu, còn số thứ tự thì giữ nguyên như trên
    giấy để đối chiếu được bằng mắt."""

    description: Mapped[str] = mapped_column(String(LINE_DESCRIPTION_MAX_LENGTH), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(LINE_UNIT_MAX_LENGTH), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(QUANTITY_PRECISION, QUANTITY_SCALE), nullable=True
    )
    unit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(UNIT_PRICE_PRECISION, UNIT_PRICE_SCALE), nullable=True
    )
    """`NULL` được, và đó là ca **thường gặp**: hóa đơn điện, nước và nhiều hóa
    đơn dịch vụ chỉ khai thành tiền. Không chia ngược từ `amount / quantity` —
    xem `inbound_parser._line`."""

    amount: Mapped[Decimal] = mapped_column(Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False)
    vat_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(VAT_RATE_PRECISION, VAT_RATE_SCALE), nullable=True
    )
    """Thuế suất **phần trăm** (`10`, `8`, `5`) — cùng đơn vị với
    `purchase_invoice_lines.vat_rate`. `NULL` khi `TSuat` là một mã chữ."""

    vat_rate_text: Mapped[str | None] = mapped_column(
        String(VAT_RATE_TEXT_MAX_LENGTH), nullable=True
    )
    """Chuỗi `TSuat` nguyên văn. Tồn tại vì `vat_rate IS NULL` một mình không
    phân biệt được **KCT** (không chịu thuế) với **KKKNT** (không kê khai nộp
    thuế) — hai thứ khác nhau về quyền khấu trừ thuế đầu vào. Đây đúng là sự
    phân biệt mà chiều gửi buộc phải đánh mất ở biên giới nhà cung cấp
    (`xml_builder`), và chiều nhận không có lý do gì phải mất theo."""

    vat_amount: Mapped[Decimal] = mapped_column(
        Numeric(AMOUNT_PRECISION, AMOUNT_SCALE), nullable=False
    )
