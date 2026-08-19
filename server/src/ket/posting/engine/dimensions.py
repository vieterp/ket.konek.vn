"""Chiều phân tích trên một dòng hạch toán (LD-08, ADR-007).

Sáu chiều lõi + đối tác + vật tư/kho là **cột cố định** trên `gl_postings`;
chiều mở rộng đi qua bảng `posting_dimension_values`. Tệp này là hình dạng
Pydantic của cả hai — thứ mà module nghiệp vụ điền vào `PostingLine`, và là
chỗ duy nhất biết cách ánh xạ giá trị `detail_tracking` của tài khoản sang
câu hỏi "dòng này đã điền chiều đó chưa".
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import BaseModel, ConfigDict, model_validator

from ket.kernel.config.accounts_models import DetailTracking


class PartnerKind(IntEnum):
    """Loại đối tác trên dòng hạch toán — `0 customer 1 vendor 2 employee`.

    Một cột `partner_id` + một cột loại, chứ không ba cột khóa riêng: TK 131
    theo dõi khách, 331 theo dõi nhà cung cấp, 141 theo dõi nhân viên — mỗi
    dòng chỉ có **một** đối tượng công nợ, và ba cột sẽ cho phép điền hai.
    """

    CUSTOMER = 0
    VENDOR = 1
    EMPLOYEE = 2


class ExtendedDimensionValue(BaseModel):
    """Một giá trị chiều mở rộng gắn vào dòng — đi vào `posting_dimension_values`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension_id: int
    value_id: int


class PostingDimensions(BaseModel):
    """Bộ chiều của một dòng hạch toán. Mọi trường đều tùy chọn — validator
    ghi sổ mới là nơi quyết định chiều nào bắt buộc, theo `detail_tracking`
    của tài khoản (FR-SYS-021) và `applies_to_accounts` của chiều mở rộng.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    partner_id: int | None = None
    partner_kind: PartnerKind | None = None

    cost_object_id: int | None = None
    project_id: int | None = None
    order_id: int | None = None
    contract_id: int | None = None
    expense_item_id: int | None = None

    item_id: int | None = None
    warehouse_id: int | None = None

    extended: tuple[ExtendedDimensionValue, ...] = ()

    @model_validator(mode="after")
    def _partner_kind_iff_partner(self) -> PostingDimensions:
        """`partner_id` và `partner_kind` sống chết cùng nhau.

        Một `partner_id` không loại là con số không tra được vào danh mục nào;
        một loại không id là câu trả lời cho câu hỏi chưa ai hỏi. Chặn ở biên
        thay vì để dữ liệu nửa vời đi vào sổ.
        """
        if (self.partner_id is None) != (self.partner_kind is None):
            raise ValueError("partner_id và partner_kind phải cùng có hoặc cùng vắng")
        return self

    def has_tracking(self, tracking: str) -> bool:
        """Dòng đã điền chiều mà `detail_tracking` của tài khoản đòi chưa.

        `customer`/`vendor`/`employee` đòi đúng **loại** đối tác chứ không chỉ
        "có đối tác": TK 131 mà điền nhà cung cấp là công nợ ghi nhầm sổ, và
        cái sai đó phải bị chặn ở đây chứ không đợi thẻ công nợ phát hiện.
        """
        match tracking:
            case DetailTracking.CUSTOMER:
                return self.partner_kind is PartnerKind.CUSTOMER
            case DetailTracking.VENDOR:
                return self.partner_kind is PartnerKind.VENDOR
            case DetailTracking.EMPLOYEE:
                return self.partner_kind is PartnerKind.EMPLOYEE
            case DetailTracking.COST_OBJECT:
                return self.cost_object_id is not None
            case DetailTracking.PROJECT:
                return self.project_id is not None
            case DetailTracking.ORDER:
                return self.order_id is not None
            case DetailTracking.CONTRACT:
                return self.contract_id is not None
            case DetailTracking.EXPENSE_ITEM:
                return self.expense_item_id is not None
            case DetailTracking.ITEM:
                return self.item_id is not None
            case DetailTracking.WAREHOUSE:
                return self.warehouse_id is not None
            case _:
                # Giá trị lạ trong `detail_tracking` là dữ liệu cấu hình hỏng,
                # không phải chứng từ hỏng — nổ to để lộ ra gói cấu hình sai.
                raise ValueError(f"detail_tracking không nhận ra: {tracking!r}")
