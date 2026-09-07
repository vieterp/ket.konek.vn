"""FR-EIV-035 — chứng từ gốc đứng yên khi hóa đơn của nó đã phát hành.

Một tờ hóa đơn đã phát hành đã ra khỏi phần mềm: nó mang số, đã ký, và (từ 7E)
đã tới cơ quan thuế. Sửa chứng từ bán hàng phía sau nó là để sổ nói một đằng và
tờ hóa đơn nói một nẻo — thứ chỉ lộ ra ở kỳ quyết toán. Đường đúng là xử lý ở
phía hóa đơn trước (thay thế / điều chỉnh / hủy), rồi mới quay lại chứng từ.

**Ba cửa phải chặn, và chúng không đi chung một chỗ:**

* **Bỏ ghi sổ** — `REFERENCE_GUARDS`, chạy trong `PostingService.unpost`, cửa
  mà mọi đường bỏ ghi sổ đều qua (endpoint hành động chung lẫn service từng
  module).
* **Sửa** và **xóa** chứng từ còn ở trạng thái Đã cất — `EDIT_GUARDS`, chạy đầu
  `VoucherService.ensure_editable`. Đây là cửa mà `REFERENCE_GUARDS` cố ý
  **không** canh (review 6G-2 M-4: guard sao kê chỉ canh chứng từ đã ghi sổ, và
  một lời gọi ở đường xóa nháp là mã chết). Guard của lát này thì ngược lại —
  nó **phải** canh chứng từ nháp, vì FR-EIV-011 cho phát hành hóa đơn ngay sau
  khi lập chứng từ, tức một chứng từ chưa ghi sổ vẫn có thể đã có hóa đơn thật.
  Cùng docstring ấy đặt điều kiện cho việc mở cửa này: kèm test chứng minh
  đường ấy tới được — `test_einvoice_source_guard.py` đi qua cả ba cửa.

Đăng ký từ phía module **giữ** ràng buộc (`einvoice`), không từ phía module bị
ràng buộc (`sales`): `sales` vì thế không phải biết phân hệ hóa đơn tồn tại, và
luật C3 (module không import module) vẫn đứng.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ket.kernel.errors import VoucherHasIssuedInvoiceError
from ket.modules.einvoice.service import EInvoiceService


def refuse_when_invoice_issued(session: Session, voucher_id: UUID) -> None:
    """Từ chối nếu chứng từ này đã có hóa đơn ra khỏi trạng thái nháp.

    Một truy vấn `LIMIT 1` trên `ix_einvoices_source_voucher`: guard chạy ở mọi
    lượt sửa, xóa và bỏ ghi sổ chứng từ của **mọi** phân hệ, nên nó phải rẻ kể
    cả ở bản cài không dùng hóa đơn điện tử — ở đó truy vấn trả rỗng ngay tại
    chỉ mục.
    """
    invoice = EInvoiceService(session).issued_for_voucher(voucher_id)
    if invoice is None:
        return
    raise VoucherHasIssuedInvoiceError(
        "Chứng từ này đã có hóa đơn điện tử phát hành nên không sửa, xóa hay bỏ "
        "ghi sổ được — xử lý ở phía hóa đơn (thay thế, điều chỉnh hoặc hủy) trước",
        invoice_no=invoice.invoice_no,
        einvoice_id=str(invoice.id),
    )
