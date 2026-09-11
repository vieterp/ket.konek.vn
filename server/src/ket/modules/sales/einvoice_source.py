"""Nguồn nội dung hóa đơn điện tử, phía bán hàng (ADR-022).

Trả lời đúng một câu hỏi: *chứng từ này in ra tờ hóa đơn thế nào*. Phân hệ hóa
đơn điện tử hỏi qua Protocol `EInvoiceSource` của kernel thay vì đọc bảng của
`sales` — C3 cấm, và cấm đúng.

**Tên hàng và đơn vị tính phân giải ở đây, không ở `einvoice`.** Chúng là hai
trường bắt buộc trên mọi dòng hóa đơn (NĐ123 §10) mà bút toán không giữ, và
việc chọn chúng là quyết định của phân hệ bán hàng: dòng tự gõ thì lấy chính
lời người bán viết, dòng theo vật tư thì lấy tên trong danh mục. Đẩy phép chọn
ấy sang `einvoice` là buộc nó tra danh mục vật tư của một phân hệ khác để đoán
lại điều `sales` đã biết.

**Một trường không mô tả tờ giấy: `adjusts_voucher_id`.** Nó có mặt vì phân hệ
hóa đơn điện tử phải phân biệt được một chứng từ **điều chỉnh** với một hóa đơn
bán thường trước khi treo tờ hóa đơn điều chỉnh lên nó — và loại nghiệp vụ
(`sales_invoices.kind`) là chuyện riêng của phân hệ này, C3 cấm bên kia hỏi
thẳng. Ràng buộc `adjustment_link_matches_kind` là thứ làm cho `None` ở đây
nghĩa **đúng** là "không phải chứng từ điều chỉnh", chứ không phải "chưa ai điền".

**Không phân giải phần mình không phải chủ:** người mua trả về dạng
`(partner_kind, partner_id)` chứ không phải tên và địa chỉ. Đối tác là danh mục
của `kernel`, nên nơi gọi đọc thẳng được — chép qua đây chỉ tạo một bản sao
chực trôi.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.contracts import PartnerKind
from ket.kernel.master_data.models.item import Item
from ket.kernel.master_data.models.unit_of_measure import UnitOfMeasure
from ket.kernel.protocols import (
    PROVIDERS,
    EInvoiceSourceDocument,
    EInvoiceSourceLine,
)
from ket.modules.sales.models import SalesInvoice, SalesInvoiceLine
from ket.posting.documents.models import Voucher


class SalesEInvoiceSource:
    """Bản cài của `EInvoiceSource` cho chứng từ bán hàng."""

    def read(self, session: Session, *, voucher_id: UUID) -> EInvoiceSourceDocument | None:
        invoice = session.get(SalesInvoice, voucher_id)
        if invoice is None:
            # Không phải chứng từ bán — câu trả lời bình thường, không phải lỗi:
            # 7F thêm nguồn hóa đơn lập trực tiếp cạnh bản cài này.
            return None
        # Ngày, tiền tệ và tỷ giá sống ở **thân chứng từ**, không ở thân hóa đơn
        # bán: `sales_invoices` là phần riêng của phân hệ, còn ba trường ấy dùng
        # chung cho mọi loại chứng từ nên `posting` giữ chúng (quyết định 7C-2 —
        # một loại chứng từ, một đường ghi sổ).
        header = session.get(Voucher, voucher_id)
        if header is None:
            return None

        rows = list(
            session.scalars(
                select(SalesInvoiceLine)
                .where(SalesInvoiceLine.voucher_id == voucher_id)
                .order_by(SalesInvoiceLine.line_no)
            )
        )
        return EInvoiceSourceDocument(
            partner_kind=PartnerKind.CUSTOMER,
            partner_id=invoice.customer_id,
            document_date=header.document_date,
            currency_code=header.currency_code,
            exchange_rate=header.exchange_rate,
            total_before_tax_fc=invoice.total_before_tax_fc,
            total_vat_fc=invoice.total_vat_fc,
            total_fc=invoice.total_fc,
            lines=tuple(self._line(session, row) for row in rows),
            adjusts_voucher_id=invoice.adjusts_voucher_id,
        )

    def _line(self, session: Session, row: SalesInvoiceLine) -> EInvoiceSourceLine:
        return EInvoiceSourceLine(
            description=self._description(session, row),
            unit=self._unit_name(session, row),
            quantity=row.quantity,
            unit_price_fc=row.unit_price_fc,
            discount_amount_fc=row.discount_amount_fc,
            amount_fc=row.amount_fc,
            vat_rate=row.vat_rate,
            vat_amount_fc=row.vat_amount_fc,
        )

    def _description(self, session: Session, row: SalesInvoiceLine) -> str:
        """Lời người bán viết thắng tên trong danh mục.

        Diễn giải trên dòng là thứ người lập chứng từ **cố ý** gõ đè — ghi
        "Dịch vụ tư vấn tháng 8/2026" thay cho tên vật tư chung chung là cách
        tờ hóa đơn nói đúng việc đã bán. Ưu tiên ngược lại sẽ lặng lẽ ném lời
        khai ấy đi ở đúng nơi nó quan trọng nhất.
        """
        if row.description:
            return row.description
        if row.item_id is not None:
            item = session.get(Item, row.item_id)
            if item is not None:
                return item.name
        # Không diễn giải, không vật tư: chứng từ ấy lẽ ra không qua nổi lượt
        # kiểm của chính `sales`. Ném ở đây thì hàng đợi kẹt vì một dòng dữ
        # liệu cũ, nên trả một chuỗi nói thẳng là thiếu — người đọc tờ hóa đơn
        # nháp nhìn thấy ngay, trước khi nó đi đâu.
        return "(chưa có diễn giải)"

    def _unit_name(self, session: Session, row: SalesInvoiceLine) -> str | None:
        if row.unit_id is None:
            return None
        unit = session.get(UnitOfMeasure, row.unit_id)
        return None if unit is None else unit.name


PROVIDERS.register_einvoice_source(SalesEInvoiceSource())
