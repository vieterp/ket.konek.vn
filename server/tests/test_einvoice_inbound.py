"""Sổ hóa đơn điện tử đầu vào trên PostgreSQL thật (lát 7F-2b, FR-EIV-040).

Bộ phân giải đã có tệp riêng không cần DB (`test_einvoice_inbound_parser.py`).
Ở đây là những thứ chỉ đo được khi có bảng thật và một chứng từ thật:

* **Khử trùng bằng ràng buộc bảng.** Khóa `(MST người bán, mẫu số, ký hiệu, số)`
  là toàn bộ lý do bảng này là một bảng — thiếu nó thì cùng một tờ hóa đơn nạp
  hai lần thành hai chứng từ mua, tức khấu trừ thuế GTGT đầu vào hai lần.
* **Người mua phải là chính mình.** Một tờ hóa đơn gửi nhầm mà lọt vào sổ là
  một khoản thuế đầu vào khấu trừ không có căn cứ, và **không** phép kiểm toàn
  vẹn nào thấy: mọi con số đều cân, chỉ có người mua là người khác.
* **Tiền thuế từng dòng cộng lại đúng bằng số tờ hóa đơn khai.** Tờ hóa đơn TCT
  cộng thuế theo nhóm thuế suất, nên số của dòng là kết quả một phép chia
  ngược — và phép chia ấy phải đúng tới từng đồng, không xấp xỉ.
* **Phép kiểm tổng lúc lập chứng từ.** Nó là thứ duy nhất giữ cho lượt "lập
  chứng từ từ hóa đơn" không ghi một số tiền khác số phải trả.
* **Xóa chứng từ trả tờ hóa đơn về "chưa lập chứng từ"** — hành vi của
  `ON DELETE SET NULL (voucher_id)`, thứ không mô phỏng được ngoài PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from einvoice_support import inbound_lines_with, inbound_xml
from ket.kernel.datasets.provisioning import DatasetRef
from ket.kernel.errors import (
    InboundInvoiceAlreadyLinkedError,
    InboundInvoiceBuyerMismatchError,
    InboundInvoiceDuplicateError,
    InboundInvoiceXmlInvalidError,
)
from ket.kernel.master_data.models.partner import Partner
from ket.kernel.organization.service import BranchService
from ket.kernel.persistence.unit_of_work import RequestScope, unit_of_work
from ket.kernel.security.models import Branch
from ket.modules.einvoice.inbound import InboundEInvoiceService, _allocate
from ket.modules.einvoice.inbound_parser import InboundInvoiceData, parse_inbound_xml
from ket.modules.einvoice.models import InboundEInvoice
from ket.modules.purchase.schemas import PurchaseInvoiceIn, PurchaseInvoiceLineIn
from ket.modules.purchase.service import PurchaseInvoiceService
from posting_support import PostingContext, posting_scope, seed_posting_context
from purchase_support import ensure_vendor, seed_purchase_package_data

pytestmark = pytest.mark.db

ACTOR_ID = 1
OUR_TAX_CODE = "0312345678"
SELLER_TAX_CODE = "0101243150"

# Khối id 97xx là của các tệp hóa đơn điện tử; 976x thuộc riêng tệp này.
VENDOR_ID = 9761
UNKNOWN_VENDOR_ID = 9762
TWIN_VENDOR_ID = 9763

HASH = "a" * 64


@pytest.fixture(scope="module")
def context(session_factory: sessionmaker[Session], dataset_alpha: DatasetRef) -> PostingContext:
    return seed_posting_context(session_factory, dataset_alpha)


@pytest.fixture(scope="module")
def accounts(
    session_factory: sessionmaker[Session], dataset_alpha: DatasetRef, context: PostingContext
) -> dict[str, int]:
    account_ids = seed_purchase_package_data(session_factory, dataset_alpha, context)
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        # Chi nhánh nhận phải khai mã số thuế, nếu không thì phép kiểm người mua
        # từ chối **mọi** tờ hóa đơn — và nó từ chối đúng, xem
        # `test_a_branch_without_a_tax_code_cannot_confirm_anything`.
        branch = session.get(Branch, context.branch_id)
        assert branch is not None
        branch.tax_code = OUR_TAX_CODE
        ensure_vendor(session, partner_id=VENDOR_ID, code="NCC-7F2B", tax_code=SELLER_TAX_CODE)
        ensure_vendor(session, partner_id=UNKNOWN_VENDOR_ID, code="NCC-7F2B-LA")
    return account_ids


Runner = Callable[[Callable[[Session], object]], object]


@pytest.fixture
def run(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
) -> Runner:
    def runner(work: Callable[[Session], object]) -> object:
        scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
        with unit_of_work(session_factory, scope) as session:
            return work(session)

    return runner


_COUNTER = {"next": 0}


def _unique_number() -> str:
    """Số hóa đơn riêng cho mỗi bài.

    Khóa nhận dạng là trạng thái **toàn dataset** và không ai reset nó giữa các
    bài, nên hai bài dùng chung một số sẽ thấy bài chạy sau đâm vào lượt khử
    trùng của bài chạy trước — đúng khuôn bẫy "bài test phụ thuộc thứ tự" đã
    bắt được nhiều lần từ 7A.
    """
    _COUNTER["next"] += 1
    return f"7F2B{_COUNTER['next']:04d}"


def _invoice(**overrides: object) -> InboundInvoiceData:
    payload: dict[str, object] = {
        "buyer_tax_code": OUR_TAX_CODE,
        "seller_tax_code": SELLER_TAX_CODE,
        "number": _unique_number(),
    }
    payload.update(overrides)
    return parse_inbound_xml(inbound_xml(**payload))  # type: ignore[arg-type]


def _record(
    session: Session, data: InboundInvoiceData, *, branch_id: int, file_name: str = "hd.xml"
) -> InboundEInvoice:
    return InboundEInvoiceService(session).record(
        data,
        branch_id=branch_id,
        content_hash=HASH,
        byte_size=1234,
        file_name=file_name,
        user_id=ACTOR_ID,
    )


def test_a_recorded_invoice_keeps_every_number_the_seller_declared(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Lượt nạp giữ nguyên phần đầu và dựng đủ dòng.

    Kèm bất biến trung tâm của bảng dòng: **tiền thuế từng dòng cộng lại đúng
    bằng `TgTThue`**. Tờ hóa đơn TCT không khai thuế theo dòng — nó cộng theo
    nhóm thuế suất — nên con số của dòng là kết quả chia ngược, và một phép chia
    "gần đúng" ở đây là một chứng từ lệch tổng vài đồng mỗi lần.
    """

    def work(session: Session) -> None:
        data = _invoice()
        invoice = _record(session, data, branch_id=context.branch_id)
        lines = InboundEInvoiceService(session).lines_of(invoice.id)

        assert invoice.voucher_id is None
        assert invoice.seller_tax_code == SELLER_TAX_CODE
        assert invoice.total_amount == Decimal(2_200_000)
        assert [line.amount for line in lines] == [Decimal(1_200_000), Decimal(800_000)]
        assert sum(line.vat_amount for line in lines) == data.total_vat

    run(work)


