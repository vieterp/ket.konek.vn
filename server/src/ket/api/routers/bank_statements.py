"""Sao kê + đối chiếu ngân hàng qua HTTP (`/api/v1/bank/statements`, lát 6D).

Quyền `bank.statement.*` riêng khỏi bốn loại chứng từ tiền gửi: người đối
chiếu không đương nhiên lập được ủy nhiệm chi (cùng tinh thần FR-BNK-022).
create = nhập sao kê, edit = khớp/gỡ khớp, delete = xóa sao kê nhập nhầm.

Không `execute_once` (idempotency route-khai) cho các endpoint ở đây, có chủ
đích: lượt nhập retry đâm vào khóa băm-nội-dung (`bank_statement.duplicate`,
409), lượt khớp retry đâm vào trạng thái đã-khớp (409) — cả hai đường đều
fail-safe sẵn, một lớp khóa idempotency nữa chỉ thêm chỗ hết hạn.

Tệp ghi vào kho đính kèm định địa chỉ theo nội dung TRƯỚC khi đọc (cùng khuôn
`routers/imports.py`): sao kê là chứng cứ đối chiếu, phải lần lại được nguyên
bản khi số liệu có nghi vấn.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ket.api.dependencies import (
    AppSettings,
    AuthorizedRequest,
    SessionFactory,
    require_permission,
)
from ket.api.routers.bank_statements_schemas import (
    AutoMatchResponse,
    BankStatementDetailResponse,
    BankStatementImportOut,
    BankStatementListResponse,
    BankStatementOut,
    BankStatementProfileDetailListResponse,
    BankStatementProfileDetailOut,
    BankStatementProfileIn,
    BankStatementProfileListResponse,
    BankStatementProfileOut,
    BankStatementProfileUpdateIn,
    MatchCandidateOut,
    MatchCandidatesResponse,
    MatchRequest,
    ReconciliationResponse,
)
from ket.kernel.attachments import storage
from ket.kernel.bank_import.profile_models import BankStatementProfile
from ket.kernel.errors import (
    AttachmentStorageNotConfiguredError,
    BankReconciliationScopeError,
    BankStatementProfileConflictError,
    BankStatementProfileNotFoundError,
)
from ket.kernel.persistence.constraints import violated_constraint
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.persistence.versioning import require_row_version
from ket.kernel.security.branch_scope import missing_scope_branch_ids
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.bank import (
    BANK_PERMISSION_MODULE,
    STATEMENT_PERMISSION_CODE,
    STATEMENT_PROFILE_PERMISSION_CODE,
)
from ket.modules.bank.models import BankStatement, BankStatementLine
from ket.modules.bank.reconciliation import (
    auto_match,
    candidates_for_line,
    match_line,
    reconciliation_summary,
    unmatch_line,
)
from ket.modules.bank.statement_import import (
    delete_statement,
    import_statement,
    require_bank_account,
    require_statement,
)
from ket.settings import Settings

router = APIRouter(prefix="/api/v1/bank", tags=["bank-statements"])

STATEMENT_VIEW = permission_code(BANK_PERMISSION_MODULE, STATEMENT_PERMISSION_CODE, Action.VIEW)
STATEMENT_CREATE = permission_code(BANK_PERMISSION_MODULE, STATEMENT_PERMISSION_CODE, Action.CREATE)
STATEMENT_EDIT = permission_code(BANK_PERMISSION_MODULE, STATEMENT_PERMISSION_CODE, Action.EDIT)
STATEMENT_DELETE = permission_code(BANK_PERMISSION_MODULE, STATEMENT_PERMISSION_CODE, Action.DELETE)

StatementViewer = Annotated[AuthorizedRequest, Depends(require_permission(STATEMENT_VIEW))]
StatementImporter = Annotated[AuthorizedRequest, Depends(require_permission(STATEMENT_CREATE))]
StatementMatcher = Annotated[AuthorizedRequest, Depends(require_permission(STATEMENT_EDIT))]
StatementRemover = Annotated[AuthorizedRequest, Depends(require_permission(STATEMENT_DELETE))]

_PROFILE_NAME_UNIQUE: Final[str] = "uq_bank_statement_profiles_bank_name"
_PROFILE_CHECK_PREFIX: Final[str] = "ck_bank_statement_profiles"
"""Quy ước đặt tên của `persistence/base.NAMING_CONVENTION` — mọi `CHECK` của
bảng hồ sơ mang tiền tố này, nên một tên khớp tiền tố nghĩa là "hình dạng khai
sai", còn tên KHÔNG khớp là ràng buộc của bảng khác trỏ tới (FK `RESTRICT` của
`bank_statements.profile_id`)."""

PROFILE_EDIT = permission_code(
    BANK_PERMISSION_MODULE, STATEMENT_PROFILE_PERMISSION_CODE, Action.EDIT
)
StatementAdmin = Annotated[AuthorizedRequest, Depends(require_permission(PROFILE_EDIT))]
"""Khai/sửa/xóa hồ sơ định dạng — **một** mã cho cả ba, không tách create/edit/
delete: ba thao tác ấy cùng một rủi ro (đổi luật đọc tệp) và không có vai trò
thực tế nào được phép sửa mà không được phép khai."""

STATEMENT_MAX_BYTES: Final[int] = 16 * 1024 * 1024
"""Trần một tệp sao kê — nửa trần nhập danh mục: sao kê một kỳ dài nhất cũng
vài nghìn dòng, và trần này chặn byte TRONG lúc ghi (cùng lý do imports.py)."""


def _require_profile(session: Session, profile_id: int) -> BankStatementProfile:
    profile = session.get(BankStatementProfile, profile_id)
    if profile is None:
        raise BankStatementProfileNotFoundError(
            "Không tìm thấy hồ sơ định dạng sao kê", profile_id=str(profile_id)
        )
    return profile


def _flush_profile(session: Session) -> None:
    """Đẩy thay đổi hồ sơ xuống DB ngay để dịch ràng buộc thành 409 đọc được.

    Ràng buộc thật nằm ở BẢNG (unique `(bank_id, name)` từ 3C-2, FK RESTRICT từ
    `bank_statements.profile_id`, và bốn `CHECK` về hình dạng cột tiền/dấu phân
    cách). Kiểm-trước-rồi-ghi ở tầng service sẽ là bản chép thứ hai của cùng
    luật — và bản chép ấy thua một lượt ghi song song. Đây chỉ dịch lỗi.
    """
    try:
        session.flush()
    except IntegrityError as error:
        constraint = violated_constraint(error)
        if constraint == _PROFILE_NAME_UNIQUE:
            raise BankStatementProfileConflictError(
                "Ngân hàng này đã có hồ sơ trùng tên — đặt tên khác",
                constraint=constraint,
            ) from error
        if constraint is not None and constraint.startswith(_PROFILE_CHECK_PREFIX):
            # `CHECK` hình dạng: một-trong-hai cột tiền, ba dấu khác nhau đôi
            # một. Trả tên ràng buộc để client chỉ đúng ô sai.
            raise BankStatementProfileConflictError(
                "Cách đọc tệp khai chưa hợp lệ — xem lại cột số tiền và các dấu phân cách",
                constraint=constraint,
            ) from error
        raise BankStatementProfileConflictError(
            "Hồ sơ này đang được sao kê đã nhập sử dụng — xóa các sao kê đó trước",
            constraint=constraint or "",
        ) from error


def _require_company_wide_scope(
    session: Session, authorized: AuthorizedRequest, *, action: str
) -> None:
    """Đối chiếu là nghiệp vụ **phạm vi công ty** (quyết định user, lát 6G-2 M-4).

    Sao kê và sổ không cùng trục phạm vi: dòng sao kê treo trên TÀI KHOẢN ngân
    hàng (mặc định dùng chung, `branch_id IS NULL` ⇒ mọi chi nhánh đọc được)
    còn chứng từ đối ứng nằm dưới RLS chi nhánh. Người được cấp một chi nhánh
    vì thế thấy **đủ** vế sao kê nhưng **thiếu** vế sổ — phần lệch phình lên
    đúng bằng phần bị giấu, và không có gì trên màn hình nói rằng nó thiếu.

    Vì sao phủ cả khớp TAY và GỠ khớp, không chỉ khớp tự động như 6D: một cặp
    khớp là phát biểu về CẢ tài khoản, không về chi nhánh người bấm. Người nhìn
    thấy một nửa gỡ được cặp mà nửa kia dựng nên, rồi khớp lại vào chứng từ
    khác — và đó đúng là câu hỏi "quyền gỡ khớp xuyên phạm vi" treo từ 6F-2,
    nay đóng bằng chính cổng này.

    `action` chỉ vào câu chữ báo lỗi: kế toán đang bấm "Gỡ khớp" cần đọc chữ
    "Gỡ khớp", không phải một thông điệp chung chung của phân hệ.
    """
    missing = missing_scope_branch_ids(session, authorized.scope)
    if not missing:
        return
    visible = len(set(authorized.scope.branch_ids))
    raise BankReconciliationScopeError(
        f"{action} cần quyền trên mọi chi nhánh — nhờ người có phạm vi toàn đơn vị chạy",
        branches_visible=visible,
        branches_total=visible + len(missing),
    )


def _storage_root(settings: Settings) -> Path:
    if settings.attachments_dir is None:
        raise AttachmentStorageNotConfiguredError(
            "Bản cài chưa cấu hình thư mục tệp (KET_ATTACHMENTS_DIR)"
        )
    return settings.attachments_dir


@router.post(
    "/statements/import",
    response_model=BankStatementImportOut,
    status_code=status.HTTP_201_CREATED,
)
def import_bank_statement(
    authorized: StatementImporter,
    factory: SessionFactory,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
    bank_account_id: Annotated[int, Form(ge=1)],
    profile_id: Annotated[int, Form(ge=1)],
) -> BankStatementImportOut:
    """Nhập một tệp sao kê theo hồ sơ per-bank (FR-BNK-032, RT-26).

    Trọn-hoặc-không: dòng hỏng nào cũng dừng cả lượt và trả toàn bộ lỗi.
    Nhập trùng tệp (băm nội dung) → 409, xóa sao kê cũ trước nếu muốn nhập lại.
    """
    stored = storage.store_stream(
        _storage_root(settings),
        authorized.scope.dataset_schema,
        file.file,
        max_bytes=STATEMENT_MAX_BYTES,
    )
    file_name = (file.filename or "sao-ke.xlsx")[:255]
    with unit_of_work(factory, authorized.scope) as session:
        with storage.open_blob(
            _storage_root(settings), authorized.scope.dataset_schema, stored.content_hash
        ) as source:
            result = import_statement(
                session,
                bank_account_id=bank_account_id,
                profile_id=profile_id,
                source=source,
                file_name=file_name,
                content_hash=stored.content_hash,
                user_id=authorized.scope.user_id,
                acting_branch_id=authorized.scope.acting_branch_id,
            )
        return BankStatementImportOut.from_result(result)


@router.get("/statements", response_model=BankStatementListResponse)
def list_bank_statements(
    authorized: StatementViewer,
    factory: SessionFactory,
    bank_account_id: Annotated[int, Query(ge=1)],
) -> BankStatementListResponse:
    with unit_of_work(factory, authorized.scope) as session:
        require_bank_account(
            session, bank_account_id, acting_branch_id=authorized.scope.acting_branch_id
        )
        statements = (
            session.execute(
                select(BankStatement)
                .where(BankStatement.bank_account_id == bank_account_id)
                .order_by(BankStatement.statement_date.desc(), BankStatement.imported_at.desc())
            )
            .scalars()
            .all()
        )
        return BankStatementListResponse(
            items=tuple(BankStatementOut.model_validate(row) for row in statements)
        )


@router.get("/statements/profiles", response_model=BankStatementProfileListResponse)
def list_statement_profiles(
    authorized: StatementViewer,
    factory: SessionFactory,
    bank_account_id: Annotated[int, Query(ge=1)],
) -> BankStatementProfileListResponse:
    """Hồ sơ định dạng dùng được cho MỘT tài khoản ngân hàng (lát 6F-2).

    Ô chọn hồ sơ của màn nhập sao kê cần danh sách này; lọc theo ngân hàng của
    tài khoản NGAY Ở ĐÂY vì `import_statement` từ chối hồ sơ khác ngân hàng —
    đưa client tự ghép `bank_id` là mời một lượt 422 đoán được trước.

    Khai TRƯỚC `/statements/{statement_id}`: FastAPI khớp theo thứ tự, đường
    tĩnh đứng sau đường UUID sẽ thành 422 "profiles không phải UUID".
    """
    with unit_of_work(factory, authorized.scope) as session:
        account = require_bank_account(
            session, bank_account_id, acting_branch_id=authorized.scope.acting_branch_id
        )
        profiles = (
            session.execute(
                select(BankStatementProfile)
                .where(BankStatementProfile.bank_id == account.bank_id)
                .order_by(BankStatementProfile.name)
            )
            .scalars()
            .all()
        )
        return BankStatementProfileListResponse(
            items=tuple(BankStatementProfileOut.model_validate(row) for row in profiles)
        )


@router.get("/statements/profiles/all", response_model=BankStatementProfileDetailListResponse)
def list_all_statement_profiles(
    authorized: StatementViewer, factory: SessionFactory
) -> BankStatementProfileDetailListResponse:
    """Mọi hồ sơ định dạng, trọn cột — thân của màn KHAI hồ sơ (lát 6G-2).

    Khác `/statements/profiles` ở hai điểm và cả hai đều cố ý: không lọc theo
    tài khoản (màn khai làm việc theo NGÂN HÀNG, không theo tài khoản), và trả
    đủ cột cách-đọc-tệp.
    """
    with unit_of_work(factory, authorized.scope) as session:
        profiles = (
            session.execute(
                select(BankStatementProfile).order_by(
                    BankStatementProfile.bank_id, BankStatementProfile.name
                )
            )
            .scalars()
            .all()
        )
        return BankStatementProfileDetailListResponse(
            items=tuple(BankStatementProfileDetailOut.model_validate(row) for row in profiles)
        )


@router.post(
    "/statements/profiles",
    response_model=BankStatementProfileDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def create_statement_profile(
    payload: BankStatementProfileIn, authorized: StatementAdmin, factory: SessionFactory
) -> BankStatementProfileDetailOut:
    """Khai một cách đọc sao kê mới cho một ngân hàng (RT-26).

    Quyền `bank.statement_profile.*` riêng khỏi `bank.statement.*`: nhập sao kê
    là việc hằng ngày của kế toán, còn sửa cách đọc tệp là việc đổi **luật diễn
    giải mọi lượt nhập sau đó** — một dấu thập phân gõ nhầm ở đây làm mọi con
    số sai gấp trăm lần mà không dòng nào báo đỏ.
    """
    with unit_of_work(factory, authorized.scope) as session:
        profile = BankStatementProfile(**payload.model_dump())
        session.add(profile)
        _flush_profile(session)
        return BankStatementProfileDetailOut.model_validate(profile)


@router.put("/statements/profiles/{profile_id}", response_model=BankStatementProfileDetailOut)
def update_statement_profile(
    profile_id: int,
    payload: BankStatementProfileUpdateIn,
    authorized: StatementAdmin,
    factory: SessionFactory,
) -> BankStatementProfileDetailOut:
    """Sửa trọn bộ một hồ sơ (PUT thay-trọn-bộ, cùng khuôn chứng từ)."""
    with unit_of_work(factory, authorized.scope) as session:
        profile = _require_profile(session, profile_id)
        require_row_version(
            current=profile.row_version,
            expected=payload.row_version,
            entity="bank_statement_profile",
        )
        for field, value in payload.model_dump(exclude={"row_version"}).items():
            setattr(profile, field, value)
        _flush_profile(session)
        return BankStatementProfileDetailOut.model_validate(profile)


@router.delete("/statements/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_statement_profile(
    profile_id: int, authorized: StatementAdmin, factory: SessionFactory
) -> None:
    """Xóa một hồ sơ.

    Sao kê đã nhập giữ `profile_id` với `ON DELETE RESTRICT`, nên hồ sơ từng
    dùng thì không xóa được — dịch `IntegrityError` thành 409 đọc được thay vì
    để 500 rơi ra, cùng lối `_flush_profile`.
    """
    with unit_of_work(factory, authorized.scope) as session:
        session.delete(_require_profile(session, profile_id))
        _flush_profile(session)


@router.get("/statements/{statement_id}", response_model=BankStatementDetailResponse)
def get_bank_statement(
    statement_id: UUID, authorized: StatementViewer, factory: SessionFactory
) -> BankStatementDetailResponse:
    """Header + toàn bộ dòng theo thứ tự tệp gốc — nguồn của khung trái U5."""
    with unit_of_work(factory, authorized.scope) as session:
        statement = require_statement(session, statement_id)
        lines = (
            session.execute(
                select(BankStatementLine)
                .where(BankStatementLine.statement_id == statement_id)
                .order_by(BankStatementLine.line_no)
            )
            .scalars()
            .all()
        )
        return BankStatementDetailResponse.from_rows(statement, list(lines))


@router.delete("/statements/{statement_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bank_statement(
    statement_id: UUID, authorized: StatementRemover, factory: SessionFactory
) -> None:
    """Xóa một sao kê nhập nhầm — chỉ khi chưa có dòng nào khớp (409 nếu có)."""
    with unit_of_work(factory, authorized.scope) as session:
        delete_statement(session, statement_id=statement_id)


@router.post("/statements/{statement_id}/actions/auto-match", response_model=AutoMatchResponse)
def auto_match_statement(
    statement_id: UUID, authorized: StatementMatcher, factory: SessionFactory
) -> AutoMatchResponse:
    """Khớp tự động (FR-BNK-030): cùng chiều + cùng số tiền + ngày ±3, ưu tiên
    trùng số tham chiếu; ứng viên nhập nhằng để lại cho khớp tay.

    Đòi phạm vi MỌI chi nhánh — từ lát 6G-2 là luật của **cả** phân hệ đối
    chiếu, không riêng đường tự động (M-4). Lý do gốc (review 6D, M-1) vẫn
    đứng: ứng viên là chứng từ dưới RLS chi nhánh, còn sao kê là dữ liệu mức
    tài khoản (tài khoản dùng chung không mang chi nhánh), nên phạm vi hẹp làm
    máy không thấy ứng viên đúng và khớp nhầm ứng viên duy nhất còn lại một
    cách tất định.
    """
    with unit_of_work(factory, authorized.scope) as session:
        _require_company_wide_scope(session, authorized, action="Khớp tự động")
        outcome = auto_match(session, statement_id=statement_id)
        return AutoMatchResponse.from_outcome(outcome)


@router.post("/statements/lines/{line_id}/actions/match", status_code=status.HTTP_204_NO_CONTENT)
def match_statement_line(
    line_id: UUID,
    payload: MatchRequest,
    authorized: StatementMatcher,
    factory: SessionFactory,
) -> None:
    """Khớp tay một dòng với một chứng từ — vẫn đòi đúng tài khoản, đúng chiều,
    đúng số tiền; chỉ cửa sổ ngày là không giới hạn.

    Từ 6G-2 cũng đòi phạm vi công ty như khớp tự động (M-4): 6D để ngỏ cửa này
    vì "người dùng chỉ chọn được thứ mình thấy", nhưng thứ họ *không* thấy mới
    là vấn đề — khớp một dòng vào chứng từ chi nhánh mình trong khi chứng từ
    đúng nằm ở chi nhánh bị RLS giấu là một cặp khớp SAI, và nó khóa luôn dòng
    ấy khỏi lượt khớp đúng sau này."""
    with unit_of_work(factory, authorized.scope) as session:
        _require_company_wide_scope(session, authorized, action="Khớp tay")
        match_line(session, line_id=line_id, voucher_id=payload.voucher_id)


@router.post("/statements/lines/{line_id}/actions/unmatch", status_code=status.HTTP_204_NO_CONTENT)
def unmatch_statement_line(
    line_id: UUID, authorized: StatementMatcher, factory: SessionFactory
) -> None:
    with unit_of_work(factory, authorized.scope) as session:
        _require_company_wide_scope(session, authorized, action="Gỡ khớp")
        unmatch_line(session, line_id=line_id)


@router.get("/statements/lines/{line_id}/candidates", response_model=MatchCandidatesResponse)
def statement_line_candidates(
    line_id: UUID, authorized: StatementViewer, factory: SessionFactory
) -> MatchCandidatesResponse:
    """Gợi ý ghép cho khớp tay (U5) — đúng chiều + đúng số tiền, xếp theo
    khoảng cách ngày."""
    with unit_of_work(factory, authorized.scope) as session:
        _require_company_wide_scope(session, authorized, action="Gợi ý ghép")
        candidates = candidates_for_line(session, line_id=line_id)
        return MatchCandidatesResponse(
            items=tuple(MatchCandidateOut.from_candidate(candidate) for candidate in candidates)
        )


@router.get("/reconciliation", response_model=ReconciliationResponse)
def get_reconciliation(
    authorized: StatementViewer,
    factory: SessionFactory,
    bank_account_id: Annotated[int, Query(ge=1)],
    as_of: Annotated[date, Query()],
) -> ReconciliationResponse:
    """Báo cáo lệch hai phía (FR-BNK-031): trên sao kê chưa có trên sổ và
    ngược lại, tính đến hết ngày `as_of`."""
    with unit_of_work(factory, authorized.scope) as session:
        require_bank_account(
            session, bank_account_id, acting_branch_id=authorized.scope.acting_branch_id
        )
        _require_company_wide_scope(session, authorized, action="Báo cáo đối chiếu")
        summary = reconciliation_summary(session, bank_account_id=bank_account_id, as_of=as_of)
        return ReconciliationResponse.from_summary(summary)
