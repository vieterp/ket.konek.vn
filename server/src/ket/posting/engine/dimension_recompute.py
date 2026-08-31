"""Tính lại chiều SUY RA của dòng đã ghi sổ, và báo chỗ lệch — lát 6G-2 (M-9).

Từ lát 6G-1 luật "dòng 112x này thuộc tài khoản ngân hàng nào" chạy ở đường
**GHI** (`bank/posting_mapper._deposit_owner` → `gl_postings.bank_account_id`)
thay vì được dựng lại mỗi lượt đọc. Đổi ấy làm sáu bản chép của luật biến mất,
nhưng đổi luôn hậu quả của một lần sai: giá trị sai **đóng băng trong sổ**. Bản
sửa luật của phiên bản sau không chạm được tới dòng đã ghi, và không có đường
nào đưa chúng về đúng ngoài "bỏ ghi sổ rồi ghi lại" — thứ mà kỳ đã khóa cấm.

Tệp này là đường ấy. Nguyên tắc: **không viết lại luật**. Nó dựng lại chính
`PostingRequest` mà module sẽ dựng nếu ghi sổ chứng từ đó hôm nay (qua
`build_request` đã đăng ký), rồi so từng dòng theo `(sổ, line_no)` — cùng phép
đánh số mà `_prepare_lines` dùng lúc ghi. Nếu luật đổi, tệp này **không** phải
sửa; nếu luật sai, chỗ sửa vẫn là một chỗ duy nhất.

Chỉ so chiều nào là chiều SUY RA từ thân chứng từ (`_DERIVED_DIMENSIONS`).
Chiều người dùng gõ trên dòng nhập liệu không nằm trong tầm: tính lại chúng chỉ
chép lại chính con số đã lưu, còn khi lệch thì lệch ấy là dữ liệu nhập bị sửa
sau lúc ghi sổ — một câu hỏi khác, có đường xử lý khác (bỏ ghi sổ, sửa, ghi
lại).

Hai chế độ: `apply=False` **chỉ báo** (dùng như một phép kiểm toàn vẹn thứ 9,
chạy được trên kỳ đã khóa vì không ghi gì), `apply=True` ghi đè giá trị lệch.
Ghi đè KHÔNG đụng số tiền, tài khoản hay trạng thái chứng từ — chỉ đúng những
cột chiều trong danh sách — nên nó không phải một lượt "sửa sổ": bảng cân đối,
sổ cái và BCTC ra đúng con số cũ; chỉ báo cáo cắt theo chiều đổi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.posting.documents.models import Voucher, VoucherStatus
from ket.posting.documents.registry import REGISTRY
from ket.posting.engine.models import GlPosting
from ket.posting.engine.requests import PostingLine, PostingRequest

_DERIVED_DIMENSIONS: Final[tuple[str, ...]] = ("bank_account_id",)
"""Chiều do mapper suy ra từ THÂN chứng từ, không do người dùng gõ trên dòng.

Hôm nay đúng một chiều. Danh sách chứ không một cột viết thẳng vào truy vấn:
phase 7–8 thêm chiều suy ra (kho theo thân phiếu xuất, hợp đồng theo đơn hàng)
thì chỉ thêm tên vào đây."""


@dataclass(frozen=True)
class DimensionDrift:
    """Một dòng sổ có chiều lệch khỏi thứ luật hiện hành sẽ tính ra."""

    voucher_id: UUID
    voucher_no: str
    ledger: int
    line_no: int
    dimension: str
    stored: int | None
    expected: int | None


@dataclass(frozen=True)
class RecomputeOutcome:
    vouchers_scanned: int
    drifts: tuple[DimensionDrift, ...]
    applied: int
    """Số dòng đã ghi đè (`0` khi `apply=False`)."""

    unresolved_vouchers: tuple[UUID, ...]
    """Chứng từ không dựng lại được yêu cầu ghi sổ (loại chưa đăng ký, thân đã
    mất). Báo ra chứ không bỏ qua im lặng: đó là dữ liệu cần người nhìn."""


def _expected_by_position(request: PostingRequest) -> dict[tuple[int, int], PostingLine]:
    """`(sổ, line_no)` → dòng của yêu cầu ghi sổ.

    Đánh số phải khớp `engine.service._prepare_lines`: mỗi sổ đánh lại từ 1, và
    `management_lines is None` nghĩa là sổ quản trị chép nguyên sổ tài chính.
    Gọi thẳng hàm ấy để không có bản chép thứ hai của quy tắc đánh số.
    """
    from ket.posting.engine.service import _prepare_lines

    # `scale` chỉ ảnh hưởng số tiền quy đổi, không ảnh hưởng chiều hay `line_no`
    # — phần duy nhất tệp này đọc.
    return {
        (prepared.ledger, prepared.line_no): prepared.source
        for prepared in _prepare_lines(request, scale=0)
    }


def recompute_derived_dimensions(
    session: Session,
    *,
    branch_id: int,
    apply: bool = False,
) -> RecomputeOutcome:
    """Rà chứng từ ĐÃ GHI SỔ của một chi nhánh, báo (và tùy chọn sửa) chiều lệch.

    Theo chi nhánh vì đây là job per-branch dưới RLS như recalc/integrity: một
    người phạm vi hẹp chỉ được rà phần mình thấy, và rà cả công ty là chạy job
    cho từng chi nhánh.
    """
    vouchers = (
        session.execute(
            select(Voucher)
            .where(Voucher.status == VoucherStatus.DA_GHI_SO, Voucher.branch_id == branch_id)
            .order_by(Voucher.posting_date, Voucher.voucher_no)
        )
        .scalars()
        .all()
    )
    drifts: list[DimensionDrift] = []
    unresolved: list[UUID] = []
    applied = 0
    for voucher in vouchers:
        try:
            document_type = REGISTRY.get(voucher.document_type)
            request = document_type.build_request(session, voucher.id)
        except Exception:
            unresolved.append(voucher.id)
            continue
        expected_lines = _expected_by_position(request)
        postings = (
            session.execute(select(GlPosting).where(GlPosting.voucher_id == voucher.id))
            .scalars()
            .all()
        )
        for posting in postings:
            source = expected_lines.get((posting.ledger, posting.line_no))
            if source is None:
                # Số dòng đã đổi kể từ lúc ghi sổ (thân sửa sau khi ghi?) — không
                # đoán, báo lên như chứng từ không rà được.
                unresolved.append(voucher.id)
                break
            for dimension in _DERIVED_DIMENSIONS:
                stored = getattr(posting, dimension)
                expected = getattr(source.dimensions, dimension)
                if stored == expected:
                    continue
                drifts.append(
                    DimensionDrift(
                        voucher_id=voucher.id,
                        voucher_no=voucher.voucher_no,
                        ledger=posting.ledger,
                        line_no=posting.line_no,
                        dimension=dimension,
                        stored=stored,
                        expected=expected,
                    )
                )
                if apply:
                    setattr(posting, dimension, expected)
                    applied += 1
    if apply:
        session.flush()
    return RecomputeOutcome(
        vouchers_scanned=len(vouchers),
        drifts=tuple(drifts),
        applied=applied,
        unresolved_vouchers=tuple(dict.fromkeys(unresolved)),
    )
