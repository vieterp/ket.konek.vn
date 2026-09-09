"""Bản thể hiện PDF và tệp XML của một tờ hóa đơn (FR-EIV-026).

**Tệp về là cất, không phải lấy rồi bỏ.** Nghĩa vụ lưu hóa đơn là mười năm, còn
hợp đồng với nhà cung cấp thì ngắn hơn thế nhiều — nên lượt tải đầu tiên ghi tệp
vào kho định địa chỉ theo nội dung (phase 2), và mọi lượt sau đọc từ đĩa. Sau
ngày ngừng dịch vụ, tờ hóa đơn vẫn mở được.

Đó cũng là lý do `einvoices` **không có cột `BYTEA` nào** — quyết định 7D, giữ
nguyên: một `pg_dump` hằng đêm (RT-03) không nên phải đọc lại vài chục GB tệp.

**Metadata nằm ở bảng RIÊNG `einvoice_representations`, không phải `attachments`
chung** — xem docstring của model về lỗ mà bản đầu của lát này mở ra. Tóm tắt:
bảng đính kèm nhận tệp từ một cửa HTTP mở cho mọi `entity_id`, nên nhận dạng
bản thể hiện trong không gian tên ấy là mời một tệp tùy ý chiếm chỗ vĩnh viễn.

**Không đi qua `outbox`.** Hàng đợi tồn tại để một lượt GỬI không đi hai lần, và
mỗi lượt gửi có thể cấp một số hóa đơn thật. Tải bản thể hiện là **đọc thuần**:
lặp lại bao nhiêu lần cũng không đụng tới ai, nên nó chạy thẳng trong request.

**Ai dựng bản thể hiện của hóa đơn phát hành nội bộ.** Không phải tệp này:
engine in sống ở `ket.reporting`, mà `modules` không import `reporting` (C5, và
tiền lệ biên bản kiểm kê 6E-2 — module dựng *dữ liệu* in, tầng `api` dựng *tờ
giấy*). Nên `ensure` nhận một hàm dựng do nơi gọi truyền vào, và chỉ gọi nó khi
adapter trả `NOT_HOSTED`. Quyết định "tự dựng hay không" vẫn nằm ở adapter, nơi
biết mình có giữ tệp hay không.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from pathlib import Path
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.kernel.attachments import storage
from ket.kernel.errors import (
    DomainError,
    EInvoiceRepresentationUnavailableError,
    EInvoiceRepresentationUnreachableError,
)
from ket.kernel.security.keystore import SecretBox
from ket.modules.einvoice.models import (
    FILE_NAME_MAX_LENGTH,
    EInvoice,
    EInvoiceOutbox,
    EInvoiceRepresentation,
    EInvoiceStatus,
)
from ket.modules.einvoice.providers.contracts import (
    EInvoiceProvider,
    ProviderBinding,
    RepresentationAvailability,
    RepresentationKind,
    RepresentationOutcome,
)
from ket.modules.einvoice.providers.registry import PROVIDERS
from ket.modules.einvoice.service import EInvoiceService

MEDIA_TYPES: Final[dict[RepresentationKind, str]] = {
    RepresentationKind.PDF: "application/pdf",
    RepresentationKind.XML: "application/xml",
}
"""Kiểu nội dung ta **tự đặt**, không lấy theo lời khai của nhà cung cấp.

Nó đi vào header lượt tải về. Nhận giá trị từ bên ngoài sẽ để một chuỗi ta không
kiểm soát quyết định trình duyệt làm gì với tệp."""

logger = structlog.get_logger(__name__)

LocalRenderer = Callable[[EInvoice], bytes]
"""Hàm dựng bản thể hiện tại chỗ — xem docstring đầu tệp về vì sao nó là tham số."""

_UNSAFE_FILE_NAME = re.compile(r'[\x00-\x1f\x7f"\\/]')
"""Ký tự không được sống trong một tên tệp đi ra header.

