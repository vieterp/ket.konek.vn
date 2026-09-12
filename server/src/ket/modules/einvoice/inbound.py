"""Nạp và giữ hóa đơn điện tử **đầu vào** (FR-EIV-040).

Tệp này làm đúng ba việc, và ranh giới của nó là chỗ đáng nói nhất:

* **ghi** một tờ hóa đơn đã phân giải vào sổ, kèm khử trùng và phép kiểm người
  mua;
* **đọc** lại tờ hóa đơn cùng các dòng của nó;
* **nối** tờ hóa đơn với chứng từ đã lập từ nó, và gỡ nối khi chứng từ bị xóa.

Thứ nó **không** làm là lập chứng từ mua. Luật C3 cấm `einvoice` import
`purchase`, và ở đây lệnh cấm ấy nói đúng bản chất chứ không chỉ đúng luật:
"dựng một chứng từ mua từ tờ hóa đơn này" là một **ca dùng** ghép hai phân hệ,
không phải một nhu cầu dữ liệu xuyên phân hệ. Chỗ của nó là tầng `api`, nơi
`routers/purchase.py` từ 7B đã ghép `purchase` với `cash_book` theo đúng lối
ấy — nên lát này **không** phải mở thêm một Protocol ở kernel (tức không phải
mở `frozen_kernel_api.txt`, tức không phải một ADR).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from psycopg.errors import UniqueViolation
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.config.catalog import MONEY_SCALE_KEY
from ket.kernel.config.settings_service import value_of
from ket.kernel.errors import (
    InboundInvoiceAlreadyLinkedError,
    InboundInvoiceBuyerMismatchError,
    InboundInvoiceDuplicateError,
    InboundInvoiceNotFoundError,
    InboundInvoiceXmlInvalidError,
    ReferenceNotFoundError,
)
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.master_data.tree_path import PATH_SEPARATOR
from ket.kernel.money import round_money
from ket.kernel.security.models import Branch
from ket.modules.einvoice.inbound_parser import (
    InboundInvoiceData,
    InboundLineData,
    VatGroupData,
)
from ket.modules.einvoice.models import (
    IDENTITY_CONSTRAINT,
    LINE_DESCRIPTION_MAX_LENGTH,
    InboundEInvoice,
    InboundEInvoiceLine,
)

_ZERO = Decimal(0)


class InboundEInvoiceService:
    """Sổ hóa đơn đầu vào, trong transaction của người gọi."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ đọc

    def require(self, invoice_id: UUID) -> InboundEInvoice:
        invoice = self._session.get(InboundEInvoice, invoice_id)
        if invoice is None:
            raise InboundInvoiceNotFoundError(
                "Không tìm thấy hóa đơn đầu vào", invoice=str(invoice_id)
            )
        return invoice

    def lines_of(self, invoice_id: UUID) -> list[InboundEInvoiceLine]:
        return list(
            self._session.scalars(
                select(InboundEInvoiceLine)
                .where(InboundEInvoiceLine.invoice_id == invoice_id)
                .order_by(InboundEInvoiceLine.line_no)
            )
        )

    def list_for(
        self, *, branch_ids: Sequence[int], pending_only: bool, limit: int, offset: int
    ) -> list[InboundEInvoice]:
        """Danh sách hóa đơn đầu vào, mới nhất trước.

        `pending_only` trả lời đúng câu hỏi làm nên màn hình này — *còn tờ nào
        chưa vào sổ* — và nó đọc thẳng `voucher_id IS NULL` chứ không một cột
        trạng thái nào khác, vì đó là chỗ duy nhất giữ sự thật ấy.
        """
        query = select(InboundEInvoice).where(InboundEInvoice.branch_id.in_(branch_ids))
        if pending_only:
            query = query.where(InboundEInvoice.voucher_id.is_(None))
        query = query.order_by(
            InboundEInvoice.invoice_date.desc(), InboundEInvoice.created_at.desc()
        )
        return list(self._session.scalars(query.limit(limit).offset(offset)))

    def vendor_id_for(self, tax_code: str) -> int | None:
        """Đối tác mang mã số thuế này, nếu tra ra **đúng một**.

        Nhiều dòng cùng mã số thuế trả `None` chứ không lấy dòng đầu: danh mục
        trùng là chuyện có thật, và đoán hộ ở đây là gán một khoản phải trả cho
        một trong hai đối tác mà người dùng không hề chọn. `None` đẩy câu hỏi
        về đúng chỗ trả lời được nó — lượt lập chứng từ, nơi người dùng chỉ
        định đối tác tường minh.
        """
        return self.vendor_ids_for((tax_code,)).get(tax_code)

    def vendor_ids_for(self, tax_codes: Sequence[str]) -> dict[str, int]:
        """Bảng tra mã số thuế → đối tác, cho **cả một trang** trong một lượt.

        Một lượt gọi cho mỗi dòng là 200 câu truy vấn cho một lưới 200 dòng —
        rẻ từng câu (`ix_partners_tax_code`), đắt ở số lượt đi về.

        Lọc `is_vendor` và còn hiệu lực: một dòng danh mục đã ngừng dùng mà tình
        cờ là dòng **duy nhất** mang mã số thuế ấy sẽ được chọn làm đối tượng
        phải trả mà người dùng không hề chọn — đúng thứ luật "không đoán hộ"
        ở đây sinh ra để tránh.
        """
        # Khóa trả về là **chuỗi người gọi đưa vào**, không phải dạng chuẩn hóa:
        # người gọi cầm `seller_tax_code` thô của từng dòng và không có lý do
        # gì phải biết luật chuẩn hóa của tệp này.
        by_normalized = {_normalized_tax_code(code): code for code in tax_codes}
        wanted = set(by_normalized)
        if not wanted:
            return {}
        found: dict[str, int] = {}
        clashed: set[str] = set()
        rows = self._session.execute(
            select(Partner.id, Partner.tax_code).where(
                Partner.tax_code.is_not(None),
                Partner.is_vendor.is_(True),
                Partner.is_active.is_(True),
            )
        )
        for partner_id, tax_code in rows:
            key = _normalized_tax_code(tax_code or "")
            if key not in wanted or key in clashed:
                continue
            if key in found:
                # Hai dòng cùng mã số thuế: danh mục trùng là chuyện có thật, và
                # lấy dòng đầu là gán một khoản phải trả cho một trong hai đối
                # tác mà người dùng không hề chọn.
                del found[key]
                clashed.add(key)
                continue
            found[key] = partner_id
        return {by_normalized[key]: partner_id for key, partner_id in found.items()}

    # ------------------------------------------------------------------ ghi

    def record(
        self,
        data: InboundInvoiceData,
        *,
        branch_id: int,
        content_hash: str,
        byte_size: int,
        file_name: str,
        user_id: int,
    ) -> InboundEInvoice:
        """Ghi một tờ hóa đơn đã phân giải vào sổ.

        Thứ tự ba bước không đổi được: kiểm người mua **trước** (một tờ hóa đơn
        của người khác thì không có lý do gì chiếm một khóa nhận dạng), chia
        tiền thuế xuống dòng, rồi mới ghi.
        """
        self._refuse_other_buyer(data.buyer_tax_code, branch_id=branch_id)
        scale = self._money_scale(user_id)
        invoice = InboundEInvoice(
            branch_id=branch_id,
            seller_tax_code=data.seller_tax_code,
            seller_name=data.seller_name,
            seller_address=data.seller_address,
            buyer_tax_code=data.buyer_tax_code,
            buyer_name=data.buyer_name,
            buyer_address=data.buyer_address,
            invoice_form=data.invoice_form,
            invoice_serial=data.invoice_serial,
            invoice_no=data.invoice_no,
            invoice_date=data.invoice_date,
            tax_authority_code=data.tax_authority_code,
            currency_code=data.currency_code,
            exchange_rate=data.exchange_rate,
            total_before_tax=data.total_before_tax,
            total_vat=data.total_vat,
            total_amount=data.total_amount,
            nature=data.nature,
            related_form=data.related_form,
            related_serial=data.related_serial,
            related_no=data.related_no,
            related_date=data.related_date,
            content_hash=content_hash,
            byte_size=byte_size,
            file_name=file_name,
            created_by=user_id,
        )
        try:
            # Savepoint chứ không `try` trần: một `IntegrityError` làm hỏng cả
            # transaction ở PostgreSQL, nên bắt nó mà không có điểm quay lui thì
            # mọi câu lệnh sau cũng đổ theo (cùng lối `representation._store`).
            with self._session.begin_nested():
                self._session.add(invoice)
                self._session.flush()
        except IntegrityError as error:
            # **Chỉ** đúng khóa nhận dạng, không phải mọi `IntegrityError`.
            # Lượt `INSERT` này còn vi phạm được tám ràng buộc khác của bảng
            # (`exchange_rate_positive`, `totals_not_negative`, sáu câu
            # `<> ''`), và nói với người dùng rằng một tờ hóa đơn có `TGia = 0`
            # "đã nạp vào sổ rồi" là câu tệ nhất có thể nói: họ đi tra sổ, không
            # thấy gì, rồi bỏ luôn tờ hóa đơn — một khoản mua không vào sổ và
            # một khoản thuế đầu vào mất quyền khấu trừ, không tiếng động nào.
            # Ràng buộc khác thì để `handle_integrity_error` gọi đúng tên nó.
            if not _is_identity_violation(error):
                raise
            # Khóa nhận dạng là thứ chặn **thật**, và nó phải là thứ chặn thật:
            # một phép tra trước lượt ghi chạy dưới RLS nên nó **không thấy** tờ
            # hóa đơn đã nạp ở chi nhánh khác — mà sổ thì chỉ có một.
            raise InboundInvoiceDuplicateError(
                "Tờ hóa đơn này đã nạp vào sổ rồi",
                seller_tax_code=data.seller_tax_code,
                invoice_no=data.invoice_no,
            ) from error

        for line, vat_amount in zip(data.lines, self._vat_of_lines(data, scale=scale), strict=True):
            self._session.add(self._line_row(invoice.id, line, vat_amount))
        self._session.flush()
        return invoice

    def link_voucher(self, invoice: InboundEInvoice, *, voucher_id: UUID) -> None:
        """Ghi nhận chứng từ đã lập từ tờ hóa đơn này — **một lượt ghi có điều kiện**.

        Phép so `invoice.voucher_id is not None` trong Python đúng nhưng không
        đủ, và chỗ nó hụt là chỗ đắt nhất: hai lượt bấm liên tiếp mang **hai
        khóa idempotency khác nhau**, nên `execute_once` không nối tiếp chúng;
        cả hai transaction đọc `voucher_id IS NULL` dưới READ COMMITTED, cả hai
        dựng một chứng từ mua, rồi lượt sau ghi đè đường trỏ của lượt trước. Kết
        quả: **hai** chứng từ mua cùng số hóa đơn nhà cung cấp, thuế đầu vào
        khấu trừ hai lần, và tờ hóa đơn chỉ trỏ vào một trong hai — đúng thứ mà
        docstring của bảng nói nó tồn tại để chặn.

        `UPDATE … WHERE voucher_id IS NULL` đẩy phép so xuống cơ sở dữ liệu, nơi
        lượt thứ hai chặn ở khóa dòng rồi thấy `rowcount = 0`. Chứng từ thừa của
        lượt thua vẫn phải cuốn lại — người gọi ném lỗi này ra ngoài
        `unit_of_work`, và lượt rollback ấy xóa nó cùng.
        """
        result = self._session.execute(
            update(InboundEInvoice)
            .where(InboundEInvoice.id == invoice.id, InboundEInvoice.voucher_id.is_(None))
            .values(voucher_id=voucher_id)
        )
        # `cast` vì `Session.execute` khai kiểu trả về rộng; một `UPDATE` thì
        # luôn là `CursorResult`, nơi `rowcount` có thật (cùng lối
        # `idempotency/service.prune_expired_keys`).
        if cast("CursorResult[Any]", result).rowcount == 0:
            raise InboundInvoiceAlreadyLinkedError(
                "Tờ hóa đơn này đã lập chứng từ rồi", invoice=str(invoice.id)
            )
        self._session.expire(invoice, ["voucher_id"])

    def delete(self, invoice: InboundEInvoice) -> None:
        """Xóa tờ hóa đơn đầu vào — chỉ khi nó chưa lập chứng từ nào.

        Chứng từ đã lập thì xóa chứng từ trước: khóa ngoại `SET NULL` trả tờ
        hóa đơn về "chưa lập chứng từ", rồi nó mới xóa được. Thứ tự ấy giữ cho
        không có chứng từ mua nào mồ côi tờ hóa đơn đã sinh ra nó.

        Tệp XML trên đĩa **ở lại**: kho định địa chỉ theo nội dung dùng chung
        cho cả bản thể hiện hóa đơn đầu ra, nên xóa tệp ở đây có thể xóa mất
        tệp mà một bản ghi khác đang trỏ tới. Lượt quét dọn tệp mồ côi của
        phase 11 là chỗ giải quyết, đúng như `einvoice_representations` đã ghi.
        """
        if invoice.voucher_id is not None:
            raise InboundInvoiceAlreadyLinkedError(
                "Xóa chứng từ đã lập trước, rồi mới xóa được tờ hóa đơn",
                invoice=str(invoice.id),
                voucher=str(invoice.voucher_id),
            )
        self._session.delete(invoice)
        self._session.flush()

    # -------------------------------------------------------------- nội bộ

    def _line_row(
        self, invoice_id: UUID, line: InboundLineData, vat_amount: Decimal
    ) -> InboundEInvoiceLine:
        if len(line.description) > LINE_DESCRIPTION_MAX_LENGTH:
            # Nói ở lượt nạp chứ không lúc lập chứng từ: xem
            # `LINE_DESCRIPTION_MAX_LENGTH` về việc hai trần phải bằng nhau.
            raise InboundInvoiceXmlInvalidError(
                f"Mô tả dòng {line.line_no} dài quá {LINE_DESCRIPTION_MAX_LENGTH} ký tự",
                reason=f"THHDVu dòng {line.line_no} quá dài",
            )
        return InboundEInvoiceLine(
            invoice_id=invoice_id,
            line_no=line.line_no,
            description=line.description,
            unit=line.unit,
            quantity=line.quantity,
            unit_price=line.unit_price,
            amount=line.amount,
            vat_rate=line.vat_rate,
            vat_rate_text=line.vat_rate_text,
            vat_amount=vat_amount,
        )

    def _money_scale(self, user_id: int) -> int:
        scale = value_of(self._session, key=MONEY_SCALE_KEY, user_id=user_id)
        if not isinstance(scale, int):  # pragma: no cover - catalog khai INTEGER
            raise RuntimeError(f"money.scale phải là số nguyên, nhận {scale!r}")
        return scale

    def _refuse_other_buyer(self, buyer_tax_code: str, *, branch_id: int) -> None:
        """Tờ hóa đơn phải xuất cho **chính đơn vị này**.

        So với mã số thuế của chi nhánh nhận **và của mọi cấp trên nó**: chi
        nhánh hạch toán phụ thuộc thường không khai mã số thuế riêng mà dùng mã
        của đơn vị chủ quản, nên so đúng một cấp sẽ từ chối những tờ hóa đơn
        hoàn toàn hợp lệ.

        Cả chuỗi không khai mã số thuế nào thì cũng **từ chối**, và đó là câu
        trả lời đúng chứ không phải một cổng quá chặt: phép kiểm này tồn tại để
        một tờ hóa đơn gửi nhầm không thành một khoản thuế GTGT đầu vào khấu
        trừ không căn cứ, và "chưa cấu hình nên cho qua" biến nó thành một phép
        kiểm không bao giờ chạy ở đúng những bản cài chưa khai gì.
        """
        branch = self._session.get(Branch, branch_id)
        if branch is None:  # pragma: no cover - phạm vi chi nhánh đã kiểm ở tầng HTTP
            raise ReferenceNotFoundError(
                "Không tìm thấy chi nhánh", entity="branches", key=str(branch_id)
            )
        ancestors = [int(part) for part in branch.path.split(PATH_SEPARATOR) if part]
        known = {
            _normalized_tax_code(code)
            for code in self._session.scalars(
                select(Branch.tax_code).where(Branch.id.in_(ancestors))
            )
            if code
        }
        if not known:
            raise InboundInvoiceBuyerMismatchError(
                "Chi nhánh nhận chưa khai mã số thuế nên không đối chiếu được người mua",
                branch=str(branch_id),
                reason="chi nhánh chưa khai mã số thuế",
            )
        if _normalized_tax_code(buyer_tax_code) not in known:
            raise InboundInvoiceBuyerMismatchError(
                "Tờ hóa đơn này xuất cho mã số thuế khác, không phải đơn vị nhận",
                branch=str(branch_id),
                buyer_tax_code=buyer_tax_code,
            )

    def _vat_of_lines(self, data: InboundInvoiceData, *, scale: int) -> tuple[Decimal, ...]:
        """Tiền thuế của **từng dòng**, chia ngược từ bảng tổng hợp thuế suất.

        Tờ hóa đơn TCT khai tiền thuế theo **nhóm thuế suất**, không theo dòng.
        Hai cách lấy số của dòng, và chỉ một cách cộng lại đúng:

        * nhân `amount × rate / 100` rồi làm tròn từng dòng — tổng lệch tổng
          khai vài đồng ngay khi nhóm có nhiều hơn một dòng, vì tổng của các số
          đã làm tròn khác số làm tròn của tổng;
        * **chia số nhóm đã khai** theo tỷ lệ tiền hàng, phần lẻ dồn về dòng
          lớn nhất (chọn) — tổng đúng bằng số tờ hóa đơn khai, theo cấu trúc.

        Cách thứ hai cũng chính là cách `purchase.landed_cost` phân bổ chi phí
        mua hàng. Không import lại được (luật C3), nhưng nó là cùng một luật kế
        toán và phải cho cùng kết quả, nên `_allocate` dưới đây chép đúng quy
        tắc ấy — kể cả quy tắc chọn dòng lớn nhất, đầu tiên khi bằng nhau.

        **Cộng các nhóm cùng thuế suất lại trước khi chia**, và đó là một bản
        sửa chứ không một lượt tối ưu. Nhà cung cấp tách `LTSuat` làm nhiều dòng
        cho cùng một thuế suất là chuyện thường (hàng và dịch vụ tách riêng), và
        `KHAC:10%` với `10%` cũng quy về một con số theo đúng thiết kế của
        `_vat_rate`. Chia từng nhóm rồi gán thẳng vào `shares` sẽ để nhóm sau
        **ghi đè** nhóm trước trên cùng những dòng ấy — hai nhóm 100 và 200 trên
        cùng thuế suất 10% cho ra tổng 200, tức 100 đồng thuế biến mất mà không
        một phép kiểm nào ở lượt nạp thấy.

        Nhóm không có dòng nào, hay dòng thuộc một thuế suất không có nhóm: cả
        hai để lại `0`. Chúng không tự sửa được ở đây, và hệ quả của chúng —
        tổng dòng không ra tổng tờ — là thứ phép kiểm lúc lập chứng từ nói ra,
        kèm cả hai con số để người dùng đối chiếu.
        """
        shares = [_ZERO] * len(data.lines)
        for rate, vat_amount in _vat_by_rate(data.vat_groups).items():
            indexes = [index for index, line in enumerate(data.lines) if line.vat_rate == rate]
            if not indexes:
                continue
            weights = [data.lines[index].amount for index in indexes]
            for index, share in zip(indexes, _allocate(vat_amount, weights, scale), strict=True):
                shares[index] = share
        return tuple(shares)


