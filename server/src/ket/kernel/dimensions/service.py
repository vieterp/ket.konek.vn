"""Đọc, khai và kiểm giá trị chiều phân tích mở rộng (LD-08, FR-SYS-051).

Dịch vụ **không** tự mở transaction — nhận `Session` đang mở của người gọi, cùng
hợp đồng với `MasterDataService`. Đó là điều kiện để gói cấu hình (phase 5) khai
một chiều và gieo luôn giá trị của nó trong một lần hoặc-được-tất-cả-hoặc-không.

Phần **ép** chiều bắt buộc theo dải tài khoản không nằm ở đây: nó cần một dòng
hạch toán thật để ép, nên nó sống cùng posting engine ở phase 4 (RT-20 hoãn
riêng phần `applies_to_accounts` tới v1.1). Cái ở đây là phần dùng được ngay và
đúng ở mọi phase: *"giá trị này có thuộc chiều đó không, và còn dùng được
không"* — câu hỏi mà mọi màn hình nhập liệu phải hỏi trước khi ghi.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ket.kernel.dimensions.models import (
    AnalysisDimension,
    AnalysisDimensionValue,
    DimensionValueSource,
)
from ket.kernel.errors import (
    DimensionNotFoundError,
    DimensionValueNotFoundError,
    DimensionValueSourceMismatchError,
)
from ket.kernel.master_data.registry import REGISTRY as CATALOG_REGISTRY
from ket.kernel.master_data.registry import CatalogRegistry
from ket.kernel.master_data.tree_path import ROOT_LEVEL, child_path, like_prefix
from ket.kernel.persistence.sequences import reserve_id


def validate_value_source(
    value_source: DimensionValueSource,
    master_slug: str | None,
    *,
    catalogs: CatalogRegistry = CATALOG_REGISTRY,
) -> None:
    """Cặp (`value_source`, `master_slug`) phải nhất quán và slug phải có thật.

    Hàm tự do chứ không phương thức, vì nó có **hai** người gọi đi hai đường
    khác nhau: `DimensionService.declare` (đường API, dùng ORM) và
    `dimensions/seed.py` (đường gieo mầm, dùng Core vì lúc cấp dataset chưa có
    người thực hiện nào để ghi nhật ký). Hai đường ghi cùng một bảng mà mỗi
    đường một bộ luật là cách bộ luật lỏng hơn trở thành bộ luật thật.

    Ràng buộc `CHECK` ở DB canh được vế thứ nhất; vế thứ hai thì không —
    `CatalogSpec.slug` là hằng trong mã nguồn, PostgreSQL không nhìn thấy. Không
    kiểm ở đây thì một slug gõ sai chỉ lộ ra khi có người mở ô chọn và thấy nó
    rỗng, thường là vài tuần sau lúc khai.
    """
    if value_source is DimensionValueSource.MASTER:
        if master_slug is None:
            raise DimensionValueSourceMismatchError(
                "Chiều lấy giá trị từ danh mục thì phải nói là danh mục nào"
            )
        if catalogs.get(master_slug) is None:
            raise DimensionValueSourceMismatchError(
                "Không có danh mục nào mang mã này", master_slug=master_slug
            )
    elif master_slug is not None:
        raise DimensionValueSourceMismatchError(
            "Chiều dùng danh sách riêng thì không gắn danh mục", master_slug=master_slug
        )


class DimensionService:
    """Thao tác trên chiều phân tích mở rộng và giá trị của chúng."""

    def __init__(self, session: Session, *, catalogs: CatalogRegistry = CATALOG_REGISTRY) -> None:
        self._session = session
        # Tiêm registry thay vì đọc thẳng biến toàn cục: test dựng registry
        # riêng để kiểm "slug lạ bị từ chối" mà không phải đăng ký một danh mục
        # giả vào registry thật — thứ sẽ rò sang mọi test chạy sau.
        self._catalogs = catalogs

    # ------------------------------------------------------------------ đọc

    def list_dimensions(self, *, include_inactive: bool = False) -> Sequence[AnalysisDimension]:
        statement = select(AnalysisDimension).order_by(AnalysisDimension.code)
        if not include_inactive:
            statement = statement.where(AnalysisDimension.is_active.is_(True))
        return self._session.execute(statement).scalars().all()

    def get_by_code(self, code: str) -> AnalysisDimension:
        dimension = (
            self._session.execute(select(AnalysisDimension).where(AnalysisDimension.code == code))
            .scalars()
            .first()
        )
        if dimension is None:
            raise DimensionNotFoundError("Không có chiều phân tích với mã này", code=code)
        return dimension

    def values_of(
        self, dimension: AnalysisDimension, *, include_inactive: bool = False
    ) -> Sequence[AnalysisDimensionValue]:
        """Giá trị của một chiều `list`, theo thứ tự cây.

        `ORDER BY path` cho ra thứ tự duyệt trước (cha rồi tới con) mà ô chọn
        dạng cây cần — cùng lối `MasterDataService.subtree_of` đã dùng.
        """
        self._ensure_list_source(dimension)
        statement = (
            select(AnalysisDimensionValue)
            .where(AnalysisDimensionValue.dimension_id == dimension.id)
            .order_by(AnalysisDimensionValue.path)
        )
        if not include_inactive:
            statement = statement.where(AnalysisDimensionValue.is_active.is_(True))
        return self._session.execute(statement).scalars().all()

    def subtree_of(self, value: AnalysisDimensionValue) -> Sequence[AnalysisDimensionValue]:
        """Một giá trị và toàn bộ nhánh dưới nó — dùng để cộng tổng theo nhánh.

        Lọc `dimension_id` **cùng với** tiền tố `path`. Hôm nay điều kiện thứ
        nhất là thừa: `add_value` lấy id từ một sequence dùng chung cho cả bảng,
        nên id duy nhất toàn bảng và hai chiều không thể có cùng `path`.

        Giữ vì bất biến ấy nằm ở **chỗ khác** — trong `add_value`, không trong
        câu truy vấn này. Đổi sang sequence riêng cho mỗi chiều là một tối ưu
        hợp lý mà người thực hiện không có lý do gì phải đọc tới đây, và ngay
        khoảnh khắc đó bộ lọc này thành thứ duy nhất chặn một báo cáo cộng nhầm
        giá trị của chiều khác vào tổng.
        """
        statement = (
            select(AnalysisDimensionValue)
            .where(AnalysisDimensionValue.dimension_id == value.dimension_id)
            .where(AnalysisDimensionValue.path.like(like_prefix(value.path)))
            .order_by(AnalysisDimensionValue.path)
        )
        return self._session.execute(statement).scalars().all()

    # ----------------------------------------------------------------- ghi

    def declare(
        self,
        *,
        code: str,
        name: str,
        name_en: str | None = None,
        value_source: DimensionValueSource = DimensionValueSource.LIST,
        master_slug: str | None = None,
        is_required: bool = False,
        applies_to_accounts: list[str] | None = None,
    ) -> AnalysisDimension:
        """Khai một chiều mới — **dữ liệu, không phải mã nguồn**.

        Đây là điều tiêu chí "chiều mở rộng khai bằng cấu hình, không sửa code"
        đòi hỏi: thêm một chiều là thêm một dòng, không migration, không triển
        khai lại. Màn hình cho người dùng cuối hoãn tới v1.1 (RT-20); ở v1 đường
        vào là API quản trị và gói cấu hình phase 5.
        """
        validate_value_source(value_source, master_slug, catalogs=self._catalogs)
        dimension = AnalysisDimension(
            code=code,
            name=name,
            name_en=name_en,
            value_source=value_source,
            master_slug=master_slug,
            is_required=is_required,
            applies_to_accounts=applies_to_accounts,
        )
        self._session.add(dimension)
        self._session.flush()
        return dimension

    def add_value(
        self,
        dimension: AnalysisDimension,
        *,
        code: str,
        name: str,
        name_en: str | None = None,
        parent: AnalysisDimensionValue | None = None,
    ) -> AnalysisDimensionValue:
        """Thêm một giá trị vào chiều `list`, tự dựng `path`/`level` từ cha.

        Lấy khóa chính trước từ sequence, cùng lý do đã ghi ở
        `MasterDataService.create` (H40): `path` chứa chính id của dòng, nên
        đường "chèn rồi sửa" sinh ra bản ghi mới toanh mang `row_version = 2`
        kèm một dòng nhật ký "vừa tạo xong đã sửa".
        """
        self._ensure_list_source(dimension)
        if parent is not None and parent.dimension_id != dimension.id:
            raise DimensionValueSourceMismatchError(
                "Giá trị cha thuộc một chiều phân tích khác",
                code=code,
                dimension=dimension.code,
            )

        value_id = reserve_id(self._session, AnalysisDimensionValue.__tablename__)
        value = AnalysisDimensionValue(
            id=value_id,
            dimension_id=dimension.id,
            code=code,
            name=name,
            name_en=name_en,
            parent_id=parent.id if parent else None,
            path=child_path(parent.path if parent else None, value_id),
            level=(parent.level + 1) if parent else ROOT_LEVEL,
        )
        self._session.add(value)
        self._session.flush()
        return value

    # ------------------------------------------------------------- bất biến

    def resolve_value(self, dimension_code: str, value_code: str) -> AnalysisDimensionValue:
        """Tra một giá trị theo mã, **trong phạm vi chiều đã cho**.

        Luôn đi qua chiều chứ không tra thẳng `value_code` toàn bảng: mã giá trị
        chỉ duy nhất trong một chiều (xem ràng buộc ở `models.py`), nên một phép
        tra bỏ qua chiều sẽ trả về giá trị của chiều nào tình cờ đứng trước.
        """
        dimension = self.get_by_code(dimension_code)
        self._ensure_list_source(dimension)
        value = (
            self._session.execute(
                select(AnalysisDimensionValue)
                .where(AnalysisDimensionValue.dimension_id == dimension.id)
                .where(AnalysisDimensionValue.code == value_code)
            )
            .scalars()
            .first()
        )
        if value is None:
            raise DimensionValueNotFoundError(
                "Không có giá trị này trong chiều phân tích",
                dimension=dimension_code,
                code=value_code,
            )
        return value

    def resolve_value_by_id(
        self, dimension: AnalysisDimension, value_id: int
    ) -> AnalysisDimensionValue:
        """Tra một giá trị theo khóa chính, **kèm điều kiện thuộc đúng chiều**.

        Không dùng `session.get`: nó tra thẳng khóa chính và trả về giá trị của
        bất kỳ chiều nào. Ở đường ghi, một `parent_id` thuộc chiều khác sẽ tạo ra
        một nhánh vắt qua hai chiều — cây đó không có nghĩa nào cả và không có
        ràng buộc nào ở DB bắt được, vì cả hai dòng đều hợp lệ khi xét riêng.
        """
        value = (
            self._session.execute(
                select(AnalysisDimensionValue)
                .where(AnalysisDimensionValue.id == value_id)
                .where(AnalysisDimensionValue.dimension_id == dimension.id)
            )
            .scalars()
            .first()
        )
        if value is None:
            raise DimensionValueNotFoundError(
                "Không có giá trị này trong chiều phân tích",
                dimension=dimension.code,
                value_id=value_id,
            )
        return value

    def ensure_selectable(self, value: AnalysisDimensionValue) -> None:
        """Chặn chọn một giá trị đã ngừng theo dõi, hoặc thuộc chiều đã tắt.

        Kiểm **cả hai cấp**: tắt một chiều mà giá trị con vẫn chọn được thì việc
        tắt chiều chẳng có tác dụng gì ngoài việc ẩn nó khỏi danh sách — và
        chứng từ vẫn tiếp tục mang giá trị của một chiều không còn ai đọc.
        """
        dimension = self._session.get(AnalysisDimension, value.dimension_id)
        if dimension is None or not dimension.is_active:
            raise DimensionValueNotFoundError(
                "Chiều phân tích đã ngừng theo dõi", code=value.code, dimension=value.dimension_id
            )
        if not value.is_active:
            raise DimensionValueNotFoundError(
                "Giá trị đã ngừng theo dõi, không dùng cho chứng từ mới",
                code=value.code,
                dimension=dimension.code,
            )

    def _ensure_list_source(self, dimension: AnalysisDimension) -> None:
        if dimension.value_source is not DimensionValueSource.LIST:
            raise DimensionValueSourceMismatchError(
                "Chiều này lấy giá trị từ danh mục, không có danh sách giá trị riêng",
                dimension=dimension.code,
                master_slug=dimension.master_slug,
            )
