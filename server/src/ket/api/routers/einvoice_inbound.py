"""Endpoint hóa đơn điện tử **đầu vào** (`/api/v1/einvoices/inbound/*`) — lát 7F-2b.

**Router riêng, và đó là chỗ luật phụ thuộc hiện ra.** Lượt "lập chứng từ mua
từ tờ hóa đơn" phải chạm cả `einvoice` lẫn `purchase`, mà luật C3 cấm hai module
nghiệp vụ nhìn thấy nhau. Tầng `api` thì được — C2 đặt nó **trên** `modules` —
và `routers/purchase.py` từ 7B đã ghép `purchase` với `cash_book` theo đúng lối
này. Nên phép ghép nằm ở đây, không ở một Protocol mới trong kernel: mở kernel
là mở `frozen_kernel_api.txt`, tức một ADR, cho một nhu cầu vốn không phải nhu
cầu dữ liệu xuyên module mà là một **ca dùng**.

**Router phải được `include` TRƯỚC `einvoice_router`.** Router kia có
`GET /api/v1/einvoices/{einvoice_id}`, nên đăng ký sau sẽ làm `inbound` bị đọc
thành một `einvoice_id` và mọi lượt gọi ở đây đổ ở phép ép UUID. Cùng vấn đề mà
router kia đã tự giải trong nội bộ bằng thứ tự khai `/error-flows`, `/outbox`
trước `/{einvoice_id}`.

**Chứng từ lập ra KHÔNG trả về trong phản hồi.** Thao tác ở đây là "tờ hóa đơn
này vào sổ", nên thứ đổi trạng thái là tờ hóa đơn, và `voucher_id` trong phản
hồi là đường client đi tiếp sang màn hình chứng từ mua. Trả nguyên thân chứng từ
sẽ buộc router này phụ thuộc hình dạng phản hồi của phân hệ mua — một ràng buộc
không mua lại được gì.
"""

from __future__ import annotations

import io
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.downloads import content_disposition
from ket.api.idempotency import idempotency_key_dependency
from ket.kernel.attachments import storage
from ket.kernel.errors import (
    AttachmentStorageNotConfiguredError,
    AttachmentTooLargeError,
    BranchNotInScopeError,
    EInvoiceRepresentationUnreachableError,
    InboundInvoiceAccountMissingError,
    InboundInvoiceNotOriginalError,
    InboundInvoiceTotalsMismatchError,
    InboundInvoiceVendorUnmatchedError,
)
from ket.kernel.idempotency.service import IdempotentRef, execute_once, fingerprint_of
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.einvoice import EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE
from ket.modules.einvoice.inbound import InboundEInvoiceService
from ket.modules.einvoice.inbound_parser import INVOICE_NATURE_ORIGINAL, parse_inbound_xml
from ket.modules.einvoice.models import InboundEInvoice, InboundEInvoiceLine
from ket.modules.einvoice.schemas import (
    InboundEInvoiceListOut,
    InboundEInvoiceOut,
    InboundLineOut,
    InboundPurchaseIn,
)

# Tầng API được phép nhìn cả hai module — luật C3 chỉ cấm module nhìn nhau.
from ket.modules.purchase.models import VendorInvoiceStatus
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from ket.posting.documents.models import Voucher
from ket.settings import Settings

INBOUND_PREFIX: Final[str] = "/api/v1/einvoices/inbound"
"""Tiền tố của nhóm, khai ở đây vì `main.py` cần nó để nới trần thân request cho
lượt tải tệp XML lên — cùng lý do và cùng lối với `ATTACHMENTS_PREFIX`."""

router = APIRouter(prefix=INBOUND_PREFIX, tags=["einvoice"])

XML_MEDIA_TYPE: Final[str] = "application/xml"

INBOUND_VIEW = permission_code(EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE, Action.VIEW)
INBOUND_CREATE = permission_code(EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE, Action.CREATE)
INBOUND_DELETE = permission_code(EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE, Action.DELETE)
INBOUND_PRINT = permission_code(EINVOICE_PERMISSION_MODULE, INBOUND_PERMISSION_CODE, Action.PRINT)

InboundReader = Annotated[AuthorizedRequest, Depends(require_permission(INBOUND_VIEW))]
InboundAuthor = Annotated[AuthorizedRequest, Depends(require_permission(INBOUND_CREATE))]
InboundRemover = Annotated[AuthorizedRequest, Depends(require_permission(INBOUND_DELETE))]
InboundPrinter = Annotated[AuthorizedRequest, Depends(require_permission(INBOUND_PRINT))]

