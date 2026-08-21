"""Danh mục tùy chọn: khóa nào tồn tại, kiểu gì, mặc định bao nhiêu (FR-SYS-060).

Catalog **đóng** — chỉ khóa khai ở đây mới ghi được. Một bảng key-value mở là
nơi mọi thứ chưa kịp thiết kế sẽ rơi vào: ba phase nữa sẽ có vài chục khóa mà
không ai biết khóa nào còn được đọc, khóa nào là rác của một lần thử nghiệm, và
đổi kiểu của một khóa sẽ là canh bạc. Khai ở đây thì màn hình thiết lập dựng
được từ chính danh mục này, và `mypy` canh được nơi đọc.

Ràng buộc giá trị (`choices`, `minimum`, `maximum`) nằm cùng chỗ với khóa chứ
không nằm trong mã đọc nó: `money.scale = 9` phải bị từ chối **lúc ghi**, ở màn
hình thiết lập, chứ không phải nổ ra giữa một lần tính giá thành ba tháng sau.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from ket.kernel.errors import SettingValueInvalidError
from ket.kernel.money import MONEY_SCALE_MAX

SettingValue = str | int | bool | Decimal
"""Giá trị vô hướng của một tùy chọn. Không có cấu trúc lồng nhau — xem
docstring của `Setting` về lý do lưu chuỗi có kiểu thay vì JSONB."""


class SettingScope(StrEnum):
    """Cấp của một tùy chọn (BR-SYS-06)."""

    SYSTEM = "system"
    USER = "user"


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DECIMAL = "decimal"


TRUE_LITERAL: Final[str] = "true"
FALSE_LITERAL: Final[str] = "false"


@dataclass(frozen=True)
class SettingDefinition:
    """Một khóa tùy chọn và mọi thứ cần biết để hiển thị, kiểm và đọc nó."""

    key: str
    value_type: ValueType
    default: str
    """Mặc định lưu ở dạng **chuỗi**, cùng dạng với cột `settings.value`: một
    đường phân giải duy nhất cho cả giá trị mặc định lẫn giá trị đã lưu, nên
    không có nhánh nào chỉ chạy khi người dùng đã từng ghi."""

    scopes: frozenset[SettingScope]
    description: str
    choices: frozenset[str] | None = None
    minimum: int | None = None
    maximum: int | None = None

    decided_once: bool = False
    """Đổi được bất kỳ lúc nào, hay **chốt một lần** (nguyên tắc U14).

    Không phải chuyện trình bày: hai nhóm khác nhau ở chỗ hậu quả. Đổi ngôn ngữ
    giao diện thì màn hình vẽ lại; đổi số chữ số thập phân khi sổ đã có số dư thì
    mọi phép làm tròn từ đó về sau lệch với những gì đã ghi, mà không có gì trên
    màn hình nói ra điều đó.

    Khai **cạnh chính khóa** chứ không trong một bảng phân nhóm đặt ở tầng API:
    bảng phân nhóm là chỗ khóa thứ mười sẽ vắng mặt, và mặc định của vắng mặt
    luôn là nhóm sai (nhóm "đổi thoải mái")."""

    def __post_init__(self) -> None:
        # Mặc định phải tự đi qua chính bộ kiểm của nó — nếu không, một khóa
        # khai sai sẽ chỉ lộ ra khi có người đầu tiên đọc nó.
        parse_value(self, self.default)


def parse_value(definition: SettingDefinition, raw: str) -> SettingValue:
    """Chuỗi trong DB → giá trị có kiểu. Sai kiểu/ngoài ràng buộc → `422`."""
    match definition.value_type:
        case ValueType.STRING:
            if definition.choices is not None and raw not in definition.choices:
                raise SettingValueInvalidError(
                    "Giá trị không nằm trong danh sách cho phép",
                    key=definition.key,
                    allowed=", ".join(sorted(definition.choices)),
                )
            return raw
        case ValueType.INTEGER:
            try:
                number = int(raw)
            except ValueError:
                raise SettingValueInvalidError(
                    "Giá trị phải là số nguyên", key=definition.key, value=raw
                ) from None
            _check_bounds(definition, number)
            return number
        case ValueType.BOOLEAN:
            if raw not in (TRUE_LITERAL, FALSE_LITERAL):
                raise SettingValueInvalidError(
                    f"Giá trị phải là `{TRUE_LITERAL}` hoặc `{FALSE_LITERAL}`",
                    key=definition.key,
                    value=raw,
                )
            return raw == TRUE_LITERAL
        case ValueType.DECIMAL:
            try:
                # `Decimal(str)` chứ không bao giờ qua `float`: mọi con số của
                # hệ này là tiền hoặc tỷ lệ dùng để tính tiền (LD-03).
                return Decimal(raw)
            except InvalidOperation:
                raise SettingValueInvalidError(
                    "Giá trị phải là số thập phân", key=definition.key, value=raw
                ) from None


def _check_bounds(definition: SettingDefinition, number: int) -> None:
    if definition.minimum is not None and number < definition.minimum:
        raise SettingValueInvalidError(
            "Giá trị nhỏ hơn mức tối thiểu", key=definition.key, minimum=definition.minimum
        )
    if definition.maximum is not None and number > definition.maximum:
        raise SettingValueInvalidError(
            "Giá trị lớn hơn mức tối đa", key=definition.key, maximum=definition.maximum
        )


MONEY_SCALE_KEY: Final[str] = "money.scale"
LOCALE_KEY: Final[str] = "ui.locale"
GRID_ENTER_KEY: Final[str] = "ui.grid_enter_moves_to_next_row"
SAVE_ALSO_POSTS_KEY: Final[str] = "posting.save_also_posts"

CASH_BALANCE_WARNING_KEY: Final[str] = "warning.cash_balance"
WARNING_LEVEL_NONE: Final[str] = "none"
WARNING_LEVEL_WARN: Final[str] = "warn"
WARNING_LEVEL_BLOCK: Final[str] = "block"
WARNING_LEVELS: Final[frozenset[str]] = frozenset(
    {WARNING_LEVEL_NONE, WARNING_LEVEL_WARN, WARNING_LEVEL_BLOCK}
)
"""Ba mức của một cảnh báo FR-SYS-062 (`docs/srs/01` §8.2) — bộ giá trị dùng
chung cho MỌI khóa `warning.*` về sau (vượt dự toán, vượt ngưỡng nợ…), để màn
thiết lập vẽ cùng một bộ ba lựa chọn cho cả nhóm."""

TREASURER_ENABLED_KEY: Final[str] = "treasurer.enabled"

PRINT_ALLOW_DRAFT_KEY: Final[str] = "print.allow_draft_vouchers"
PRINT_ALLOW_LOCKED_KEY: Final[str] = "print.allow_locked_vouchers"
QUANTITY_DECIMALS_KEY: Final[str] = "format.quantity_decimals"
REPORT_FONT_SIZE_KEY: Final[str] = "report.font_size_pt"
REPORT_LOGO_HASH_KEY: Final[str] = "report.logo_content_hash"
REPORT_LOGO_MEDIA_KEY: Final[str] = "report.logo_media_type"
REPORT_PDF_JOB_THRESHOLD_KEY: Final[str] = "report.pdf_job_threshold_rows"
REPORT_XLSX_JOB_THRESHOLD_KEY: Final[str] = "report.xlsx_job_threshold_rows"

CATALOG: Final[dict[str, SettingDefinition]] = {
    definition.key: definition
    for definition in (
        SettingDefinition(
            key=MONEY_SCALE_KEY,
            value_type=ValueType.INTEGER,
            default="2",
            # Chỉ cấp hệ thống: số chữ số thập phân quyết định cách **làm tròn
            # khi ghi sổ** (FR-NFR-002, FR-SYS-064). Cho phép mỗi người một giá
            # trị nghĩa là hai kế toán viên nhập cùng một hóa đơn ra hai con số
            # khác nhau — đúng thứ mục tiêu chất lượng số một cấm.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Số chữ số thập phân khi làm tròn tiền",
            minimum=0,
            maximum=MONEY_SCALE_MAX,
            # Chốt một lần (U14): đổi khi sổ đã có số dư nghĩa là từ đó về sau
            # làm tròn theo một luật khác với những gì đã ghi — chênh lệch không
            # ai đối chiếu lại được vì cả hai bên đều "đúng theo cấu hình lúc đó".
            decided_once=True,
        ),
        SettingDefinition(
            key=LOCALE_KEY,
            value_type=ValueType.STRING,
            default="vi",
            scopes=frozenset({SettingScope.SYSTEM, SettingScope.USER}),
            description="Ngôn ngữ giao diện (FR-NFR-034)",
            choices=frozenset({"vi", "en"}),
        ),
        SettingDefinition(
            key=GRID_ENTER_KEY,
            value_type=ValueType.BOOLEAN,
            default=TRUE_LITERAL,
            # Thói quen bàn phím là thứ **thuộc về từng người** (FR-NFR-052):
            # người quen Excel muốn Enter xuống dòng, người quen phần mềm cũ
            # muốn Enter sang ô kế. Ép chung một giá trị là cách chắc chắn làm
            # chậm một nửa số người nhập liệu.
            scopes=frozenset({SettingScope.SYSTEM, SettingScope.USER}),
            description="Phím Enter chuyển xuống dòng kế trong lưới nhập liệu",
        ),
        SettingDefinition(
            key=PRINT_ALLOW_DRAFT_KEY,
            value_type=ValueType.BOOLEAN,
            default=TRUE_LITERAL,
            # FR-RPT-011: cho/không cho in chứng từ CHƯA ghi sổ. Mặc định cho
            # phép — bản in mang dấu BẢN NHÁP (watermark) nên không giả được
            # chứng từ thật; đơn vị muốn quy trình chặt hơn thì tắt.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Cho phép in chứng từ chưa ghi sổ (bản in mang dấu BẢN NHÁP)",
        ),
        SettingDefinition(
            key=PRINT_ALLOW_LOCKED_KEY,
            value_type=ValueType.BOOLEAN,
            default=TRUE_LITERAL,
            # FR-RPT-011: cho/không cho in chứng từ thuộc kỳ ĐÃ khóa. Mặc định
            # cho phép — chứng từ kỳ khóa là bất động, in lại phục vụ lưu trữ/
            # thanh tra là nhu cầu thật; tắt dành cho đơn vị coi mọi bản in
            # sau khóa sổ là bản phải xin phép.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Cho phép in chứng từ thuộc kỳ đã khóa sổ",
        ),
        SettingDefinition(
            key=QUANTITY_DECIMALS_KEY,
            value_type=ValueType.INTEGER,
            default="2",
            # FR-RPT-012: định dạng số riêng cho từng loại. v1 khai loại đầu
            # tiên có cột hiển thị (số lượng); đơn giá/tỷ giá/tỷ lệ vào cùng
            # dataset đầu tiên dùng chúng (phase 6–8) — cùng doctrine trì hoãn
            # với `STANDARD_PARAMS` (currency/org_unit_ids).
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Số chữ số thập phân hiển thị cho cột số lượng trên báo cáo",
            minimum=0,
            maximum=6,
        ),
        SettingDefinition(
            key=REPORT_FONT_SIZE_KEY,
            value_type=ValueType.INTEGER,
            default="9",
            # FR-RPT-010: cấu hình font trên bản in. Họ font là token thương
            # hiệu (Be Vietnam Pro, nhúng trong server để PDF giống nhau mọi
            # máy — quyết định 5C) nên thứ cấu hình được là CỠ chữ.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Cỡ chữ thân bản in PDF (pt)",
            minimum=8,
            maximum=12,
        ),
        SettingDefinition(
            key=REPORT_LOGO_HASH_KEY,
            value_type=ValueType.STRING,
            default="",
            # FR-RPT-010: logo trên báo cáo/chứng từ. Giá trị = `content_hash`
            # của một tệp đã tải lên kho đính kèm (content-addressed) — đọc
            # thẳng từ đĩa lúc render, KHÔNG qua bảng `attachments`: logo là
            # nhận diện của đơn vị, mọi chi nhánh cùng thấy, còn bảng
            # attachments nằm sau RLS chi nhánh. Rỗng = không logo.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="content_hash (SHA-256) của tệp logo in trên báo cáo; rỗng = không logo",
        ),
        SettingDefinition(
            key=REPORT_LOGO_MEDIA_KEY,
            value_type=ValueType.STRING,
            default="image/png",
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Kiểu nội dung của tệp logo",
            choices=frozenset({"image/png", "image/jpeg", "image/svg+xml"}),
        ),
        SettingDefinition(
            key=REPORT_PDF_JOB_THRESHOLD_KEY,
            value_type=ValueType.INTEGER,
            default="2000",
            # Bước 19 (FR-NFR-041/042/044): render PDF vượt ngưỡng dòng này thì
            # đi job nền thay vì giữ request HTTP. Mặc định 2.000 hạ từ phác
            # thảo 20.000 của 5C — spike S2 đo ~3.000 dòng đã ≈ 10s (đúng mốc
            # FR-NFR-041), và ngưỡng phải chừa dư địa cho máy đích yếu hơn máy
            # đo. Chỉnh được vì tốc độ máy chủ mỗi bản cài mỗi khác.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Số dòng mà từ đó kết xuất PDF chuyển sang chạy nền",
            # Sàn 1 chứ không phải một con số "hợp lý": ép mọi lượt render đi
            # job là một lựa chọn vận hành chính đáng (máy chủ yếu, giờ cao
            # điểm), và test công tắc cũng cần hạ được ngưỡng xuống đáy.
            minimum=1,
            maximum=1_000_000,
        ),
        SettingDefinition(
            key=REPORT_XLSX_JOB_THRESHOLD_KEY,
            value_type=ValueType.INTEGER,
            default="100000",
            # XLSX nhanh hơn PDF hai bậc (S2: 50.000 dòng ≈ 1,1s, constant
            # memory) nên ngưỡng cao hơn hẳn; tồn tại để báo cáo 200.000+ dòng
            # (FR-RPT-015) vẫn có đường chạy nền hủy được thay vì một request
            # treo nhiều giây.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Số dòng mà từ đó kết xuất XLSX chuyển sang chạy nền",
            minimum=1,
            maximum=5_000_000,
        ),
        SettingDefinition(
            key=CASH_BALANCE_WARNING_KEY,
            value_type=ValueType.STRING,
            default=WARNING_LEVEL_NONE,
            # FR-QUY-020/FR-SYS-062: "Chi quá số tồn tiền mặt/tiền gửi" — số dư
            # TK 111x/112x âm sau khi ghi sổ. Cấp hệ thống: hai kế toán viên
            # cùng một két tiền phải bị cùng một mức chặn. Mặc định "none" —
            # SRS 01 §8.2 không ấn định mức khởi đầu, và một bản cài mới nhập
            # liệu lùi ngày (chi trước, nhập phiếu thu sau) sẽ bị dội cảnh báo
            # trên dữ liệu chưa đủ; đơn vị bật "warn"/"block" ở màn Thiết lập
            # khi số dư đã vào nề nếp.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Cảnh báo khi chi quá số tồn tiền mặt/tiền gửi (ba mức FR-SYS-062)",
            choices=WARNING_LEVELS,
        ),
        SettingDefinition(
            key=TREASURER_ENABLED_KEY,
            value_type=ValueType.BOOLEAN,
            default=FALSE_LITERAL,
            # FR-WHK-021 (SRS 17 §1): doanh nghiệp không tách vai trò thủ quỹ
            # thì ẩn phân hệ — phiếu thu/chi vào thẳng sổ quỹ lúc ghi sổ kế
            # toán, không có hàng đợi. Mặc định **tắt** (phase-06 §Risk: DN nhỏ
            # là số đông; bật là lựa chọn của đơn vị có thủ quỹ riêng). Cấp hệ
            # thống: một két tiền không thể nửa phiếu chờ thủ quỹ nửa phiếu tự
            # vào sổ tùy người nhập.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Bật phân hệ thủ quỹ: phiếu thu/chi chờ thủ quỹ ghi sổ quỹ",
        ),
        SettingDefinition(
            key=SAVE_ALSO_POSTS_KEY,
            value_type=ValueType.BOOLEAN,
            default=FALSE_LITERAL,
            # Chế độ ghi sổ (FR-SYS-061, SRS 01 §8.2): "Cất đồng thời ghi sổ"
            # hay "Cất không ghi sổ". Cấp hệ thống, không theo người: hai kế
            # toán viên cùng một quy trình phải cho ra chứng từ ở cùng trạng
            # thái, nếu không tab "chưa ghi sổ" (U1) của mỗi người nói một kiểu.
            #
            # Mặc định **tắt** — Cất và Ghi sổ là hai bước tách bạch (SRS 00
            # §3.3). Bật là lựa chọn của đơn vị muốn bỏ bước duyệt trung gian.
            scopes=frozenset({SettingScope.SYSTEM}),
            description="Cất chứng từ thì ghi sổ luôn trong cùng một lần lưu",
        ),
    )
}


def definition_of(key: str) -> SettingDefinition | None:
    return CATALOG.get(key)
