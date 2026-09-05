"""Định giá dòng chứng từ sắp lập (FR-SAL §4.2, FR-SYS-042/043/045).

"Toàn bộ là dữ liệu, tính ở **server**; client chỉ hiển thị kết quả" (plan §Chính
sách giá & chiết khấu). Endpoint này là cách form mua/bán lấy đơn giá và tỷ lệ
chiết khấu **trước khi** có chứng từ nào được cất.

**`POST` chứ không `GET`** dù nó không ghi gì: đầu vào là chín tham số, trong đó
có ngày và ba id tùy chọn, và một `GET` với chín tham số truy vấn là chín chỗ để
client dựng URL sai mà không nhận được lỗi theo trường. Hệ quả có chủ đích là nó
không được cache — đúng thứ ta muốn ở một con số đổi theo bảng giá đang hiệu lực.

**Không** khai khóa idempotency, khác mọi `POST` khác: nó không tạo gì, nên thực
hiện hai lần không để lại hai thứ. Cổng `test_idempotency_route_coverage.py` chỉ
đòi khóa ở đường **ghi**.

**Mã quyền là `master.price_lists.view`.** Đường này đọc đúng dữ liệu ấy — bảng
giá, mức giá danh mục, bậc chiết khấu — và không đọc gì khác. Một mã quyền riêng
cho "được xem giá" sẽ tạo ra tình huống người dùng mở được màn bảng giá nhưng
không hỏi được giá từ chính nó, hoặc ngược lại — cùng lập luận đã ghi ở
`items_common.py` khi hai bảng con dùng lại mã quyền của mã hàng chủ.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends

from ket.api.dependencies import AuthorizedRequest, SessionFactory, require_permission
from ket.api.routers.price_list_lines import SPEC
from ket.api.routers.price_list_schemas import (
    PriceQuoteBatchRequest,
    PriceQuoteBatchResponse,
    PriceQuoteRequest,
    PriceQuoteResponse,
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.pricing import price_is_tax_inclusive_default, quote_price
from ket.kernel.security.permissions import Action

PREFIX: Final[str] = "/api/v1/pricing"

router = APIRouter(prefix=PREFIX, tags=["pricing"])

PriceReader = Annotated[
    AuthorizedRequest, Depends(require_permission(SPEC.permission_code(Action.VIEW)))
]


@router.post("/quote", response_model=PriceQuoteResponse, summary="Định giá một dòng chứng từ")
def quote(
    payload: PriceQuoteRequest,
    authorized: PriceReader,
    factory: SessionFactory,
) -> PriceQuoteResponse:
    """Đơn giá và chiết khấu theo ba tầng nguồn giá — xem `kernel.pricing`.

    Không tầng nào khai giá thì trả `source = "none"` kèm đơn giá `0`, **không**
    phải lỗi: mã hàng chưa khai giá là chuyện thường ngày và người lập chứng từ gõ
    tay đơn giá là đường hợp lệ.
    """
    with unit_of_work(factory, authorized.scope) as session:
        quoted = quote_price(
            session,
            item_id=payload.item_id,
            unit_id=payload.unit_id,
            quantity=payload.quantity,
            direction=payload.direction,
            on_date=payload.on_date,
            tax_inclusive_default=price_is_tax_inclusive_default(
                session, user_id=authorized.scope.user_id
            ),
            partner_id=payload.partner_id,
            contract_id=payload.contract_id,
            price_list_id=payload.price_list_id,
            level=payload.level,
            tax_rate=payload.tax_rate,
        )
        return PriceQuoteResponse.model_validate(quoted)


@router.post(
    "/quote-batch",
    response_model=PriceQuoteBatchResponse,
    summary="Định giá nhiều dòng chứng từ trong một lượt",
)
def quote_batch(
    payload: PriceQuoteBatchRequest,
    authorized: PriceReader,
    factory: SessionFactory,
) -> PriceQuoteBatchResponse:
    """Cùng luật với `/quote`, chạy cho cả chứng từ trong **một** transaction.

    Cái nó gộp là **request và transaction**, không phải truy vấn: form bán hàng
    (7H) hỏi giá cho cả hóa đơn bằng một lượt thay vì một lượt mỗi dòng, và tùy
    chọn "giá đã gồm thuế" cấp hệ thống đọc đúng **một lần** cho cả lô. Số truy
    vấn mỗi dòng vẫn nguyên — xem `PriceQuoteBatchRequest`, nơi ghi rõ phần nào
    của nợ N+1 lát 7C-1 đã hết và phần nào còn.

    Dòng nào không tầng giá nào trả lời được thì phần tử của nó mang `source =
    "none"` và đơn giá `0` — cùng luật với `/quote`, không phải lỗi và không
    làm hỏng cả lô: một mã hàng chưa khai giá không được phép chặn 49 dòng còn
    lại.
    """
    with unit_of_work(factory, authorized.scope) as session:
        tax_inclusive_default = price_is_tax_inclusive_default(
            session, user_id=authorized.scope.user_id
        )
        quoted = [
            quote_price(
                session,
                item_id=line.item_id,
                unit_id=line.unit_id,
                quantity=line.quantity,
                direction=line.direction,
                on_date=line.on_date,
                tax_inclusive_default=tax_inclusive_default,
                partner_id=line.partner_id,
                contract_id=line.contract_id,
                price_list_id=line.price_list_id,
                level=line.level,
                tax_rate=line.tax_rate,
            )
            for line in payload.lines
        ]
        return PriceQuoteBatchResponse(
            items=tuple(PriceQuoteResponse.model_validate(row) for row in quoted)
        )
