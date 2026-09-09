"""Client HTTP tới EasyInvoice — bốn lời gọi của luồng hóa đơn.

**`Status` trong thân JSON là câu trả lời thật, không phải mã HTTP.** Máy chủ
trả `200 OK` kèm `{"Status": 4, "Message": "..."}` cho một lượt từ chối, nên đọc
`raise_for_status()` rồi đi tiếp là coi mọi lỗi nghiệp vụ thành thành công.
`Status == 2` là nhận; mọi giá trị khác là từ chối kèm lý do.

**Phân biệt "bị từ chối" với "không rõ"** là việc của tệp này, và là lý do nó
không ném ngoại lệ chung cho cả hai. Một lượt hết thời gian chờ **không** phải
từ chối: yêu cầu có thể đã tới nơi và đã được cấp số. Ai gộp hai thứ ấy lại thì
lượt sau sẽ gửi lại một tờ hóa đơn đã phát hành (xem `reconcile.py`).

Không ghi log header: nó chứa mật khẩu dạng rõ (xem `auth.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx

READ_TIMEOUT_SECONDS: Final[int] = 30
"""Chờ tối đa cho một lượt gọi.

Rộng so với một API thường, và cố ý: bên kia còn phải ký số rồi gửi cơ quan
thuế. Ngắn hơn thì ta tự tạo ra đúng những lượt "không rõ kết quả" mà cả đường
`needs_reconcile` sinh ra để dọn.

Số **nguyên** giây, không phải `float`: ADR-015 cấm dấu phẩy động trong mã
nghiệp vụ, và một mốc chờ không cần độ phân giải dưới giây để đúng."""

CONNECT_TIMEOUT_SECONDS: Final[int] = 10
"""Chờ bắt tay. Ngắn hơn nhiều: không nối được thì chắc chắn chưa ai nhận gì,
nên đây là lượt hỏng **an toàn** — khác hẳn một lượt hết giờ khi đọc."""

TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)

ACCEPTED_STATUS: Final[int] = 2
"""Giá trị `Status` duy nhất nghĩa là nhà cung cấp đã nhận."""


class EasyInvoiceRefusedError(Exception):
    """Nhà cung cấp trả lời **rõ ràng** là không.

    Ngoại lệ riêng chứ không một `bool`: nơi gọi phải xử lý nó khác hẳn một lượt
    mất tín hiệu, và hai đường ấy không được phép trộn ở một chỗ rẽ nhánh.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class EasyInvoiceCredentials:
    base_url: str
    username: str
    password: str
    tax_code: str


class EasyInvoiceClient:
    """Bốn cửa: ba của luồng phát hành, một để tải bản thể hiện về.

    Không giữ trạng thái giữa các lượt gọi."""

    def __init__(
        self,
        credentials: EasyInvoiceCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport = transport

    def import_invoice(self, xml_data: str, *, pattern: str, serial: str) -> list[str]:
        """Nạp bản XML **chưa ký** làm hóa đơn nháp; trả về danh sách `Ikeys`."""
        data = self._post(
            "api/publish/importInvoice",
            {"XmlData": xml_data, "Pattern": pattern, "Serial": serial},
        )
        return _string_list(data.get("Ikeys"))

    def issue_invoices(self, ikeys: list[str], *, pattern: str, serial: str) -> dict[str, Any]:
        """Phát hành: nhà cung cấp ký bằng chứng thư người bán rồi gửi cơ quan thuế.

        Trả về nguyên `Data` vì hai thứ nơi gọi cần nằm ở hai nhánh khác nhau:
        `KeyInvoiceNo` ánh xạ `Ikey → số hóa đơn`, còn mã cơ quan thuế nằm trong
        `Invoices`.
        """
        return self._post(
            "api/publish/issueInvoices",
            {"Ikeys": ikeys, "Pattern": pattern, "Serial": serial},
        )

    def get_invoices_by_ikeys(self, ikeys: list[str]) -> list[dict[str, Any]]:
        """Tra cứu theo khóa chống trùng — cửa duy nhất giải một lượt mất tín hiệu."""
        data = self._post("api/publish/getInvoicesByIkeys", {"Ikeys": ikeys})
        records = data.get("__list__")
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
        return [data] if data else []

    def get_invoice_pdf(self, ikey: str, *, option: int) -> dict[str, Any]:
        """Tải bản thể hiện (`option=1`) hoặc tệp XML gốc (`option=2`).

        Trả nguyên `Data` vì nội dung về dưới dạng base64 trong `FileContent`
        kèm `FileName`, và việc giải mã thuộc về adapter — tệp này chỉ nói
        chuyện HTTP.
        """
        return self._post("api/publish/getInvoicePdf", {"Ikey": ikey, "Option": option})

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Một lượt gọi. Ném `EasyInvoiceRefusedError` khi bị từ chối rõ ràng.

        Lỗi mạng, hết giờ, thân trả về không phải JSON — **không** bắt ở đây:
        chúng nổi lên nguyên vẹn để `reconcile` đọc thành "không rõ kết quả".
        Nuốt chúng thành một giá trị trả về là xóa mất chính sự phân biệt ấy.
        """
        from ket.modules.einvoice.providers.easyinvoice.auth import build_header

        headers = {
            "Authorization": build_header(
                username=self._credentials.username,
                password=self._credentials.password,
                tax_code=self._credentials.tax_code,
            ),
            "Content-Type": "application/json",
        }
        url = f"{self._credentials.base_url.rstrip('/')}/{path}"
        with httpx.Client(timeout=TIMEOUT, transport=self._transport) as client:
            response = client.post(url, json=payload, headers=headers)
        body = response.json()
        if not isinstance(body, dict):
            raise EasyInvoiceRefusedError("EasyInvoice trả về thân không đúng khuôn")

        status = body.get("Status")
        if status != ACCEPTED_STATUS:
            message = body.get("Message") or body.get("Errors") or "Lỗi không xác định"
            code = body.get("ErrorCode") or ""
            raise EasyInvoiceRefusedError(
                f"EasyInvoice từ chối (Status={status}, Code={code}): {message}",
                status=status if isinstance(status, int) else None,
            )

        data = body.get("Data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            # `getInvoicesByIkeys` trả `Data` là **mảng**; gói lại để một hàm
            # `_post` phục vụ được cả hai hình dạng thay vì hai đường gần giống
            # nhau.
            return {"__list__": data}
        return {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
