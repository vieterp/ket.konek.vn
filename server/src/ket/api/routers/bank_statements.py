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
from sqlalchemy import func, select

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
    BankStatementProfileListResponse,
    BankStatementProfileOut,
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
)
from ket.kernel.persistence.unit_of_work import unit_of_work
from ket.kernel.security.models import Branch
from ket.kernel.security.permissions import Action, permission_code
from ket.modules.bank import BANK_PERMISSION_MODULE, STATEMENT_PERMISSION_CODE
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

STATEMENT_MAX_BYTES: Final[int] = 16 * 1024 * 1024
"""Trần một tệp sao kê — nửa trần nhập danh mục: sao kê một kỳ dài nhất cũng
vài nghìn dòng, và trần này chặn byte TRONG lúc ghi (cùng lý do imports.py)."""


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

    Đòi phạm vi MỌI chi nhánh (review 6D, M-1): ứng viên là chứng từ dưới RLS
    chi nhánh, còn sao kê là dữ liệu mức tài khoản — phạm vi hẹp làm máy không
    thấy ứng viên đúng và khớp nhầm ứng viên duy nhất còn lại một cách tất
    định. Khớp TAY không bị chặn: người dùng chỉ chọn được thứ mình thấy.
    """
    with unit_of_work(factory, authorized.scope) as session:
        total_branches = session.execute(select(func.count()).select_from(Branch)).scalar_one()
        if len(set(authorized.scope.branch_ids)) < total_branches:
            raise BankReconciliationScopeError(
                "Khớp tự động cần quyền trên mọi chi nhánh — dùng khớp tay, hoặc nhờ "
                "người có phạm vi toàn đơn vị chạy",
                branches_visible=len(set(authorized.scope.branch_ids)),
                branches_total=int(total_branches),
            )
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
    đúng số tiền; chỉ cửa sổ ngày là không giới hạn."""
    with unit_of_work(factory, authorized.scope) as session:
        match_line(session, line_id=line_id, voucher_id=payload.voucher_id)


@router.post("/statements/lines/{line_id}/actions/unmatch", status_code=status.HTTP_204_NO_CONTENT)
def unmatch_statement_line(
    line_id: UUID, authorized: StatementMatcher, factory: SessionFactory
) -> None:
    with unit_of_work(factory, authorized.scope) as session:
        unmatch_line(session, line_id=line_id)


@router.get("/statements/lines/{line_id}/candidates", response_model=MatchCandidatesResponse)
def statement_line_candidates(
    line_id: UUID, authorized: StatementViewer, factory: SessionFactory
) -> MatchCandidatesResponse:
    """Gợi ý ghép cho khớp tay (U5) — đúng chiều + đúng số tiền, xếp theo
    khoảng cách ngày."""
    with unit_of_work(factory, authorized.scope) as session:
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
        summary = reconciliation_summary(session, bank_account_id=bank_account_id, as_of=as_of)
        return ReconciliationResponse.from_summary(summary)
