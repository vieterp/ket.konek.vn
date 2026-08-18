"""Phép kiểm phạm vi của một request danh mục — dùng chung cho route sinh và bảng con.

Tách khỏi `routers/master_data.py` (nợ L5 của lát 3B-2, trả ở 3B-3): tệp đó giữ
**bộ sinh route**, còn đây là những câu hỏi "người gọi có được thấy/chọn/ghi vào
bản ghi này không" mà cả ba bên đều phải hỏi — route sinh từ registry, bảng con
tài khoản ngân hàng của đối tác, và từ lát này là hai bảng con của vật tư hàng
hóa. Trước khi tách, bên thứ hai đã phải `import` xuyên vào module bộ sinh; bên
thứ ba sẽ làm điều đó thành thói quen.

Một chỗ cho mọi phép kiểm chi nhánh là **yêu cầu đúng đắn**, không phải sở thích
sắp xếp: bảng danh mục cố ý **không** bật RLS (H39), nên những hàm dưới đây là
lớp cô lập chi nhánh **duy nhất** của danh mục. Hai bản sao là hai chỗ để chúng
trôi lệch nhau, và bản trôi lệch sẽ là bản không ai đọc lại — đúng bài học
`_visible_to` của lát 3A.

Ranh giới chi nhánh ở đây là ranh giới của một **request**, không phải của tầng
dịch vụ: `MasterDataService.get` có những người gọi hợp lệ ở tầng trong (gộp bản
ghi, nhập liệu, posting engine) vốn phải nhìn thấy mọi chi nhánh.
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ket.api.dependencies import AuthorizedRequest
from ket.kernel.errors import (
    BranchNotInScopeError,
    MasterDataFilterUnknownError,
    MasterDataGroupNotPostableError,
    MasterDataNotFoundError,
)
from ket.kernel.master_data.base import MasterDataRow
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogSpec
from ket.kernel.master_data.service import MasterDataService


def ensure_visible(record: MasterDataRow, branch_id: int | None, spec: CatalogSpec) -> None:
    """Bản ghi riêng của chi nhánh khác thì coi như không tồn tại.

    `404` chứ không `403`, có chủ đích: `403` là lời xác nhận "có một bản ghi id
    47 ở chi nhánh khác", và một vòng lặp qua id sẽ vẽ lại được danh mục của chi
    nhánh bên cạnh. Cùng lập luận đã áp cho `AttachmentNotFoundError`.
    """
    if record.branch_id is None or record.branch_id == branch_id:
        return
    raise MasterDataNotFoundError(
        "Không tìm thấy bản ghi danh mục",
        entity_type=spec.entity_type,
        entity_id=record.id,
    )


def ensure_branch_in_scope(branch_id: int | None, authorized: AuthorizedRequest) -> None:
    """Chỉ tạo được danh mục riêng cho chi nhánh **mình đang thao tác**.

    `branch_id` đến từ thân request, nên không kiểm thì bất kỳ ai có quyền tạo
    danh mục cũng cắm được bản ghi vào ngăn của chi nhánh khác — và vì chính họ
    không nhìn thấy nó sau đó (`ensure_visible`), bản ghi ấy trở thành thứ chỉ
    chi nhánh kia thấy mà không ai bên đó tạo ra.

    `None` luôn hợp lệ: đó là danh mục **dùng chung toàn công ty** (FR-SYS-018),
    và quyền tạo nó là chính mã quyền `create` của danh mục đó.

    `403` chứ không `404` như đường đọc: ở đây người gọi tự nêu ra một chi nhánh
    thay vì dò một khóa chính, nên câu trả lời "ngoài phạm vi của bạn" không tiết
    lộ gì mà họ chưa gõ vào.

    So với **chi nhánh đang thao tác**, không so với cả phạm vi được gán (sửa sau
    review L-1, H54). Người được gán cả A lẫn B, đang thao tác ở A, mà tạo được
    bản ghi mang `branch_id = B` thì sẽ mất dấu nó ngay lập tức — mọi đường đọc
    lọc theo `acting_branch_id`, nên bản ghi vừa tạo biến mất khỏi chính màn hình
    vừa tạo ra nó. Phạm vi ghi và phạm vi đọc phải là **một**.
    """
    acting = authorized.scope.acting_branch_id
    if branch_id is None or branch_id == acting:
        return
    raise BranchNotInScopeError(
        "Chỉ tạo được danh mục riêng cho chi nhánh đang thao tác",
        branch=branch_id,
        acting_branch=acting,
    )


def ensure_parent_visible(
    service: MasterDataService[MasterDataRow],
    parent_id: int | None,
    authorized: AuthorizedRequest,
    spec: CatalogSpec,
) -> None:
    """Nhóm cha phải **nhìn thấy được** từ chi nhánh đang thao tác (review H-4, H55).

    Không có bước này thì lỗi phạm vi của tầng dịch vụ trả lời hộ hai câu hỏi mà
    người gọi chưa được phép hỏi: `422` nghĩa là "id này có thật, ở chi nhánh
    khác" còn `404` nghĩa là "không có id này" — đủ để dò ra danh mục của chi
    nhánh bên cạnh gồm những id nào. Nay cả hai đều là `404`.

    `MasterDataParentScopeError` của tầng dịch vụ **vẫn giữ** và vẫn có ích: nó
    bắt trường hợp cha nhìn thấy được nhưng phạm vi vẫn sai (cha riêng chi nhánh
    A, con khai dùng chung). Ở đó `parent_branch_id` trong `details` không lộ gì
    — người gọi nhìn thấy chính nhóm cha ấy.
    """
    if parent_id is None:
        return
    ensure_visible(service.get(parent_id), authorized.scope.acting_branch_id, spec)


def flag_column(spec: CatalogSpec, value: str | None) -> str | None:
    """`?flag=customer` → tên cột boolean phải `TRUE`, hoặc `None` khi không lọc.

    Một tham số **chung** cho mọi danh mục thay vì mỗi danh mục một tham số
    riêng (H62): chữ ký của endpoint sinh ra phải là kiểu tĩnh để mypy kiểm được
    thân hàm, và một tham số truy vấn khai theo từng danh mục lúc chạy sẽ tái lập
    đúng vấn đề annotation mà `_register_with_body` đã giải một lần. Danh sách giá
    trị hợp lệ của từng danh mục nằm trong mô tả OpenAPI.
    """
    if value is None:
        return None
    flag = spec.flag(value)
    if flag is None:
        allowed = ", ".join(item.value for item in spec.flags) or "(danh mục này không có bộ lọc)"
        raise MasterDataFilterUnknownError(
            "Bộ lọc không hợp lệ cho danh mục này",
            entity_type=spec.entity_type,
            flag=value,
            allowed=allowed,
        )
    return flag.column


def ensure_references_visible(
    session: Session,
    spec: CatalogSpec,
    payload: BaseModel,
    authorized: AuthorizedRequest,
) -> None:
    """Mọi khóa ngoại trỏ sang danh mục khác phải **nhìn thấy được** (`CatalogReference`).

    Khóa ngoại của DB chỉ trả lời "id có tồn tại", và đó là câu trả lời sai ở hai
    mặt: bản ghi riêng của chi nhánh khác vẫn tồn tại (nên gắn được, rồi biến mất
    khỏi màn hình của chính người vừa gắn), và một nút **nhóm** cũng tồn tại (nên
    gắn được, dù nhóm chỉ để gom cây chứ không mang giá trị nào).

    `404` cho bản ghi ngoài phạm vi, cùng mã với "không có id này" — cùng lập
    luận H55 đã áp cho nhóm cha: hai mã khác nhau là một oracle liệt kê id.
    """
    for reference in spec.references:
        value = getattr(payload, reference.field, None)
        if value is not None:
            ensure_catalog_choice(session, reference.slug, int(value), authorized)


def ensure_catalog_choice(
    session: Session, slug: str, record_id: int, authorized: AuthorizedRequest
) -> None:
    """Một giá trị người dùng chọn từ danh mục `slug` phải dùng được thật.

    Dùng chung cho hai đường: cột khóa ngoại khai trong `CatalogSpec.references`
    (đối tác → điều khoản thanh toán) và các bảng con viết tay (tài khoản ngân
    hàng của đối tác → ngân hàng; đơn vị quy đổi của vật tư → đơn vị tính). Một
    hàm cho cả hai vì câu hỏi giống hệt nhau, và hai bản sao là hai chỗ để phép
    kiểm chi nhánh trôi lệch.
    """
    target_spec = CATALOG_REGISTRY.get(slug)
    if target_spec is None:  # pragma: no cover - test registry canh trước khi chạy tới đây
        raise MasterDataNotFoundError(
            "Danh mục đích của tham chiếu không tồn tại", entity_type=slug
        )
    service: MasterDataService[MasterDataRow] = MasterDataService(session, target_spec.model)
    record = service.get(record_id)
    ensure_visible(record, authorized.scope.acting_branch_id, target_spec)
    if record.is_group:
        # `MasterDataGroupNotPostableError` chứ không `404` như nhánh trên: tới
        # đây bản ghi đã nhìn thấy được, nên nói thẳng "đây là nhóm" không lộ gì
        # mà người gọi chưa biết — và đó là câu duy nhất giúp họ sửa. Bản ghi
        # **đã ngừng theo dõi** thì vẫn cho gắn: chặn nó sẽ làm mọi lần sửa tên
        # một đối tác cũ bị từ chối vì một điều khoản thanh toán mà chính lần sửa
        # đó không đụng tới.
        raise MasterDataGroupNotPostableError(
            "Giá trị được chọn là một nhóm — hãy chọn một mục con",
            entity_type=target_spec.entity_type,
            entity_id=record.id,
        )
