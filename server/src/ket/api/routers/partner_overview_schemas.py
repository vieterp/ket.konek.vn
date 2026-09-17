"""Hình dạng response của BFF `GET /api/v1/partners/{id}/overview` (lát 7G-4, nợ H56)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from ket.api.routers.master_data_schemas import MasterDataBaseResponse
from ket.kernel.master_data.models.partner import PartnerFields


class PartnerInfo(MasterDataBaseResponse, PartnerFields):
    """Hồ sơ đối tác — cùng bộ trường với `PartnersResponse` của router danh mục.

    Lớp TĨNH kế thừa đúng hai mảnh mà `build_schemas` ghép cho danh mục đối tác,
    thay vì gọi `build_schemas` lần nữa: hàm ấy dựng model bằng `create_model`
    và gọi hai lần là hai lớp cùng tên `PartnersResponse` trên OpenAPI — bộ sinh
    type cho client sẽ đổi tên một trong hai, và client không biết cái nào là
    cái nào. Tên khác (`PartnerInfo`) nói thẳng: đây là phần "thông tin" của
    thẻ, và thêm cột vào `PartnerFields` thì cả hai cùng nhận.
    """


class DebtSide(BaseModel):
    """Một chiều công nợ của đối tác tại `as_of`, trong phạm vi RLS người xem.

    Số VND theo tỷ giá GHI NHẬN nợ (cùng cột `remaining` của báo cáo tuổi nợ);
    khoản nhiều đồng tiền cộng được trên trục này, còn nguyên tệ thì không.
    `oldest_due_date` là hạn sớm nhất trong các khoản QUÁ HẠN — chỗ bấu đầu tiên.
    """

    open_amount: Decimal
    open_count: int
    overdue_amount: Decimal
    overdue_count: int
    oldest_due_date: date | None


class PartnerDebtCard(BaseModel):
    """Thẻ công nợ (nguyên tắc nhóm 07: "thẻ công nợ hiện ngay").

    Mỗi nửa chỉ có mặt khi người xem có quyền xem chứng từ của chiều đó
    (`sales.invoice.view` ↔ phải thu, `purchase.invoice.view` ↔ phải trả —
    quyết định user 2026-09-17, cùng trục với báo cáo tuổi nợ). Thiếu quyền →
    nửa ấy `null`, không phải 403: kế toán bán hàng vẫn mở được hồ sơ khách.

    `credit_available` = `credit_limit` − phải thu còn treo **không kể khoản ứng
    trước** (cùng luật với `partner_open_debt()` của guard ngưỡng nợ); `null` khi
    đối tác không khai ngưỡng HOẶC người xem không thấy nửa phải thu (không có
    gì để trừ). Chỉ sổ tài chính — cùng sổ với `PartnerDebtGuard`; khác guard ở
    phạm vi chi nhánh (thẻ theo RLS người xem, guard toàn công ty).
    """

    as_of: date
    receivable: DebtSide | None
    payable: DebtSide | None
    credit_limit: Decimal | None
    credit_available: Decimal | None


class PartnerOverviewResponse(BaseModel):
    """Thông tin + thẻ công nợ của một đối tác — hai module, một màn hình (RT-21)."""

    partner: PartnerInfo
    debt: PartnerDebtCard