def test_the_vat_split_lands_the_rounding_remainder_on_the_largest_line(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Ba dòng lẻ trong một nhóm thuế — tổng vẫn đúng tới từng đồng.

    Đây là ca mà phép nhân từng dòng (`amount × rate / 100`) hỏng: tổng của ba
    số đã làm tròn khác số làm tròn của tổng. Phần lẻ dồn về dòng **lớn nhất**,
    cùng quy tắc `purchase.landed_cost` — nó cho kết quả xác định, không phụ
    thuộc thứ tự nhập.
    """
    odd_lines = "\n".join(
        f"""        <HHDVu>
          <STT>{index}</STT>
          <THHDVu>Dòng {index}</THHDVu>
          <ThTien>{amount}</ThTien>
          <TSuat>10%</TSuat>
        </HHDVu>"""
        for index, amount in enumerate(("333.33", "333.33", "333.34"), start=1)
    )
    groups = """          <LTSuat>
            <TSuat>10%</TSuat>
            <ThTien>1000</ThTien>
            <TThue>100</TThue>
          </LTSuat>"""

    def work(session: Session) -> None:
        data = _invoice(
            lines=odd_lines,
            vat_groups=groups,
            total_before_tax="1000",
            total_vat="100",
            total_amount="1100",
        )
        invoice = _record(session, data, branch_id=context.branch_id)
        shares = [line.vat_amount for line in InboundEInvoiceService(session).lines_of(invoice.id)]

        assert sum(shares) == Decimal(100)
        assert shares[2] == max(shares)

    run(work)


def test_the_same_invoice_cannot_be_recorded_twice(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Cùng một tờ hóa đơn thường đến hai lần — bản gửi thư và bản tải từ cổng
    tra cứu. Nạp cả hai là khấu trừ thuế đầu vào hai lần trên một tờ."""

    def work(session: Session) -> None:
        number = _unique_number()
        _record(session, _invoice(number=number), branch_id=context.branch_id)
        with pytest.raises(InboundInvoiceDuplicateError):
            _record(
                session,
                _invoice(number=number),
                branch_id=context.branch_id,
                file_name="ban-tai-ve.xml",
            )

    run(work)


def test_the_duplicate_check_survives_a_different_file_of_the_same_invoice(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Khóa nhận dạng là **danh tính pháp lý**, không phải nội dung tệp.

    Hai tệp khác nhau từng byte — một tệp thêm một dòng ghi chú — vẫn là một tờ
    hóa đơn. Một phép chống trùng theo `content_hash` sẽ cho lượt thứ hai đi
    qua, và đó chính là lý do khóa này gồm bốn cột chứ không một hash.
    """
    note = """        <HHDVu>
          <TChat>4</TChat>
          <STT>9</STT>
          <THHDVu>Ghi chú thêm ở bản tải về</THHDVu>
        </HHDVu>"""

    def work(session: Session) -> None:
        number = _unique_number()
        _record(session, _invoice(number=number), branch_id=context.branch_id)
        with pytest.raises(InboundInvoiceDuplicateError):
            _record(
                session,
                _invoice(number=number, lines=inbound_lines_with(note)),
                branch_id=context.branch_id,
            )

    run(work)


def test_an_invoice_issued_to_someone_else_is_refused(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Phép kiểm không một bất biến kế toán nào thay thế được.

    Một tờ hóa đơn xuất cho mã số thuế khác vẫn cân từng đồng — thứ sai là nó
    không phải hóa đơn của mình, và khấu trừ thuế đầu vào trên nó là khấu trừ
    không có căn cứ.
    """

    def work(session: Session) -> None:
        with pytest.raises(InboundInvoiceBuyerMismatchError):
            _record(session, _invoice(buyer_tax_code="0999999999"), branch_id=context.branch_id)

    run(work)


def test_a_tax_code_written_with_spaces_still_matches(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Khoảng trắng trong mã số thuế là cách viết, không phải một mã khác.

    Dấu gạch nối thì **không** được bỏ: `0312345678-001` là mã của một đơn vị
    phụ thuộc khác với `0312345678` của đơn vị chủ quản, và gộp hai mã ấy sẽ cho
    hóa đơn xuất cho chi nhánh này khấu trừ ở chi nhánh kia.
    """

    def work(session: Session) -> None:
        spaced = f"{OUR_TAX_CODE[:4]} {OUR_TAX_CODE[4:]}"
        invoice = _record(session, _invoice(buyer_tax_code=spaced), branch_id=context.branch_id)
        assert invoice.buyer_tax_code == spaced

        with pytest.raises(InboundInvoiceBuyerMismatchError):
            _record(
                session, _invoice(buyer_tax_code=f"{OUR_TAX_CODE}-001"), branch_id=context.branch_id
            )

    run(work)


def test_a_branch_without_a_tax_code_cannot_confirm_anything(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Chưa khai mã số thuế thì **từ chối**, không phải cho qua.

    "Chưa cấu hình nên bỏ qua" biến một phép kiểm chống khấu trừ sai thành một
    phép kiểm không bao giờ chạy ở đúng những bản cài chưa khai gì.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        branch = session.get(Branch, context.branch_id)
        assert branch is not None
        branch.tax_code = None
        session.flush()
        try:
            with pytest.raises(InboundInvoiceBuyerMismatchError) as error:
                _record(session, _invoice(), branch_id=context.branch_id)
            assert "chưa khai mã số thuế" in str(error.value)
        finally:
            branch.tax_code = OUR_TAX_CODE
            session.flush()


def test_a_line_description_longer_than_the_voucher_allows_is_refused_on_import(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Trần mô tả dòng bằng đúng trần của `purchase_invoice_lines`.

    Một trần rộng hơn ở đây chỉ dời chỗ hỏng sang lượt lập chứng từ — nơi người
    dùng đã tưởng tờ hóa đơn vào sổ xong rồi.
    """
    long_line = f"""        <HHDVu>
          <STT>1</STT>
          <THHDVu>{"X" * 501}</THHDVu>
          <ThTien>1000</ThTien>
          <TSuat>KCT</TSuat>
        </HHDVu>"""

    def work(session: Session) -> None:
        with pytest.raises(InboundInvoiceXmlInvalidError):
            _record(
                session,
                _invoice(
                    lines=long_line,
                    vat_groups="",
                    total_before_tax="1000",
                    total_vat="0",
                    total_amount="1000",
                ),
                branch_id=context.branch_id,
            )

    run(work)


def test_the_seller_is_matched_by_tax_code_and_never_guessed(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Ba câu trả lời, và hai trong ba là "chưa biết".

    Mã số thuế lạ trả `None` vì danh mục dùng chung không được để một tệp XML
    bất kỳ ghi vào. **Hai** dòng cùng mã số thuế cũng trả `None`: danh mục trùng
    là chuyện có thật, và lấy dòng đầu là gán một khoản phải trả cho một trong
    hai đối tác mà người dùng không hề chọn.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        service = InboundEInvoiceService(session)
        assert service.vendor_id_for(SELLER_TAX_CODE) == VENDOR_ID
        assert service.vendor_id_for("0000000000") is None

        twin = session.get(
            Partner,
            ensure_vendor(
                session,
                partner_id=TWIN_VENDOR_ID,
                code="NCC-7F2B-SINH-DOI",
                tax_code=SELLER_TAX_CODE,
            ),
        )
        assert twin is not None
        try:
            assert service.vendor_id_for(SELLER_TAX_CODE) is None
        finally:
            # Gỡ bằng ORM chứ không gọi lại `ensure_vendor`: helper ấy **giữ**
            # mã số thuế khi lượt gọi không nói tới nó, đúng như thế — nếu không
            # thì một tệp test gọi `ensure_vendor` vì lý do khác sẽ xóa mất mã
            # mà tệp này vừa đặt.
            twin.tax_code = None
            session.flush()


def test_an_invoice_links_to_exactly_one_voucher(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """`voucher_id` là **một** cột, nên nó tự nó đã nói "một tờ, một chứng từ".

    Lượt gọi thứ hai là câu trả lời cho điều cột ấy đã quy định, không phải một
    cấm đoán thêm.
    """

    def work(session: Session) -> None:
        invoice = _record(session, _invoice(), branch_id=context.branch_id)
        service = InboundEInvoiceService(session)
        first = _some_voucher_id(session, context, accounts)
        service.link_voucher(invoice, voucher_id=first)
        with pytest.raises(InboundInvoiceAlreadyLinkedError):
            service.link_voucher(invoice, voucher_id=first)

    run(work)


def test_an_invoice_that_already_has_a_voucher_cannot_be_deleted(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Xóa chứng từ trước, rồi mới xóa được tờ hóa đơn.

    Thứ tự ấy giữ cho không có chứng từ mua nào mồ côi tờ hóa đơn đã sinh ra nó.
    """

    def work(session: Session) -> None:
        service = InboundEInvoiceService(session)
        pending = _record(session, _invoice(), branch_id=context.branch_id)
        service.delete(pending)

        linked = _record(session, _invoice(), branch_id=context.branch_id)
        service.link_voucher(linked, voucher_id=_some_voucher_id(session, context, accounts))
        with pytest.raises(InboundInvoiceAlreadyLinkedError):
            service.delete(linked)

    run(work)


def test_deleting_the_voucher_returns_the_invoice_to_the_pending_list(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """`ON DELETE SET NULL (voucher_id)` — hành vi không mô phỏng được ngoài PostgreSQL.

    Hai điều được đo cùng lúc, và điều thứ nhất mới là điều dễ mất: chứng từ
    mua lập từ hóa đơn đầu vào **vẫn xóa được**. `RESTRICT` sẽ biến nó thành
    chứng từ không xóa được — một hồi quy đặt lên phân hệ mua, vì
    `REFERENCE_GUARDS` không chạy ở `VoucherService.delete`.
    """

    def work(session: Session) -> None:
        service = InboundEInvoiceService(session)
        invoice = _record(session, _invoice(), branch_id=context.branch_id)
        voucher_id = _some_voucher_id(session, context, accounts)
        service.link_voucher(invoice, voucher_id=voucher_id)

        PurchaseInvoiceService(session).delete(voucher_id)
        session.expire(invoice)

        assert invoice.voucher_id is None
        assert session.get(InboundEInvoice, invoice.id) is not None

    run(work)


def _some_voucher_id(session: Session, context: PostingContext, accounts: dict[str, int]) -> UUID:
    """Một chứng từ mua **mới** để nối vào.

    Dựng bằng chính `PurchaseInvoiceService` chứ không cắm một UUID bịa: khóa
    ngoại ghép `(voucher_id, branch_id)` đòi chứng từ tồn tại **và** đúng chi
    nhánh, nên một UUID bịa sẽ làm bài đỏ vì lý do không liên quan tới điều nó
    đang khẳng định. Mỗi lượt một chứng từ mới vì `voucher_id` là **một** cột —
    dùng lại một chứng từ cho hai tờ hóa đơn không phải hình dạng nào có thật.
    """
    voucher = PurchaseInvoiceService(session).create(
        PurchaseInvoiceIn(
            kind=1,
            operation_code="mua-dich-vu",
            vendor_id=VENDOR_ID,
            payable_account_id=accounts["331"],
            branch_id=context.branch_id,
            document_date=date(2026, 3, 5),
            posting_date=date(2026, 3, 5),
            currency_code="VND",
            exchange_rate=Decimal(1),
            lines=(
                PurchaseInvoiceLineIn(
                    description="Dịch vụ nền cho bài test",
                    amount_fc=Decimal(100_000),
                    account_id=accounts["642"],
                ),
            ),
        ),
        user_id=ACTOR_ID,
    )
    return voucher.id


def test_two_rate_groups_on_one_rate_are_added_up_not_overwritten(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Nhà cung cấp tách `LTSuat` làm nhiều dòng cho cùng một thuế suất.

    Hàng và dịch vụ tách riêng là cách tách thường gặp nhất, và `KHAC:10%` với
    `10%` cũng quy về một con số theo đúng thiết kế của `_vat_rate`. Chia từng
    nhóm rồi gán thẳng vào chỗ của dòng sẽ để nhóm sau **ghi đè** nhóm trước
    trên cùng những dòng ấy: hai nhóm 100 và 200 cho ra tổng 200, tức 100 đồng
    thuế biến mất mà không phép kiểm nào ở lượt nạp thấy.
    """
    lines = "\n".join(
        f"""        <HHDVu>
          <STT>{index}</STT>
          <THHDVu>Dòng {index}</THHDVu>
          <ThTien>{amount}</ThTien>
          <TSuat>10%</TSuat>
        </HHDVu>"""
        for index, amount in enumerate(("1000", "2000"), start=1)
    )
    groups = """          <LTSuat><TSuat>10%</TSuat><ThTien>1000</ThTien><TThue>100</TThue></LTSuat>
          <LTSuat><TSuat>KHAC:10%</TSuat><ThTien>2000</ThTien><TThue>200</TThue></LTSuat>"""

    def work(session: Session) -> None:
        data = _invoice(
            lines=lines,
            vat_groups=groups,
            total_before_tax="3000",
            total_vat="300",
            total_amount="3300",
        )
        invoice = _record(session, data, branch_id=context.branch_id)
        shares = [line.vat_amount for line in InboundEInvoiceService(session).lines_of(invoice.id)]

        assert shares == [Decimal(100), Decimal(200)]
        assert sum(shares) == data.total_vat

    run(work)


def test_a_fractional_vat_total_survives_a_money_scale_of_zero(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Số tờ hóa đơn tự khai **không** được làm tròn trước khi chia.

    `money.scale = 0` (đồng Việt Nam không lẻ) là một lựa chọn hợp lệ và
    `decided_once`. Làm tròn tổng nhóm trước khi chia sẽ đổi `100.55` thành
    `101`, tổng dòng thành `1101` trong khi tờ hóa đơn nói `1100.55` — và phép
    kiểm tổng lúc lập chứng từ từ chối **vĩnh viễn** một tờ hóa đơn bình thường.
    Chỉ các phần chia được làm tròn; phần dư dòng lớn nhất nhận.
    """
    shares = _allocate(Decimal("100.55"), [Decimal(1000), Decimal(2000)], 0)

    assert sum(shares) == Decimal("100.55")
    assert shares[1] == max(shares)


def test_a_broken_constraint_that_is_not_the_identity_key_is_not_called_a_duplicate(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Câu trả lời sai tệ nhất mà cửa nạp có thể đưa ra.

    Lượt `INSERT` này vi phạm được tám ràng buộc khác ngoài khóa nhận dạng. Nói
    với người dùng rằng một tờ hóa đơn có `TGia = 0` "đã nạp vào sổ rồi" khiến
    họ đi tra sổ, không thấy gì, rồi bỏ luôn tờ hóa đơn — một khoản mua không
    vào sổ và một khoản thuế đầu vào mất quyền khấu trừ, không tiếng động nào.
    """

    def work(session: Session) -> None:
        data = _invoice()
        broken = replace(data, exchange_rate=Decimal(0))
        with pytest.raises(IntegrityError):
            _record(session, broken, branch_id=context.branch_id)

    run(work)


def test_the_identity_key_looks_past_padding_and_letter_case(
    run: Runner, accounts: dict[str, int], context: PostingContext
) -> None:
    """Bản gửi thư và bản tải từ cổng tra cứu khác nhau đúng ở cách viết.

    Số hóa đơn là một **số** — `00004994` và `4994` là một; ký hiệu viết hoa ở
    nhà cung cấp này và viết thường ở nhà cung cấp kia; mã số thuế có khoảng
    trắng. So chuỗi thô thì cả hai lọt, và thuế đầu vào được khấu trừ hai lần
    trên cùng một tờ hóa đơn.

    Cột thô vẫn giữ nguyên chữ người bán viết — thứ phải in ra và đối chiếu với
    cơ quan thuế; chuẩn hóa chỉ nằm trong biểu thức của chỉ mục.
    """

    def work(session: Session) -> None:
        number = _unique_number()
        first = _record(
            session, _invoice(number=f"0000{number}", serial="C26TAA"), branch_id=context.branch_id
        )
        assert first.invoice_no == f"0000{number}"

        with pytest.raises(InboundInvoiceDuplicateError):
            _record(
                session,
                _invoice(
                    number=number,
                    serial="c26taa",
                    seller_tax_code=f" {SELLER_TAX_CODE} ",
                ),
                branch_id=context.branch_id,
            )

    run(work)


def test_the_identity_key_reaches_across_branches_where_row_security_cannot(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Lý do khóa nhận dạng cố ý **không** mang `branch_id`, đo từ hai phía.

    Người đứng ở chi nhánh B không nhìn thấy dòng của chi nhánh A — RLS che nó
    — nên phép tra trước lượt ghi trượt, và ràng buộc bảng mới là thứ chặn
    thật. Mà sổ thì chỉ có một: tờ hóa đơn ấy đã vào sổ rồi.
    """
    # Chi nhánh **của riêng bài này**, không mượn `ensure_second_branch`: bài
    # này phải ĐẶT mã số thuế cho chi nhánh ấy, và đặt lên một chi nhánh dùng
    # chung là sửa bối cảnh của tệp test khác — đúng khuôn bẫy "bài test phụ
    # thuộc thứ tự tệp" mà dự án đã bắt nhiều lần.
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        other = BranchService(session).create(
            code=f"PB7F2B{uuid4().hex[:4].upper()}",
            name="Chi nhánh nhận hóa đơn đầu vào",
            tax_code=OUR_TAX_CODE,
        )
        other_id = other.id

    data = _invoice()
    with unit_of_work(session_factory, scope) as session:
        _record(session, data, branch_id=context.branch_id)

    elsewhere = RequestScope(
        dataset_schema=dataset_alpha.schema_name,
        user_id=ACTOR_ID,
        branch_ids=(other_id,),
        acting_branch_id=other_id,
    )
    with unit_of_work(session_factory, elsewhere) as session:
        service = InboundEInvoiceService(session)
        # Đối chứng: dòng kia thật sự vô hình ở đây, nên lượt chặn bên dưới
        # không thể là nhờ một phép tra nào của tầng ứng dụng.
        assert (
            service.list_for(branch_ids=(other_id,), pending_only=False, limit=50, offset=0) == []
        )
        with pytest.raises(InboundInvoiceDuplicateError):
            _record(session, data, branch_id=other_id)


def test_only_one_voucher_can_ever_link_to_one_invoice(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Lượt nối là một phép ghi **có điều kiện**, không một phép so trong Python.

    Phép so `invoice.voucher_id is not None` đúng nhưng hụt ở chỗ đắt nhất: hai
    lượt bấm liên tiếp mang hai khóa idempotency khác nhau, nên `execute_once`
    không nối tiếp chúng; cả hai đọc `voucher_id IS NULL`, cả hai dựng một
    chứng từ mua, và lượt sau ghi đè đường trỏ của lượt trước — hai chứng từ
    cùng số hóa đơn nhà cung cấp, thuế đầu vào khấu trừ hai lần.

    Bài dựng đúng hình dạng ấy bằng **hai session**: cả hai đọc bản ghi trước
    khi một trong hai ghi, nên phép so trong Python của session thứ hai thấy
    `None` — chỉ có `WHERE voucher_id IS NULL` ở cơ sở dữ liệu chặn được.
    """
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        invoice_id = _record(session, _invoice(), branch_id=context.branch_id).id

    with (
        unit_of_work(session_factory, scope) as first,
        unit_of_work(session_factory, scope) as second,
    ):
        stale = InboundEInvoiceService(second).require(invoice_id)
        assert stale.voucher_id is None

        InboundEInvoiceService(first).link_voucher(
            InboundEInvoiceService(first).require(invoice_id),
            voucher_id=_some_voucher_id(first, context, accounts),
        )

    with unit_of_work(session_factory, scope) as session:
        with pytest.raises(InboundInvoiceAlreadyLinkedError):
            InboundEInvoiceService(session).link_voucher(
                stale, voucher_id=_some_voucher_id(session, context, accounts)
            )


def test_a_retired_or_non_vendor_partner_is_never_picked_for_the_payable(
    session_factory: sessionmaker[Session],
    dataset_alpha: DatasetRef,
    context: PostingContext,
    accounts: dict[str, int],
) -> None:
    """Một dòng danh mục đã ngừng dùng mà tình cờ là dòng **duy nhất** mang mã
    số thuế ấy sẽ được chọn làm đối tượng phải trả mà người dùng không hề chọn —
    đúng thứ luật "không đoán hộ" ở đây sinh ra để tránh."""
    scope = posting_scope(dataset_alpha, context, user_id=ACTOR_ID)
    with unit_of_work(session_factory, scope) as session:
        service = InboundEInvoiceService(session)
        partner = session.get(Partner, VENDOR_ID)
        assert partner is not None
        assert service.vendor_id_for(SELLER_TAX_CODE) == VENDOR_ID

        partner.is_active = False
        session.flush()
        try:
            assert service.vendor_id_for(SELLER_TAX_CODE) is None
        finally:
            partner.is_active = True
            session.flush()
