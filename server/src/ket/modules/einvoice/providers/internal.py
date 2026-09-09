"""Nhà cung cấp `internal` — phát hành **không qua bên thứ ba**.

Đây không phải một bản giả cho test: nó là hành vi phát hành nội bộ mà 7D đã
dựng (cấp số + lật trạng thái, không gửi đi đâu), nay nói ra thành một adapter
để đường ghi của `outbox` chỉ có **một** hình dạng. Không có nó, `outbox.py`
phải mang một nhánh "nếu chưa cấu hình nhà cung cấp thì bỏ qua bước gửi" — đúng
loại nhánh mà registry sinh ra để không phải viết.

Nó cũng là người dùng thật đầu tiên của registry, nên cơ chế tra cứu adapter
được đo trên đường chạy thật ngay từ lát này thay vì chỉ trong test.

**Dùng thật ở đâu:** bản cài chưa ký hợp đồng với nhà cung cấp nào, hoặc dữ liệu
huấn luyện. Hóa đơn đi hết vòng đời trong phần mềm; số vẫn cấp gap-free và mọi
bất biến của 7D vẫn canh y nguyên. Nó **không** thay được nghĩa vụ pháp lý gửi
cơ quan thuế — 7E-2 cắm EasyInvoice vào đúng chỗ này.
"""

from __future__ import annotations

from uuid import UUID

from ket.modules.einvoice.providers.contracts import (
    IssueOutcome,
    PrepareOutcome,
    ProviderAcceptance,
    ProviderBinding,
    ProviderRecordState,
    ProviderStatus,
    RepresentationAvailability,
    RepresentationKind,
    RepresentationOutcome,
)
from ket.modules.einvoice.providers.registry import PROVIDERS

INTERNAL_PROVIDER_CODE = "internal"


class InternalProvider:
    """Nhận mọi tờ hóa đơn, ngay lập tức, không đi ra khỏi tiến trình.

    Vẫn đi qua **hai chặng** như nhà cung cấp thật dù không cần: một hợp đồng
    cho mọi adapter thì đường ghi của `reconcile` chỉ có một hình dạng, và chặng
    `prepare` ở đây rẻ đúng bằng một phép gán.
    """

    def prepare(self, *, client_ref: UUID, invoice_id: UUID) -> PrepareOutcome:
        """Khóa "của nhà cung cấp" ở đây chính là khóa của ta.

        Không có bên thứ ba nào sinh khóa, nên dùng lại `client_ref` là lời khai
        trung thực nhất — và nó giữ cho mọi bất biến đọc `provider_ref` vẫn đúng
        với adapter này.
        """
        return PrepareOutcome(acceptance=ProviderAcceptance.ACCEPTED, provider_ref=str(client_ref))

    def issue(self, *, provider_ref: str, invoice_id: UUID) -> IssueOutcome:
        """Không cấp số: hóa đơn phát hành nội bộ mang số của dãy `gap_free` cục
        bộ, đã cấp từ lúc xếp hàng (xem `service.issue`)."""
        return IssueOutcome(acceptance=ProviderAcceptance.ACCEPTED)

    def query_status(self, *, provider_ref: str) -> ProviderStatus:
        """Luôn `PREPARED` — "bản nháp còn đây, cứ phát hành đi".

        **Không** `ISSUED`: bịa ra nó sẽ đánh `done` một dòng mà chẳng ai gửi gì.
        **Không** `UNKNOWN`: đó là câu trả lời cho "tôi không biết khóa này", và
        `reconcile` đọc nó thành mâu thuẫn rồi để dòng nằm lại chờ người xử lý —
        vĩnh viễn, vì lượt sau cũng hỏi ra đúng thế. Mà `prepare` của chính
        adapter này thì **có** ghi khóa, nên mọi lượt thử lại đều đi qua đây.

        `PREPARED` là câu trả lời đúng theo nghĩa đen: không có gì rời phần mềm,
        nên tờ hóa đơn vẫn đang chờ được phát hành, và phát hành nó lần nữa
        không đụng tới ai. Đây là ngõ cụt mà review bắt được sau khi tách ba
        trạng thái — ở bản một chặng, câu trả lời cũ rơi xuống nhánh phát hành
        và `internal` nhận ngay.
        """
        return ProviderStatus(state=ProviderRecordState.PREPARED)

    def fetch_representation(
        self, *, provider_ref: str | None, kind: RepresentationKind
    ) -> RepresentationOutcome:
        """Hai câu trả lời khác hẳn nhau cho hai loại tệp.

        `XML` → `UNAVAILABLE`, và đó là câu trả lời **đúng theo luật**: hóa đơn
        phát hành nội bộ là hóa đơn đặt in hoặc tự in, bản gốc của nó là tờ
        giấy. Không có tệp XML ký số nào tồn tại, không phải "chưa cài".

        `PDF` → `NOT_HOSTED`: bản thể hiện dựng được, chỉ là không phải ở đây.
        Engine in sống ở tầng `reporting` mà `modules` không import được (C5,
        tiền lệ biên bản kiểm kê 6E-2), nên nơi dựng là tầng `api` — xem
        `representation.ensure`.

        `provider_ref` **không đọc tới**, kể cả khi nó `None`: không có bên thứ
        ba nào giữ gì cho tờ hóa đơn này, nên câu trả lời không phụ thuộc vào
        việc đã có dòng hàng đợi nào chạy hay chưa.
        """
        if kind is RepresentationKind.XML:
            return RepresentationOutcome(
                availability=RepresentationAvailability.UNAVAILABLE,
                message="Hóa đơn đặt in / tự in không có tệp XML",
            )
        return RepresentationOutcome(
            availability=RepresentationAvailability.NOT_HOSTED,
            message="Bản thể hiện của hóa đơn phát hành nội bộ dựng tại chỗ",
        )


def _build(binding: ProviderBinding) -> InternalProvider:
    """Không dùng gì trong `binding` — nó không gọi ra ngoài và không có bí mật
    nào để giải mã. Vẫn đi qua nhà máy để registry chỉ có một hợp đồng."""
    return InternalProvider()


PROVIDERS.register(INTERNAL_PROVIDER_CODE, _build)