Ký tự điều khiển (gồm CR-LF) là đường **chèn header**; `"` và `\\` là ký tự cấu
trúc của giá trị trong ngoặc kép; `/` biến tên thành một đường dẫn. Tên do nhà
cung cấp đặt nên nó là dữ liệu ngoài, không phải hằng số của ta."""


def find(
    session: Session, *, einvoice_id: str, kind: RepresentationKind
) -> EInvoiceRepresentation | None:
    """Tệp đã cất cho hóa đơn này, nếu có.

    Tra theo cột `kind` — có `UNIQUE (einvoice_id, kind)` đằng sau — chứ không
    theo `media_type`. Bản đầu của lát này làm cách sau, và nó vừa không dựng
    được bất biến "mỗi loại một tệp", vừa để một chuỗi có thể tới từ ngoài
    quyết định phép nhận dạng.
    """
    return session.scalars(
        select(EInvoiceRepresentation).where(
            EInvoiceRepresentation.einvoice_id == einvoice_id,
            EInvoiceRepresentation.kind == kind.value,
        )
    ).one_or_none()


def ensure(
    session: Session,
    *,
    invoice: EInvoice,
    kind: RepresentationKind,
    storage_root: Path,
    dataset_schema: str,
    max_bytes: int,
    user_id: int,
    secret_box: SecretBox | None,
    render_locally: LocalRenderer,
) -> EInvoiceRepresentation:
    """Tệp bản thể hiện của tờ hóa đơn — lấy từ kho, hoặc lấy về rồi cất.

    Bốn câu trả lời của adapter dẫn tới bốn đường đi khác nhau, và không đường
    nào gộp được với đường khác:

    * `AVAILABLE` → cất rồi trả;
    * `NOT_HOSTED` → tự dựng bằng `render_locally`, rồi cất như trên;
    * `UNAVAILABLE` → 404, tệp này không tồn tại và sẽ không;
    * `UNKNOWN` → 503, chưa hỏi được, lượt sau có thể được.

    **Hóa đơn chưa phát hành thì chưa có bản thể hiện**, và đó là phép kiểm đầu
    tiên chứ không phải hệ quả của một lượt hỏi hụt: bản nháp chưa có số, chưa
    có ngày, và một tờ giấy in ra từ nó trông y hệt hóa đơn thật.

    Câu hỏi "có khóa nhà cung cấp chưa" **không** hỏi ở đây mà giao cho adapter
    (xem Protocol): `internal` không cần khóa nào để dựng bản thể hiện, nên chặn
    trước sẽ làm hóa đơn phát hành nội bộ chưa qua lượt bơm vĩnh viễn không in
    được.
    """
    einvoice_id = str(invoice.id)
    existing = find(session, einvoice_id=einvoice_id, kind=kind)
    if existing is not None:
        return existing

    if EInvoiceStatus(invoice.status) < EInvoiceStatus.DA_PHAT_HANH:
        raise EInvoiceRepresentationUnavailableError(
            "Hóa đơn chưa phát hành xong nên chưa có bản thể hiện",
            einvoice=einvoice_id,
            kind=kind.value,
        )

    provider = PROVIDERS.resolve(
        EInvoiceService(session).provider_code_for(invoice.id),
        ProviderBinding(session=session, secret_box=secret_box),
    )
    outcome = _ask(provider, provider_ref=_provider_ref(session, invoice), kind=kind)

    if outcome.availability is RepresentationAvailability.AVAILABLE:
        # `content` là `bytes` theo hợp đồng của `AVAILABLE`; `or b""` chỉ để
        # kiểu tĩnh khép lại, và nội dung rỗng đã bị chính adapter chặn từ trước.
        content = outcome.content or b""
        file_name = _safe_file_name(outcome.file_name) or _default_file_name(invoice, kind)
    elif outcome.availability is RepresentationAvailability.NOT_HOSTED:
        content = render_locally(invoice)
        file_name = _default_file_name(invoice, kind)
    elif outcome.availability is RepresentationAvailability.UNAVAILABLE:
        raise EInvoiceRepresentationUnavailableError(
            outcome.message or "Hóa đơn này không có tệp bản thể hiện loại đó",
            einvoice=einvoice_id,
            kind=kind.value,
        )
    else:
        raise EInvoiceRepresentationUnreachableError(
            outcome.message or "Chưa lấy được bản thể hiện từ nhà cung cấp — thử lại sau",
            einvoice=einvoice_id,
            kind=kind.value,
        )

    return _store(
        session,
        invoice=invoice,
        kind=kind,
        content=content,
        file_name=file_name,
        storage_root=storage_root,
        dataset_schema=dataset_schema,
        max_bytes=max_bytes,
        user_id=user_id,
    )


def _ask(
    provider: EInvoiceProvider, *, provider_ref: str | None, kind: RepresentationKind
) -> RepresentationOutcome:
    """Hỏi adapter, và biến **mọi** sự cố truyền dẫn thành `UNKNOWN`.

    Protocol khai rằng lỗi mạng nổi lên nguyên vẹn "để nơi gọi đọc thành
    `UNKNOWN`" — bản đầu của lát này viết đúng câu ấy rồi **không cài nơi gọi
    nào bắt**, nên một lượt hết giờ (ca phổ biến nhất) ra `500 "lỗi không mong
    muốn"` thay vì `503`, và cả nhánh `UNKNOWN` gần như không với tới được.

    Bắt rộng chứ không liệt kê từng lớp ngoại lệ, cùng lối `reconcile._ask`: thư
    viện HTTP, bộ giải JSON và bộ giải base64 ném ba họ ngoại lệ khác nhau, và
    một danh sách gõ tay là danh sách sẽ thiếu ở đúng lần bên kia hỏng theo kiểu
    mới. **Trừ `DomainError`**: "chưa khai hồ sơ đăng nhập nhà cung cấp" là việc
    người vận hành sửa được, và nuốt nó thành "thử lại sau" là giấu đi câu trả
    lời duy nhất có ích.
    """
    try:
        return provider.fetch_representation(provider_ref=provider_ref, kind=kind)
    except DomainError:
        raise
    except Exception:
        # **Ghi log, và ghi kèm vết ngoại lệ.** Bắt rộng mà im lặng thì một lỗi
        # lập trình trong adapter cũng thành "thử lại sau" — mãi mãi, và không
        # có gì trong log để ai đi tìm. Precedent `reconcile._ask` ít nhất còn
        # để lại `last_error` trên dòng hàng đợi mà người vận hành đọc; đường
        # này chạy trong một request và không để lại gì.
        logger.warning(
            "einvoice.representation.fetch_failed",
            provider_ref=provider_ref,
            kind=kind.value,
            exc_info=True,
        )
        # Thông điệp ra ngoài là câu **chung**, không phải văn bản ngoại lệ:
        # nó đi vào thân phản hồi API, và một `AttributeError` nội bộ hay message
        # thô của nhà cung cấp đều không phải thứ người dùng cuối cần đọc.
        return RepresentationOutcome(
            availability=RepresentationAvailability.UNKNOWN,
            message="Chưa hỏi được nhà cung cấp về bản thể hiện — thử lại sau ít phút",
        )


def _store(
    session: Session,
    *,
    invoice: EInvoice,
    kind: RepresentationKind,
    content: bytes,
    file_name: str,
    storage_root: Path,
    dataset_schema: str,
    max_bytes: int,
    user_id: int,
) -> EInvoiceRepresentation:
    """Ghi tệp xuống kho rồi ghi metadata — đúng thứ tự của `attachments`.

    Ghi đĩa trước, ghi bảng sau: hướng hỏng còn lại là một tệp không ai trỏ tới
    (vô hình, dọn được), còn hướng ngược lại để lại một dòng trỏ vào hư không.

    Hai lượt tải song song cùng trượt `find` là chuyện thường (người dùng bấm
    hai lần): cả hai ghi ra **cùng một tệp** vì kho định địa chỉ theo nội dung,
    rồi một lượt thua ở `uq_einvoice_representations_kind`. Lượt thua đọc lại
    bản ghi của lượt thắng thay vì báo lỗi — không có gì sai đã xảy ra.
    """
    stored = storage.store_stream(
        storage_root, dataset_schema, io.BytesIO(content), max_bytes=max_bytes
    )
    representation = EInvoiceRepresentation(
        einvoice_id=invoice.id,
        branch_id=invoice.branch_id,
        kind=kind.value,
        content_hash=stored.content_hash,
        byte_size=stored.byte_size,
        media_type=MEDIA_TYPES[kind],
        file_name=file_name,
        created_by=user_id,
    )
    try:
        # Savepoint chứ không `try` trần: một `IntegrityError` làm hỏng cả
        # transaction ở PostgreSQL, nên bắt nó mà không có điểm quay lui thì câu
        # đọc lại bên dưới cũng đổ theo (cùng lập luận `attachments.attach`).
        with session.begin_nested():
            session.add(representation)
            session.flush()
    except IntegrityError:
        winner = find(session, einvoice_id=str(invoice.id), kind=kind)
        if winner is None:
            raise
        return winner
    return representation


def _provider_ref(session: Session, invoice: EInvoice) -> str | None:
    """Khóa mà **nhà cung cấp** biết tờ hóa đơn này dưới cái tên đó.

    Đọc từ `einvoice_outbox` chứ không từ `einvoices`, vì đó là nơi nó thật sự
    sống: dòng hàng đợi là thứ đã nói chuyện với nhà cung cấp, nên khóa của nó
    chắc chắn là khóa họ nhận. Chép sang một cột thứ hai trên `einvoices` sẽ là
    đúng thứ mà 7D đã tránh với con số tiền — hai chỗ giữ một giá trị thì sớm
    muộn cũng lệch.

    Lấy dòng **mới nhất** có khóa: phát hành lại sau khi bị từ chối (ADR-013)
    để lại nhiều dòng cho cùng một tờ, và bản thể hiện thuộc về lượt cuối cùng
    tới nơi.
    """
    return session.scalars(
        select(EInvoiceOutbox.provider_ref)
        .where(
            EInvoiceOutbox.einvoice_id == invoice.id,
            EInvoiceOutbox.provider_ref.is_not(None),
        )
        .order_by(EInvoiceOutbox.id.desc())
        .limit(1)
    ).one_or_none()


def _safe_file_name(raw: str | None) -> str | None:
    """Tên do nhà cung cấp đặt, sau khi lọc — hoặc `None` nếu không còn gì.

    Tên này đi ra `Content-Disposition`, nên ký tự điều khiển và ký tự cấu trúc
    phải rụng ở đây chứ không ở tầng HTTP: bản đầu của lát này đưa thẳng nó vào
    header và một tên **tiếng Việt** (ca thường gặp của EasyInvoice) làm cả lượt
    tải đổ `500` — vĩnh viễn, vì tệp đã cất xong trước khi dựng phản hồi. Tầng
    HTTP vẫn mã hóa lần nữa (`filename*=UTF-8''…`); hai lớp trả lời hai câu hỏi
    khác nhau, và lớp này là lớp giữ cho **dữ liệu đã cất** luôn dùng được.
    """
    if raw is None:
        return None
    cleaned = _UNSAFE_FILE_NAME.sub("", raw).strip()[:FILE_NAME_MAX_LENGTH]
    return cleaned or None


def _default_file_name(invoice: EInvoice, kind: RepresentationKind) -> str:
    """Tên tệp khi nhà cung cấp không đặt tên, hoặc khi ta tự dựng.

    Số hóa đơn có thể còn `None` ở những trạng thái hợp lệ mà chưa có số (7E-2),
    nên nó không đứng một mình trong tên tệp được — sáu ký tự đầu của `id` là
    thứ luôn có và luôn phân biệt được.
    """
    suffix = "pdf" if kind is RepresentationKind.PDF else "xml"
    number = (invoice.invoice_no or "chua-co-so").replace("/", "-")
    return f"hoa-don-{number}-{invoice.id.hex[:6]}.{suffix}"