def _vat_by_rate(groups: Sequence[VatGroupData]) -> dict[Decimal | None, Decimal]:
    """Tiền thuế **đã cộng** theo từng thuế suất — xem `_vat_of_lines`.

    `dict` giữ nguyên thứ tự chèn, nên kết quả không phụ thuộc cách PostgreSQL
    hay Python sắp xếp `Decimal` với `None` — một thứ tự khác sẽ đổi dòng nào
    nhận phần lẻ làm tròn, tức đổi một con số tiền.
    """
    totals: dict[Decimal | None, Decimal] = {}
    for group in groups:
        totals[group.vat_rate] = totals.get(group.vat_rate, _ZERO) + group.vat_amount
    return totals


def _is_identity_violation(error: IntegrityError) -> bool:
    """Đúng khóa nhận dạng của tờ hóa đơn, không phải một ràng buộc nào khác.

    Đọc tên ràng buộc từ `diag` của psycopg, cùng nguồn mà
    `api/middleware/problem_details.handle_integrity_error` đọc — nên hai chỗ
    không thể bất đồng về việc ràng buộc nào vừa nổ.
    """
    original = error.orig
    if not isinstance(original, UniqueViolation):
        return False
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    return constraint == IDENTITY_CONSTRAINT


def _normalized_tax_code(raw: str) -> str:
    """Mã số thuế bỏ khoảng trắng, viết hoa.

    Không bỏ dấu gạch nối: `0123456789-001` là mã của một **đơn vị phụ thuộc**
    khác với `0123456789` của đơn vị chủ quản, và gộp hai mã ấy làm một sẽ cho
    hóa đơn xuất cho chi nhánh này khấu trừ ở chi nhánh kia.
    """
    return "".join(raw.split()).upper()


