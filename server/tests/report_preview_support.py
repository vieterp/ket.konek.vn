"""Bộ đọc kết quả `/reports/{code}/preview` dùng chung cho test báo cáo.

Tách ra ở lát 7G-2a theo đúng lời hẹn `test_purchase_reports` ghi lại khi nó dựng
bản thứ hai: "giữ bản riêng cho tới khi có tệp thứ ba cần nó". Tệp thứ ba là
`test_sales_reports`, và một bản chép thứ ba của bộ đọc ô tiền là bản chép thứ ba
của cùng cái bẫy định dạng — xem docstring `money`.

`test_cash_bank_book_reports` **vẫn giữ bản riêng**: `money` của nó đọc số âm
dạng ngoặc đơn `(1.234)` và cố ý bỏ phần thập phân, hai thứ bộ đọc này không làm.
Gộp nó vào đây là đổi khẳng định của một tệp đã giao, thuộc một lát khác.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal


class PreviewResult:
    """Thân trả về của `/preview` → dòng dữ liệu + tiêu đề nhóm + dòng tổng.

    Ô trong `cells` xếp đúng thứ tự `columns`, nên tra theo KHÓA CỘT thay vì cắt
    chuỗi: khẳng định về một cột tiền phải nói về đúng cột đó, không về ô thứ tám
    của một dòng đã định dạng.
    """

    def __init__(self, body: dict[str, object]) -> None:
        columns = body["columns"]
        assert isinstance(columns, list)
        self.column_keys = [str(column["key"]) for column in columns]
        rows = body["rows"]
        assert isinstance(rows, list)
        self.rows: list[dict[str, str]] = []
        self.headings: list[str] = []
        self.totals: list[dict[str, str]] = []
        for row in rows:
            cells = row.get("cells")
            if row["kind"] in ("group_header", "group_footer") and row.get("heading"):
                self.headings.append(str(row["heading"]))
            if row["kind"] in ("group_footer", "grand_total") and cells:
                self.totals.append(self._total_cells(row))
            if row["kind"] != "data" or not cells:
                continue
            self.rows.append(self._cells(cells))

    def _cells(self, cells: object) -> dict[str, str]:
        assert isinstance(cells, list)
        return {key: str(cell["text"]) for key, cell in zip(self.column_keys, cells, strict=True)}

    def _total_cells(self, row: dict[str, object]) -> dict[str, str]:
        """Dòng tổng KHÔNG thẳng cột với dòng dữ liệu.

        `presentation.total_cells` dựng một ô nhãn gộp `label_span` cột đầu, rồi
        mỗi cột từ `label_span` trở đi một ô. Zip nó với cả danh sách cột sẽ gán
        con số của cột tiền cho cột đầu tiên của layout — bài kiểm khi ấy đọc
        `KeyError`, hoặc tệ hơn, đọc con số của một cột khác.
        """
        cells = row["cells"]
        assert isinstance(cells, list)
        span = int(row.get("label_span", 0) or 0)
        return {
            key: str(cell["text"])
            for key, cell in zip(self.column_keys[span:], cells[1:], strict=True)
        }

    def texts(self) -> list[str]:
        return ["|".join(row.values()) for row in self.rows]

    def all_text(self) -> str:
        """Cả tiêu đề nhóm lẫn dòng dữ liệu — chiều gộp nằm trên TIÊU ĐỀ nhóm,
        không lặp lại ở từng dòng."""
        return "\n".join([*self.headings, *self.texts()])

    def total(self, key: str) -> Decimal:
        """Dòng tổng CUỐI của báo cáo — tổng từng nhóm nằm ở các dòng trước."""
        assert self.totals, "báo cáo không có dòng tổng nào"
        return money(self.totals[-1], key)

    def sum_of(self, key: str) -> Decimal:
        return sum((money(row, key) for row in self.rows), Decimal(0))


def money(row: dict[str, str], key: str) -> Decimal:
    """Ô tiền đã định dạng → `Decimal`. Ô rỗng = 0 (renderer bỏ trắng số 0).

    Định dạng của `kernel.formatting.format_money` là kiểu Việt Nam: dấu chấm
    phân cách nghìn, dấu **phẩy** phân cách thập phân, dấu trừ đứng trước. Bỏ cả
    hai dấu phân cách (cách viết đầu tiên của hàm này, lát 7G-1) biến
    `30.019.336,5` thành `300193365` — gấp mười lần, trên một cột tiền, và bài
    kiểm "báo cáo khớp sổ" đỏ vì chính bộ đọc của nó chứ không vì báo cáo.
    """
    text = row[key].strip().replace("\u00a0", "").replace(" ", "")
    if text in ("", "-"):
        return Decimal(0)
    return Decimal(text.replace(".", "").replace(",", "."))


Preview = Callable[..., PreviewResult]