IMPORT_ROUTE: Final[str] = "POST /api/v1/einvoices/inbound/import"
CREATE_PURCHASE_ROUTE: Final[str] = "POST /api/v1/einvoices/inbound/{id}/actions/create-purchase"
ImportKey = Annotated[str, Depends(idempotency_key_dependency(IMPORT_ROUTE))]
CreatePurchaseKey = Annotated[str, Depends(idempotency_key_dependency(CREATE_PURCHASE_ROUTE))]

MAX_PAGE_SIZE: Final[int] = 200
_ZERO = Decimal(0)

# Quyền ghi sổ của phân hệ MUA, không của phân hệ hóa đơn: chứng từ dựng ra ở
# đây là chứng từ mua, nên nó phải đi qua đúng những mã quyền mà một chứng từ
# mua lập bằng tay phải đi qua. Ngược lại thì `einvoice.inbound.create` thành
# một đường vòng lập chứng từ mua mà không cần quyền lập chứng từ mua.
PURCHASE_CREATE: Final[str] = permission_code("purchase", "invoice", Action.CREATE)


def _storage_root(settings: Settings) -> Path:
    """Thư mục kho tệp, hoặc lỗi nêu đúng cách bật — cùng khuôn `routers/attachments.py`."""
    if settings.attachments_dir is None:
        raise AttachmentStorageNotConfiguredError(
            "Bản cài chưa cấu hình thư mục tệp đính kèm (KET_ATTACHMENTS_DIR)"
        )
    return settings.attachments_dir


def _require_branch_in_scope(authorized: AuthorizedRequest, branch_id: int) -> None:
    if branch_id not in authorized.scope.branch_ids:
        raise BranchNotInScopeError(
            "Chi nhánh này không nằm trong phạm vi được gán cho tài khoản", branch=branch_id
        )