def _allocate(total: Decimal, weights: Sequence[Decimal], scale: int) -> tuple[Decimal, ...]:
    """Chia `total` theo `weights`, tổng đúng bằng `total`.

    Phần lẻ làm tròn dồn về dòng có trọng số lớn nhất (dòng đầu tiên nếu bằng
    nhau) — cùng quy tắc, cùng lý do với `purchase.landed_cost._proportional`:
    nó cho kết quả **xác định**, không phụ thuộc thứ tự nhập.

    **`total` KHÔNG được làm tròn trước.** Nó là con số tờ hóa đơn tự khai, và
    làm tròn nó về `money.scale` rồi chia là thay số của người bán bằng một số
    khác: ở bản cài chọn `money.scale = 0` (đồng Việt Nam không lẻ — một lựa
    chọn hợp lệ và `decided_once`), một nhóm khai `100.55` sẽ thành `101`, tổng
    dòng thành `1101` trong khi tờ hóa đơn nói `1100.55`, và phép kiểm tổng lúc
    lập chứng từ từ chối **vĩnh viễn** một tờ hóa đơn hoàn toàn bình thường.
    Chỉ các **phần chia** được làm tròn, còn phần dư thì dòng lớn nhất nhận —
    nên tổng luôn đúng bằng `total` ở mọi `scale`.
    """
    weight_sum = sum(weights, _ZERO)
    if not weights or total == _ZERO or weight_sum == _ZERO:
        return tuple(_ZERO for _ in weights)
    shares = [round_money(total * weight / weight_sum, scale) for weight in weights]
    largest = max(range(len(weights)), key=lambda index: weights[index])
    shares[largest] += total - sum(shares, _ZERO)
    return tuple(shares)
