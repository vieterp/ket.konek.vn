"""Dải tài khoản trong công thức BCTC: `1121`, `11*`, `131..138`.

Cả ba dạng đều so **theo tiền tố mã số** — hệ quả của quy ước hệ thống TK Việt
Nam (và luật mở TK con FR-SYS-020): mã TK con luôn nối dài mã TK cha (`1281` là
con của `128`). Vì thế "TK 128" trong một chỉ tiêu nghĩa là *cả nhánh 128*, và
số dư chỉ nằm ở TK hạch-toán-được (BR-SYS-03 cấm ghi thẳng vào TK tổng hợp) nên
khớp tiền tố không bao giờ đếm trùng cha–con. Đếm trùng duy nhất có thể xảy ra
là do **người soạn công thức** khai hai dải chồng nhau (`DR(128) + DR(1281)`) —
đó là lỗi dữ liệu gói, loader không đoán được ý nên không chặn.
"""

from __future__ import annotations

from dataclasses import dataclass

from ket.kernel.errors import StatementFormulaInvalidError


@dataclass(frozen=True)
class AccountRange:
    """Một dải TK đã kiểm: `high is None` = tiền tố đơn (dạng exact và `*` là
    một — `1121` và `1121*` cùng nghĩa "cả nhánh 1121"), ngược lại là khoảng
    `low..high` so tiền tố cùng độ dài."""

    low: str
    high: str | None = None

    def matches(self, account_code: str) -> bool:
        if self.high is None:
            return account_code.startswith(self.low)
        prefix = account_code[: len(self.low)]
        # Hai cận cùng độ dài (kiểm ở `make_range`), nên so chuỗi các tiền tố
        # chữ số cùng độ dài chính là so số.
        return len(prefix) == len(self.low) and self.low <= prefix <= self.high

    def spec(self) -> str:
        """Dạng văn bản để nhét lại vào thông điệp lỗi (`131..138`, `11*`)."""
        return self.low if self.high is None else f"{self.low}..{self.high}"


def make_range(low: str, high: str | None) -> AccountRange:
    """Dựng + kiểm một dải TK từ token của parser (chữ số đã được lexer bảo đảm)."""
    if high is not None:
        if len(high) != len(low):
            raise StatementFormulaInvalidError(
                "Hai cận của dải tài khoản phải cùng độ dài số hiệu",
                low=low,
                high=high,
            )
        if low > high:
            raise StatementFormulaInvalidError(
                "Cận dưới của dải tài khoản phải không lớn hơn cận trên",
                low=low,
                high=high,
            )
    return AccountRange(low=low, high=high)