def _safe_file_name(raw: str | None) -> str:
    """Tên hiển thị của tệp — cùng luật `routers/attachments._safe_file_name`.

    Không dùng dựng đường dẫn (đường dẫn sinh từ hash), nhưng nó đi vào header
    `Content-Disposition`, và một dấu gạch chéo ở đó là thứ mỗi trình duyệt
    diễn giải một kiểu.
    """
    candidate = (raw or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(character for character in candidate if character.isprintable())
    return cleaned[:255] or "hoa-don-dau-vao.xml"


def _detail_of(service: InboundEInvoiceService, invoice: InboundEInvoice) -> InboundEInvoiceOut:
    """Một tờ hóa đơn kèm từng dòng, đối tác tra ngay tại chỗ."""
    return _row_of(
        invoice,
        vendor_id=service.vendor_id_for(invoice.seller_tax_code),
        lines=tuple(InboundLineOut.model_validate(line) for line in service.lines_of(invoice.id)),
    )


def _row_of(
    invoice: InboundEInvoice,
    *,
    vendor_id: int | None,
    lines: tuple[InboundLineOut, ...] = (),
) -> InboundEInvoiceOut:
    """Một dòng của lưới. `vendor_id` **luôn** truyền vào, kể cả khi nó rỗng.

    Không có mặc định `None` kèm một lượt tra dự phòng: "chưa khớp đối tác" là
    câu trả lời hợp lệ và hay gặp, nên một mặc định như thế sẽ biến đúng những
    dòng chưa khớp thành một câu truy vấn mỗi dòng — tức lượt tra gộp cho cả
    trang lặng lẽ mất tác dụng ở đúng chỗ nó được thêm vào để giúp.
    """
    base = InboundEInvoiceOut.model_validate(invoice)
    return base.model_copy(update={"vendor_id": vendor_id, "lines": lines})


@router.post(
    "/import",
    response_model=InboundEInvoiceOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": InboundEInvoiceOut, "description": "Lần gửi lại: tờ hóa đơn đã nạp"},
        409: {"description": "Tờ hóa đơn này đã có trong sổ"},
        422: {"description": "Tệp không đọc được, hoặc xuất cho mã số thuế khác"},
    },
)
def import_inbound_invoice(
    authorized: InboundAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: ImportKey,
    response: Response,
    file: Annotated[UploadFile, File()],
) -> InboundEInvoiceOut:
    """Nạp một tệp XML hóa đơn đầu vào chuẩn TCT (FR-EIV-040).

    Tệp ghi xuống kho **trước** khi mở transaction, cùng lối
    `POST /api/v1/attachments`: vân tay idempotency của lượt này gồm hash nội
    dung, nên không có cách nào biết hai lượt gửi có cùng một tệp trước khi đọc
    hết tệp. Kho định địa chỉ theo nội dung nên "tệp thừa" là **cùng một tệp**,
    không tốn thêm byte nào.

    Chi nhánh của tờ hóa đơn = chi nhánh đang thao tác của người nạp — nó quyết
    định ai còn nhìn thấy tờ này, và nó là vế mà mã số thuế người mua phải khớp.

    FR-EIV-041 (kéo hóa đơn đầu vào thẳng từ hệ thống nhà cung cấp) **chưa làm**:
    không có sandbox nào để chứng minh, cùng lập luận D3 đã dùng cho 7E-2.
    """
    root = _storage_root(settings)
    branch_id = authorized.scope.acting_branch_id
    if branch_id is None:
        raise BranchNotInScopeError(
            "Chọn chi nhánh đang thao tác (header X-Branch) trước khi nạp hóa đơn đầu vào",
            branch=None,
        )
    _require_branch_in_scope(authorized, branch_id)

    # Đọc trọn vào bộ nhớ một lần rồi dùng lại khối ấy cho cả hai việc: bộ phân
    # giải cần cả cây XML nên tệp dù sao cũng phải vào RAM, và `UploadFile` có
    # thể đã tràn sang một tệp tạm trên đĩa — tua nó về đầu là một lượt đọc đĩa
    # thứ hai cho dữ liệu đang nằm sẵn trong tay. Đọc dư **một** byte so với
    # trần để một tệp vượt trần vẫn chạm `store_stream`, nơi trần được canh
    # trong lúc ghi chứ không theo lời khai `Content-Length` của client.
    content = file.file.read(settings.attachment_max_bytes + 1)
    if len(content) > settings.attachment_max_bytes:
        # Chặn **trước** lượt phân giải: một tệp quá khổ đi qua đó sẽ đổ ở bộ
        # phân giải với câu "không phải XML đọc được" — đúng về hiện tượng
        # (khối byte bị cắt ngang), sai hẳn về nguyên nhân, và người dùng đi
        # sửa một tệp không hỏng. `store_stream` vẫn canh trần lần nữa lúc ghi;
        # ở đây chỉ là chỗ gọi đúng tên vấn đề.
        raise AttachmentTooLargeError(
            "Tệp hóa đơn vượt quá dung lượng cho phép",
            limit_bytes=settings.attachment_max_bytes,
        )
    # Phân giải **trước** khi cất tệp: một tệp không đọc được thì không có lý do
    # nào để lại một khối byte trên đĩa mà không bản ghi nào trỏ tới.
    data = parse_inbound_xml(content)
    stored = storage.store_stream(
        root,
        authorized.scope.dataset_schema,
        io.BytesIO(content),
        max_bytes=settings.attachment_max_bytes,
    )
    file_name = _safe_file_name(file.filename)

    def work(session: Session) -> tuple[InboundEInvoiceOut, IdempotentRef]:
        service = InboundEInvoiceService(session)
        invoice = service.record(
            data,
            branch_id=branch_id,
            content_hash=stored.content_hash,
            byte_size=stored.byte_size,
            file_name=file_name,
            user_id=authorized.scope.user_id,
        )
        return _detail_of(service, invoice), IdempotentRef(
            result_type=InboundEInvoice.__tablename__, result_id=str(invoice.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> InboundEInvoiceOut:
        service = InboundEInvoiceService(session)
        return _detail_of(service, service.require(UUID(ref.result_id)))

    body, created = execute_once(
        factory,
        authorized.scope,
        route_key=IMPORT_ROUTE,
        # Hash nội dung nằm trong vân tay: dùng lại một khóa cho **tệp khác** là
        # lỗi client và phải nổ ra, chứ không được im lặng trả tờ hóa đơn cũ.
        fingerprint=fingerprint_of(f"{branch_id}|{stored.content_hash}"),
        key=idempotency_key,
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return body


@router.get("", response_model=InboundEInvoiceListOut)
def list_inbound_invoices(
    authorized: InboundReader,
    factory: SessionFactory,
    pending_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InboundEInvoiceListOut:
    """Danh sách hóa đơn đầu vào trong phạm vi chi nhánh của người gọi."""
    with unit_of_work(factory, authorized.scope) as session:
        service = InboundEInvoiceService(session)
        branch_ids = tuple(authorized.scope.branch_ids)
        rows = service.list_for(
            branch_ids=branch_ids, pending_only=pending_only, limit=limit, offset=offset
        )
        pending = session.scalar(
            select(func.count())
            .select_from(InboundEInvoice)
            .where(
                InboundEInvoice.branch_id.in_(branch_ids),
                InboundEInvoice.voucher_id.is_(None),
            )
        )
        # Một lượt tra cho **cả trang**: gọi theo từng dòng là 200 câu truy vấn
        # cho một lưới 200 dòng — rẻ từng câu, đắt ở số lượt đi về.
        vendors = service.vendor_ids_for([row.seller_tax_code for row in rows])
        return InboundEInvoiceListOut(
            items=tuple(_row_of(row, vendor_id=vendors.get(row.seller_tax_code)) for row in rows),
            pending=pending or 0,
        )


@router.get("/{inbound_id}", response_model=InboundEInvoiceOut)
def get_inbound_invoice(
    inbound_id: UUID, authorized: InboundReader, factory: SessionFactory
) -> InboundEInvoiceOut:
    """Một tờ hóa đơn đầu vào kèm từng dòng hàng."""
    with unit_of_work(factory, authorized.scope) as session:
        service = InboundEInvoiceService(session)
        invoice = service.require(inbound_id)
        _require_branch_in_scope(authorized, invoice.branch_id)
        return _detail_of(service, invoice)


@router.get(
    "/{inbound_id}/xml",
    response_class=StreamingResponse,
    responses={
        200: {"content": {XML_MEDIA_TYPE: {}}, "description": "Tệp XML gốc người bán gửi"},
        404: {"description": "Không có tờ hóa đơn này"},
    },
)
def download_inbound_xml(
    inbound_id: UUID,
    authorized: InboundPrinter,
    factory: SessionFactory,
    settings: AppSettings,
) -> StreamingResponse:
    """Tải lại **đúng tệp** người bán gửi (nghĩa vụ lưu trữ, FR-NFR-023).

    Tệp gốc chứ không một bản dựng lại: thứ có giá trị đối chiếu với cơ quan
    thuế là tệp mang chữ ký số của người bán, và mọi lượt sinh lại đều làm mất
    chữ ký ấy.
    """
    root = _storage_root(settings)
    with unit_of_work(factory, authorized.scope) as session:
        invoice = InboundEInvoiceService(session).require(inbound_id)
        # Lớp phòng thủ thứ hai như mọi cửa đọc của dự án: RLS đã lọc tờ hóa đơn
        # ngoài phạm vi nên `require` ném 404 trước khi tới đây.
        _require_branch_in_scope(authorized, invoice.branch_id)
        content_hash = invoice.content_hash
        file_name = invoice.file_name

    # Phát luồng SAU khi transaction đóng, cùng lối cửa tải bản thể hiện: thân
    # tệp không phải dữ liệu của giao dịch, và giữ một kết nối của pool cho một
    # việc không còn chạm cơ sở dữ liệu là giữ nhầm thứ.
    path = storage.blob_path(root, authorized.scope.dataset_schema, content_hash)
    if not path.is_file():
        raise EInvoiceRepresentationUnreachableError(
            "Tệp hóa đơn có trong sổ nhưng không đọc được trên máy chủ",
            einvoice=str(inbound_id),
            kind="xml",
        )
    return StreamingResponse(
        storage.iter_blob(root, authorized.scope.dataset_schema, content_hash),
        media_type=XML_MEDIA_TYPE,
        headers={
            "Content-Disposition": content_disposition(file_name),
            "Content-Length": str(path.stat().st_size),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/{inbound_id}/actions/create-purchase",
    response_model=InboundEInvoiceOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": InboundEInvoiceOut, "description": "Lần gửi lại: chứng từ đã lập"},
        409: {"description": "Tờ hóa đơn này đã lập chứng từ rồi"},
        422: {"description": "Chưa khớp đối tác, hoặc tổng chứng từ lệch tổng tờ hóa đơn"},
    },
)
def create_purchase_from_inbound(
    inbound_id: UUID,
    payload: InboundPurchaseIn,
    authorized: InboundAuthor,
    factory: SessionFactory,
    settings: AppSettings,
    idempotency_key: CreatePurchaseKey,
    response: Response,
) -> InboundEInvoiceOut:
    """Lập chứng từ mua hàng từ một tờ hóa đơn đầu vào (FR-EIV-040).

    Hai nửa dữ liệu, và ranh giới giữa chúng là điều đáng nói: tờ hóa đơn nói
    **đã mua gì** (người bán, ký hiệu, số, ngày, từng dòng, thuế suất, tổng),
    thân request nói **hạch toán vào đâu**. Không con số tiền nào nhận lại từ
    client — nếu nhận thì phép kiểm tổng dưới đây chẳng còn gì để đối chứng.

    Đòi **cả** `einvoice.inbound.create` lẫn `purchase.invoice.create`: chứng
    từ dựng ra ở đây là chứng từ mua, nên nó phải đi qua đúng những mã quyền mà
    một chứng từ mua lập bằng tay phải đi qua.
    """
    with unit_of_work(factory, authorized.scope) as session:
        invoice = InboundEInvoiceService(session).require(inbound_id)
        _require_branch_in_scope(authorized, invoice.branch_id)
    authorized.access.require(PURCHASE_CREATE)

    def work(session: Session) -> tuple[InboundEInvoiceOut, IdempotentRef]:
        service = InboundEInvoiceService(session)
        target = service.require(inbound_id)
        vendor_id = payload.vendor_id or service.vendor_id_for(target.seller_tax_code)
        if vendor_id is None:
            raise InboundInvoiceVendorUnmatchedError(
                "Chưa biết người bán trên tờ hóa đơn là đối tác nào — chọn đối tác, "
                "hoặc khai mã số thuế ấy trong danh mục",
                seller_tax_code=target.seller_tax_code,
            )
        lines = service.lines_of(target.id)
        voucher = PurchaseInvoiceService(session).create(
            _purchase_payload(target, lines, payload, vendor_id=vendor_id),
            user_id=authorized.scope.user_id,
        )
        service.link_voucher(target, voucher_id=voucher.id)
        return _detail_of(service, target), IdempotentRef(
            result_type=Voucher.__tablename__, result_id=str(voucher.id)
        )

    def replay(session: Session, ref: IdempotentRef) -> InboundEInvoiceOut:
        service = InboundEInvoiceService(session)
        return _detail_of(service, service.require(inbound_id))

    body, created = execute_once(
        factory,
        authorized.scope,
        route_key=CREATE_PURCHASE_ROUTE,
        key=idempotency_key,
        fingerprint=fingerprint_of(f"{inbound_id}|{payload.model_dump_json()}"),
        work=work,
        replay=replay,
        ttl=timedelta(hours=settings.idempotency_ttl_hours),
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return body


def _purchase_payload(
    invoice: InboundEInvoice,
    lines: list[InboundEInvoiceLine],
    payload: InboundPurchaseIn,
    *,
    vendor_id: int,
) -> PurchaseInvoiceIn:
    """Thân chứng từ mua dựng từ tờ hóa đơn — kèm phép kiểm tổng.

    **Dòng 0 đồng không lên chứng từ.** Hàng khuyến mại (`TChat` = 2) khai giá
    0, mà `PurchaseInvoiceLineIn.amount_fc` đòi dương — đúng như thế, một dòng
    chứng từ không mang tiền là một dòng không có bút toán nào. Tờ hóa đơn vẫn
    giữ dòng ấy để đối chiếu, và nó không làm lệch tổng vì nó cộng vào 0.

    **Phép kiểm tổng là phép kiểm duy nhất giữ cho lượt này không ghi một số
    tiền khác số phải trả.** Nó đỏ ở ba ca — bộ đọc bỏ sót dòng, hóa đơn có
    chiết khấu thương mại trên tổng, hóa đơn có khoản phí khác — và cả ba đều
    không đoán hộ được. Người dùng lập chứng từ bằng tay cho tờ ấy; tệp XML vẫn
    nằm trong sổ.
    """
    if invoice.nature != INVOICE_NATURE_ORIGINAL:
        # Chặn **ở đây** chứ không ở lượt nạp (user chốt 2026-09-12): tờ hóa đơn
        # vẫn phải vào sổ vì nghĩa vụ lưu trữ không phân biệt loại, và nó phải
        # nhìn thấy được trong danh sách việc cần làm. Thứ không làm được là
        # biến nó thành dữ liệu kế toán — cùng chỗ mà hai ca "không dựng được
        # chứng từ" khác đã đứng (chưa khớp đối tác, lệch tổng).
        raise InboundInvoiceNotOriginalError(
            "Tờ hóa đơn này điều chỉnh hoặc thay thế một tờ khác — lập chứng từ bằng tay",
            invoice=str(invoice.id),
            nature=invoice.nature,
            related_no=invoice.related_no,
        )
    priced = [line for line in lines if line.amount > _ZERO]
    built = sum((line.amount + line.vat_amount for line in priced), _ZERO)
    if not priced or built != invoice.total_amount:
        raise InboundInvoiceTotalsMismatchError(
            "Tổng các dòng trên tờ hóa đơn không ra đúng tổng tiền thanh toán tờ hóa đơn khai",
            invoice=str(invoice.id),
            built_total=str(built),
            declared_total=str(invoice.total_amount),
        )
    if payload.vat_account_id is None and any(line.vat_amount > _ZERO for line in priced):
        # Nói ở đây chứ để `PurchaseInvoiceLineIn` nói: lỗi của nó là một
        # `ValidationError` của pydantic dựng **bên trong** handler, không phải
        # `RequestValidationError` của FastAPI — không handler nào bắt, nên nó
        # rơi thẳng vào lưới `500`. Một trường tùy chọn theo *dữ liệu* thì phải
        # được kiểm theo dữ liệu.
        raise InboundInvoiceAccountMissingError(
            "Tờ hóa đơn có thuế GTGT nên phải chỉ tài khoản thuế được khấu trừ",
            invoice=str(invoice.id),
            field="vat_account_id",
        )
    return PurchaseInvoiceIn(
        kind=payload.kind,
        operation_code=payload.operation_code,
        vendor_id=vendor_id,
        payable_account_id=payload.payable_account_id,
        branch_id=invoice.branch_id,
        document_date=invoice.invoice_date,
        posting_date=payload.posting_date or invoice.invoice_date,
        currency_code=invoice.currency_code,
        exchange_rate=invoice.exchange_rate,
        # Đã nhận hóa đơn — chính tờ hóa đơn ấy là thứ đang được nạp, nên thuế
        # GTGT đầu vào của chứng từ này có căn cứ khấu trừ (BR-PUR-02).
        vendor_invoice_status=VendorInvoiceStatus.RECEIVED,
        vendor_invoice_form=invoice.invoice_form,
        vendor_invoice_serial=invoice.invoice_serial,
        vendor_invoice_no=invoice.invoice_no,
        vendor_invoice_date=invoice.invoice_date,
        payment_term_id=payload.payment_term_id,
        description=payload.description,
        lines=tuple(
            PurchaseInvoiceLineIn(
                description=line.description,
                quantity=line.quantity,
                unit_price_fc=line.unit_price,
                amount_fc=line.amount,
                vat_rate=line.vat_rate,
                vat_amount_fc=line.vat_amount,
                account_id=payload.account_id,
                # Tài khoản thuế chỉ gắn khi dòng **có** tiền thuế:
                # `PurchaseInvoiceLineIn._line_sane` đòi có nó khi thuế dương,
                # và gắn thừa ở dòng không thuế là để một tài khoản 133 đứng
                # trên một dòng không có đồng thuế nào.
                vat_account_id=payload.vat_account_id if line.vat_amount > _ZERO else None,
            )
            for line in priced
        ),
    )


@router.delete("/{inbound_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_inbound_invoice(
    inbound_id: UUID, authorized: InboundRemover, factory: SessionFactory
) -> Response:
    """Xóa một tờ hóa đơn đầu vào chưa lập chứng từ.

    Đã lập chứng từ thì không: xóa chứng từ trước — khóa ngoại `SET NULL` trả
    tờ hóa đơn về "chưa lập chứng từ" — rồi tờ hóa đơn mới xóa được. Thứ tự ấy
    giữ cho không có chứng từ mua nào mồ côi tờ hóa đơn đã sinh ra nó.
    """
    with unit_of_work(factory, authorized.scope) as session:
        service = InboundEInvoiceService(session)
        invoice = service.require(inbound_id)
        _require_branch_in_scope(authorized, invoice.branch_id)
        service.delete(invoice)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
