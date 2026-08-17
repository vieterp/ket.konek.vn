"""Đếm tham chiếu để trả lời "danh mục này xóa được chưa" (BR-SYS-02).

Câu hỏi nghe đơn giản, nhưng cách trả lời trực tiếp — quét mọi bảng chứng từ tìm
`partner_id = 47` — có hai vấn đề không sửa được bằng tối ưu: tới phase 9 có
hàng chục bảng phát sinh, nên phép quét là hàng chục lần quét, và nó phải chạy
**mỗi lần** người dùng bấm nút xóa hoặc chỉ để màn hình biết có nên làm mờ nút
đó không.

Cách ở đây đổi chiều: mỗi lần một chứng từ **dùng** danh mục thì bộ đếm nhích
lên, mỗi lần thôi dùng thì nhích xuống. Đọc trở thành một phép tra khóa chính.

Đánh đổi phải nói rõ: bộ đếm là dữ liệu **dẫn xuất**, nên nó lệch được — một
đường ghi mới ở phase sau quên gọi `record_use` sẽ cho xóa nhầm một danh mục
đang được dùng. Hai thứ giữ nó trung thực:

1. Bộ đếm cập nhật trong **cùng transaction** với việc ghi chứng từ (không có
   API nào ở đây tự mở transaction), nên nó không bao giờ lệch vì hỏng giữa chừng.
2. Bộ kiểm tra toàn vẹn ở phase 4 (FR-NFR-007) đối chiếu bộ đếm với số đếm thật
   và báo lệch — chỗ bắt được lỗi "quên gọi", thứ mà transaction không bắt được.

Ở phase 3 bảng này **chưa có người ghi**: chứng từ đầu tiên ra đời ở phase 6.
Nó có mặt từ đây vì `MasterDataService.delete` cần một câu trả lời ngay bây giờ,
và vì đổi cơ chế trả lời sau khi hai mươi danh mục đã dựng xong là việc đắt hơn
nhiều so với dựng đúng chỗ ngay từ đầu.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, Integer, String, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from ket.kernel.errors import MasterDataInUseError
from ket.kernel.persistence.base import DatasetBase

MASTER_DATA_USAGE_TABLE_NAME = "master_data_usage"


class MasterDataUsage(DatasetBase):
    """Số lần một bản ghi danh mục đang được tham chiếu.

    Cố ý **không** `Audited`: bộ đếm nhích theo mỗi dòng chứng từ, nên ghi vết
    nó sẽ nhân đôi khối lượng nhật ký để ghi lại một con số dẫn xuất từ chính
    những bản ghi đã có vết riêng. Cùng lập luận đã áp cho `number_sequences`.
    """

    __tablename__ = MASTER_DATA_USAGE_TABLE_NAME
    __table_args__ = (CheckConstraint("usage_count >= 0", name="usage_count_not_negative"),)

    entity_type: Mapped[str] = mapped_column(String(63), primary_key=True)
    """Tên bảng danh mục (`cost_objects`, `partners`…) — cùng quy ước với
    `audit_log.entity_type` và `attachments.entity_type`, không phải khóa ngoại.
    Một khóa ngoại cho mỗi bảng danh mục nghĩa là `ALTER TABLE` mỗi lần phase sau
    thêm một danh mục."""

    entity_id: Mapped[int] = mapped_column(Integer, primary_key=True)

    usage_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    """`BIGINT` vì nó đếm **dòng chứng từ**, không đếm chứng từ: một danh mục
    vật tư phổ biến qua mười năm ở doanh nghiệp thương mại vượt xa tầm `int`
    ngưỡng hai tỷ thì không, nhưng cột đếm là chỗ rẻ nhất để không phải nghĩ."""


def record_use(session: Session, *, entity_type: str, entity_id: int, delta: int = 1) -> None:
    """Cộng/trừ bộ đếm trong transaction đang mở của người gọi.

    `INSERT … ON CONFLICT DO UPDATE` chứ không "đọc rồi ghi": hai chứng từ cùng
    dùng một mã hàng được ghi đồng thời là chuyện thường ngày, và đọc-rồi-ghi
    làm mất một lượt đếm mỗi lần chúng chồng nhau. Với upsert, PostgreSQL nối
    tiếp hai lượt cộng trên cùng một dòng.

    **Cộng và trừ đi hai đường khác nhau**, có chủ đích (sửa sau review H2):

    * Cộng dùng upsert — dòng đếm ra đời cùng lần dùng đầu tiên.
    * Trừ dùng `UPDATE` thuần và **đòi** đúng một dòng bị ảnh hưởng. Không có
      dòng nào nghĩa là ai đó đang gỡ một tham chiếu chưa từng ghi nhận, và điều
      đó phải nổ: ở phase 6+ một đường "gỡ tham chiếu" chạy hai lần sẽ âm thầm
      mở khóa xóa cho một danh mục đang được dùng.

    Không gộp hai đường vào một upsert được: PostgreSQL kiểm `CHECK` trên **dòng
    đề xuất chèn** trước cả khi phát hiện xung đột, nên một upsert mang
    `usage_count = -1` đổ ngay cả khi dòng đã tồn tại và lẽ ra chỉ cần `UPDATE`.

    Ràng buộc `usage_count >= 0` lo nốt vế còn lại — trừ nhiều hơn cộng — ngay
    tại chỗ ghi, thay vì để một bộ đếm âm âm thầm cho xóa mọi thứ.
    """
    if delta == 0:
        raise ValueError("delta = 0 không có tác dụng — nhiều khả năng là lỗi gọi hàm")

    if delta < 0:
        # Khóa dòng trước khi trừ. Đọc-rồi-ghi ở Python là chỗ mất lượt đếm khi
        # hai chứng từ cùng thôi dùng một danh mục; `FOR UPDATE` nối tiếp chúng
        # đúng như upsert làm ở nhánh cộng. Đường trừ không phải đường nóng (nó
        # chạy khi chứng từ bị sửa hoặc xóa), nên cái giá nối tiếp là chấp nhận được.
        counter = (
            session.execute(
                select(MasterDataUsage)
                .where(
                    MasterDataUsage.entity_type == entity_type,
                    MasterDataUsage.entity_id == entity_id,
                )
                .with_for_update()
            )
            .scalars()
            .first()
        )
        if counter is None:
            raise ValueError(
                f"Gỡ tham chiếu tới {entity_type}#{entity_id} nhưng chưa có lần dùng nào "
                "được ghi nhận — bộ đếm và đường ghi đã lệch nhau"
            )
        counter.usage_count += delta
        session.flush()
        return

    statement = pg_insert(MasterDataUsage).values(
        entity_type=entity_type, entity_id=entity_id, usage_count=delta
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[MasterDataUsage.entity_type, MasterDataUsage.entity_id],
            set_={"usage_count": MasterDataUsage.usage_count + delta},
        )
    )


def usage_count_of(session: Session, *, entity_type: str, entity_id: int) -> int:
    """Số tham chiếu hiện tại; chưa có dòng nào nghĩa là chưa ai dùng."""
    count = session.execute(
        select(MasterDataUsage.usage_count).where(
            MasterDataUsage.entity_type == entity_type,
            MasterDataUsage.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    return count or 0


def ensure_deletable(session: Session, *, entity_type: str, entity_id: int) -> None:
    """Ném `MasterDataInUseError` nếu bản ghi đang được tham chiếu."""
    count = usage_count_of(session, entity_type=entity_type, entity_id=entity_id)
    if count > 0:
        raise MasterDataInUseError(
            "Không xóa được vì bản ghi đang được dùng — hãy chuyển sang mã khác "
            'hoặc đặt "Ngừng theo dõi"',
            entity_type=entity_type,
            entity_id=entity_id,
            usage_count=count,
        )
