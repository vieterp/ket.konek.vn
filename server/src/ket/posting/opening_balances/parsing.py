"""Đọc workbook số dư ban đầu thành dòng có kiểu — phần kiểm **không cần DB**.

Tách khỏi `service.py` theo đúng đường ranh dữ liệu: mọi phép kiểm ở đây chỉ
cần nội dung tệp cộng ba tham số của năm tài chính (đồng tiền hạch toán, số lẻ
tiền, ngày đầu năm) — nên chúng test được bằng bảng giá trị không cần
PostgreSQL. Phần cần tra cứu (tài khoản, đối tác, nhân viên) nằm ở `service.py`.

Kiểm bằng Python theo dòng chứ không SQL tập hợp trên bảng đệm như khung danh
mục (H82): số dư ban đầu là tệp **một-lần-mỗi-năm** cỡ vài trăm tới vài nghìn
dòng, không phải đường nhập 10.000 dòng chạy hằng tuần của FR-NFR-043. Mọi tra
cứu DB vẫn là truy vấn **một lượt cho cả tệp** (xem `service._Lookups`) — thứ
LD-14 thật sự cấm là N+1 và vòng lặp trên dữ liệu lớn, không phải vòng lặp trên
một tệp Excel nhỏ mà cái giá của SQL hóa là chép lại cả tầng staging cho một
hình dạng dòng khác.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import IO, Final

from ket.kernel.errors import ImportSheetMissingError
from ket.kernel.excel.descriptors import CellKind, ColumnDescriptor, TemplateDescriptor
from ket.kernel.excel.reader import ensure_structure, iter_rows
from ket.kernel.money import RATE_SCALE_DEFAULT, ZERO, convert_currency
from ket.posting.opening_balances.models import OpeningDetailKind
from ket.posting.opening_balances.report import OpeningImportReport, OpeningRowError
from ket.posting.opening_balances.template import SHEETS


def _parse_date_text(candidate: str) -> date | None:
    """ISO (Excel đổi ô ngày thành ISO qua `_cell_text`) hoặc ngày/tháng/năm gõ tay.

    Tự tách thay vì `strptime`: hàm kia dựng `datetime` naive (cổng DTZ canh),
    mà thứ cần ở đây chỉ là một `date`.
    """
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        pass
    for separator in ("/", "-"):
        parts = candidate.split(separator)
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            day, month, year = (int(part) for part in parts)
            if year >= 1000:
                try:
                    return date(year, month, day)
                except ValueError:
                    return None
    return None


NATURAL_DEBIT_KINDS: Final[frozenset[int]] = frozenset(
    {OpeningDetailKind.RECEIVABLE, OpeningDetailKind.EMPLOYEE_ADVANCE}
)
"""Nhóm mà bên **Nợ** là bên còn nợ — dòng Nợ mang chi tiết chứng từ, dòng Có là
khoản ứng trước không gắn hóa đơn."""


@dataclass(frozen=True)
class ParsedOpeningRow:
    """Một dòng đã qua kiểm hình thức, chờ kiểm tra cứu ở `service.py`."""

    kind: int
    sheet: str
    row: int
    account_code: str
    partner_code: str | None
    currency_code: str
    exchange_rate: Decimal
    invoice_no: str | None
    invoice_date: date | None
    due_date: date | None
    debit_fc: Decimal
    credit_fc: Decimal
    debit: Decimal
    credit: Decimal

    @property
    def is_natural_side(self) -> bool:
        """Dòng nằm ở bên "còn nợ" của nhóm — bên mang chi tiết chứng từ."""
        if self.kind in NATURAL_DEBIT_KINDS:
            return self.debit > ZERO
        return self.credit > ZERO


def _decimal_of(
    values: dict[str, str | None],
    column: ColumnDescriptor,
    *,
    scale: int,
    sheet: str,
    row: int,
    report: OpeningImportReport,
    failed: set[tuple[str, int]],
) -> Decimal:
    """Một ô số → `Decimal`, hoặc ghi lỗi và trả 0 để các phép kiểm sau còn chạy.

    Số lẻ vượt `scale` là **lỗi** chứ không phải làm tròn hộ (review 4C, H2):
    cột DB `NUMERIC(18,2)` sẽ tự tròn im lặng, và khi đó tổng cân đối mà báo
    cáo kiểm tính bằng số CHƯA tròn nói ngược với dữ liệu thật — banner
    FR-OPB-006 "đã cân" trong khi cổng toàn vẹn 4D đọc DB thấy lệch.
    """
    text = values.get(column.field)
    if text is None:
        return ZERO
    try:
        value = Decimal(text)
    except InvalidOperation:
        report.add(
            OpeningRowError(
                sheet=sheet,
                row=row,
                column=column.display_header,
                code="opening.not_a_number",
                message="Ô phải là số, dùng dấu chấm cho phần thập phân",
                value=text,
            )
        )
        failed.add((sheet, row))
        return ZERO
    if value < ZERO:
        report.add(
            OpeningRowError(
                sheet=sheet,
                row=row,
                column=column.display_header,
                code="opening.negative_amount",
                message="Số dư không được âm — ghi vào cột bên kia thay vì đổi dấu",
                value=text,
            )
        )
        failed.add((sheet, row))
        return ZERO
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > scale:
        report.add(
            OpeningRowError(
                sheet=sheet,
                row=row,
                column=column.display_header,
                code="opening.too_many_decimals",
                message=f"Số chỉ được tối đa {scale} chữ số thập phân",
                value=text,
            )
        )
        failed.add((sheet, row))
        return ZERO
    return value


def _length_errors(
    values: dict[str, str | None],
    descriptor: TemplateDescriptor,
    *,
    sheet: str,
    row: int,
    report: OpeningImportReport,
    failed: set[tuple[str, int]],
) -> None:
    """Chuỗi dài quá cột `varchar` là lỗi của lượt kiểm, không phải crash của lượt ghi.

    Không có phép kiểm này thì lượt kiểm báo "hợp lệ" cho một tệp mà lượt ghi
    chắc chắn đổ `StringDataRightTruncation` giữa transaction (review 4C, H1) —
    đúng kiểu lỗi H85 dựng ra để chống.
    """
    for column in descriptor.columns:
        if column.max_length is None or column.kind is not CellKind.TEXT:
            continue
        text = values.get(column.field)
        if text is not None and len(text) > column.max_length:
            _row_error(
                report,
                failed,
                sheet=sheet,
                row=row,
                column=column.display_header,
                code="opening.too_long",
                message=f"Quá {column.max_length} ký tự",
                value=text[: column.max_length] + "…",
            )


def _date_of(
    values: dict[str, str | None],
    column: ColumnDescriptor | None,
    *,
    sheet: str,
    row: int,
    report: OpeningImportReport,
    failed: set[tuple[str, int]],
) -> date | None:
    if column is None:
        return None
    text = values.get(column.field)
    if text is None:
        return None
    # `_cell_text` đổi ô ngày của Excel thành ISO datetime — cắt phần giờ trước.
    parsed = _parse_date_text(text.split("T")[0])
    if parsed is not None:
        return parsed
    report.add(
        OpeningRowError(
            sheet=sheet,
            row=row,
            column=column.display_header,
            code="opening.invalid_date",
            message="Không đọc được ngày — dùng dạng ngày/tháng/năm hoặc năm-tháng-ngày",
            value=text,
        )
    )
    failed.add((sheet, row))
    return None


def _row_error(
    report: OpeningImportReport,
    failed: set[tuple[str, int]],
    *,
    sheet: str,
    row: int,
    column: str | None,
    code: str,
    message: str,
    value: str | None = None,
) -> None:
    report.add(
        OpeningRowError(
            sheet=sheet, row=row, column=column, code=code, message=message, value=value
        )
    )
    failed.add((sheet, row))


def _parse_sheet(
    source: IO[bytes],
    kind: int,
    descriptor: TemplateDescriptor,
    *,
    base_currency: str,
    money_scale: int,
    fiscal_year_start: date,
    report: OpeningImportReport,
    failed: set[tuple[str, int]],
) -> list[ParsedOpeningRow]:
    sheet = descriptor.sheet_name
    partner_column = descriptor.column("partner_code") or descriptor.column("employee_code")
    invoice_no_column = descriptor.column("invoice_no")
    invoice_date_column = descriptor.column("invoice_date")
    due_date_column = descriptor.column("due_date")
    rate_column = _column(descriptor, "exchange_rate")

    rows: list[ParsedOpeningRow] = []
    source.seek(0)
    for row_no, values in iter_rows(source, descriptor):
        report.total_rows += 1
        _length_errors(values, descriptor, sheet=sheet, row=row_no, report=report, failed=failed)

        account_code = values.get("account_code")
        if account_code is None:
            _row_error(
                report,
                failed,
                sheet=sheet,
                row=row_no,
                column="Số hiệu tài khoản *",
                code="opening.required",
                message="Thiếu số hiệu tài khoản",
            )
        partner_code = values.get(partner_column.field) if partner_column is not None else None
        if partner_column is not None and partner_code is None:
            _row_error(
                report,
                failed,
                sheet=sheet,
                row=row_no,
                column=partner_column.display_header,
                code="opening.required",
                message=f"Thiếu {partner_column.header.lower()}",
            )

        debit_fc = _decimal_of(
            values,
            _column(descriptor, "debit_fc"),
            scale=money_scale,
            sheet=sheet,
            row=row_no,
            report=report,
            failed=failed,
        )
        credit_fc = _decimal_of(
            values,
            _column(descriptor, "credit_fc"),
            scale=money_scale,
            sheet=sheet,
            row=row_no,
            report=report,
            failed=failed,
        )
        debit = _decimal_of(
            values,
            _column(descriptor, "debit"),
            scale=money_scale,
            sheet=sheet,
            row=row_no,
            report=report,
            failed=failed,
        )
        credit = _decimal_of(
            values,
            _column(descriptor, "credit"),
            scale=money_scale,
            sheet=sheet,
            row=row_no,
            report=report,
            failed=failed,
        )

        currency = (values.get("currency_code") or base_currency).upper()
        rate_text = values.get("exchange_rate")
        exchange_rate = Decimal(1)
        parsed_rate = _decimal_of(
            values,
            rate_column,
            scale=RATE_SCALE_DEFAULT,
            sheet=sheet,
            row=row_no,
            report=report,
            failed=failed,
        )
        if currency == base_currency:
            # Đồng tiền hạch toán: tỷ giá luôn 1; cột nguyên tệ hoặc bỏ trống
            # hoặc chép đúng số quy đổi (quy ước của posting engine: dòng VND
            # có `debit_fc = debit`). So bằng SỐ chứ không bằng chuỗi — ô ghi
            # "1.00" là tỷ giá 1, không phải một tỷ giá lạ (review 4C, L4).
            if rate_text is not None and parsed_rate != Decimal(1):
                _row_error(
                    report,
                    failed,
                    sheet=sheet,
                    row=row_no,
                    column=rate_column.display_header,
                    code="opening.rate_for_base_currency",
                    message="Dòng đồng tiền hạch toán không có tỷ giá khác 1",
                    value=rate_text,
                )
            if (debit_fc != ZERO and debit_fc != debit) or (
                credit_fc != ZERO and credit_fc != credit
            ):
                _row_error(
                    report,
                    failed,
                    sheet=sheet,
                    row=row_no,
                    column=None,
                    code="opening.fc_on_base_currency",
                    message="Dòng đồng tiền hạch toán không có cột nguyên tệ khác số quy đổi",
                )
            debit_fc, credit_fc = debit, credit
        else:
            if rate_text is None or parsed_rate <= ZERO:
                _row_error(
                    report,
                    failed,
                    sheet=sheet,
                    row=row_no,
                    column=rate_column.display_header,
                    code="opening.rate_required",
                    message="Dòng ngoại tệ phải có tỷ giá dương",
                    value=rate_text,
                )
            else:
                exchange_rate = parsed_rate
                # Validator #9 của posting engine, áp cho số dư: cột quy đổi bỏ
                # trống thì server tự tính; đã điền thì phải bằng nguyên tệ ×
                # tỷ giá theo luật làm tròn của hệ thống — chỉnh tỷ giá của
                # dòng cho khớp thay vì chỉnh số tiền (cùng hợp đồng với
                # `JournalVoucherService._verify_client_amounts`).
                if values.get("debit") is None and debit_fc > ZERO:
                    debit = convert_currency(debit_fc, exchange_rate, money_scale)
                if values.get("credit") is None and credit_fc > ZERO:
                    credit = convert_currency(credit_fc, exchange_rate, money_scale)
                for side_header, fc_value, converted in (
                    ("Dư Nợ quy đổi", debit_fc, debit),
                    ("Dư Có quy đổi", credit_fc, credit),
                ):
                    if fc_value == ZERO and converted == ZERO:
                        continue
                    expected = convert_currency(fc_value, exchange_rate, money_scale)
                    if converted != expected:
                        _row_error(
                            report,
                            failed,
                            sheet=sheet,
                            row=row_no,
                            column=side_header,
                            code="opening.conversion_mismatch",
                            message=(
                                "Số quy đổi phải bằng nguyên tệ × tỷ giá "
                                f"(hệ thống tính ra {expected})"
                            ),
                            value=str(converted),
                        )

        has_debit = debit > ZERO or debit_fc > ZERO
        has_credit = credit > ZERO or credit_fc > ZERO
        if has_debit and has_credit:
            _row_error(
                report,
                failed,
                sheet=sheet,
                row=row_no,
                column=None,
                code="opening.both_sides",
                message="Mỗi dòng chỉ điền một bên Nợ hoặc Có — tách thành hai dòng",
            )
        if not has_debit and not has_credit:
            _row_error(
                report,
                failed,
                sheet=sheet,
                row=row_no,
                column=None,
                code="opening.no_amount",
                message="Dòng không có số dư nào",
            )

        invoice_no = values.get("invoice_no") if invoice_no_column is not None else None
        invoice_date = _date_of(
            values, invoice_date_column, sheet=sheet, row=row_no, report=report, failed=failed
        )
        due_date = _date_of(
            values, due_date_column, sheet=sheet, row=row_no, report=report, failed=failed
        )

        parsed = ParsedOpeningRow(
            kind=kind,
            sheet=sheet,
            row=row_no,
            account_code=account_code or "",
            partner_code=partner_code,
            currency_code=currency,
            exchange_rate=exchange_rate,
            invoice_no=invoice_no,
            invoice_date=invoice_date,
            due_date=due_date,
            debit_fc=debit_fc,
            credit_fc=credit_fc,
            debit=debit,
            credit=credit,
        )

        if (
            invoice_no_column is not None
            and (has_debit or has_credit)
            and not (has_debit and has_credit)
        ):
            _check_invoice_fields(
                parsed,
                invoice_no_column=invoice_no_column,
                invoice_date_column=invoice_date_column,
                fiscal_year_start=fiscal_year_start,
                report=report,
                failed=failed,
            )

        rows.append(parsed)
    return rows


def _check_invoice_fields(
    parsed: ParsedOpeningRow,
    *,
    invoice_no_column: ColumnDescriptor,
    invoice_date_column: ColumnDescriptor | None,
    fiscal_year_start: date,
    report: OpeningImportReport,
    failed: set[tuple[str, int]],
) -> None:
    """Chi tiết chứng từ theo bên của dòng (FR-OPB-003, BR-OPB-05).

    Bên "còn nợ" của nhóm bắt buộc mang số + ngày chứng từ — thiếu chúng thì
    đối trừ công nợ ở phase 7 không có gì để trừ vào. Bên ngược lại (ứng
    trước/trả trước) phải **trống**: một khoản ứng trước gắn số hóa đơn sẽ được
    gộp vào dòng cha như thể nó là nợ, và FR-OPB-007 lệch không ai hiểu vì sao.
    """
    if parsed.is_natural_side:
        if parsed.invoice_no is None:
            _row_error(
                report,
                failed,
                sheet=parsed.sheet,
                row=parsed.row,
                column=invoice_no_column.display_header,
                code="opening.invoice_no_required",
                message="Dòng còn nợ phải có số chứng từ để đối trừ công nợ về sau",
            )
        if parsed.invoice_date is None:
            column = invoice_date_column.display_header if invoice_date_column else None
            _row_error(
                report,
                failed,
                sheet=parsed.sheet,
                row=parsed.row,
                column=column,
                code="opening.invoice_date_required",
                message="Dòng còn nợ phải có ngày chứng từ",
            )
        elif parsed.invoice_date >= fiscal_year_start:
            _row_error(
                report,
                failed,
                sheet=parsed.sheet,
                row=parsed.row,
                column=invoice_date_column.display_header if invoice_date_column else None,
                code="opening.invoice_date_in_year",
                message=(
                    "Ngày chứng từ công nợ đầu kỳ phải trước ngày bắt đầu năm tài chính "
                    f"({fiscal_year_start.isoformat()})"
                ),
                value=parsed.invoice_date.isoformat(),
            )
    elif parsed.invoice_no is not None or parsed.invoice_date is not None:
        _row_error(
            report,
            failed,
            sheet=parsed.sheet,
            row=parsed.row,
            column=invoice_no_column.display_header,
            code="opening.invoice_on_counter_side",
            message="Dòng ứng trước/trả trước không ghi số chứng từ — để trống hai cột chứng từ",
        )


def _column(descriptor: TemplateDescriptor, field: str) -> ColumnDescriptor:
    column = descriptor.column(field)
    if column is None:  # pragma: no cover — mọi sheet đều khai đủ bốn cột số
        raise ValueError(f"Sheet {descriptor.sheet_name!r} thiếu cột {field!r}")
    return column


def parse_workbook(
    source: IO[bytes],
    *,
    base_currency: str,
    money_scale: int,
    fiscal_year_start: date,
    report: OpeningImportReport,
) -> tuple[list[ParsedOpeningRow], set[tuple[str, int]]]:
    """Đọc mọi sheet **có mặt** trong workbook; sheet vắng = nhóm không đụng tới.

    Trả về `(dòng đã đọc, tập (sheet, dòng) có lỗi)` — dòng lỗi vẫn nằm trong
    danh sách để các phép kiểm sau báo tiếp lỗi khác của cùng dòng (nguyên tắc
    trả-toàn-bộ-lỗi của phase 4), nhưng không bao giờ được ghi.
    """
    rows: list[ParsedOpeningRow] = []
    failed: set[tuple[str, int]] = set()
    seen_any_sheet = False
    for kind, descriptor in SHEETS.items():
        source.seek(0)
        try:
            ensure_structure(source, descriptor)
        except ImportSheetMissingError:
            continue
        seen_any_sheet = True
        rows.extend(
            _parse_sheet(
                source,
                kind,
                descriptor,
                base_currency=base_currency,
                money_scale=money_scale,
                fiscal_year_start=fiscal_year_start,
                report=report,
                failed=failed,
            )
        )
    if not seen_any_sheet:
        expected = ", ".join(repr(item.sheet_name) for item in SHEETS.values())
        raise ImportSheetMissingError(
            f"Tệp không có sheet số dư nào — cần ít nhất một trong: {expected}",
            expected=expected,
            found=[],
        )
    return rows, failed
