"""Luật dọn dòng giá khi gộp hai đơn vị tính — bảng giá trị biên (lát 7C-1).

`plan_unit_merge_cleanup` là hàm **thuần**, nên nó kiểm được ở đây bằng bảng giá
trị biên thay vì bằng một lần gộp thật qua DB. Đó là lý do luật ấy bị tách khỏi
hai hook: ca đắt nhất của nó — dòng trở thành "trỏ đúng đơn vị chính" **sau** khi
`merge_service` dời `items.base_unit_id` — chỉ dựng được qua HTTP bằng bốn bản ghi
và hai lần gộp, và nó là ca dễ viết sai nhất.

Phần đi qua DB thật (hook chạy đúng lúc, `audit_log` có vết) nằm ở
`test_item_pricing_api.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from ket.kernel.master_data.unit_priced_rows import plan_unit_merge_cleanup

SOURCE = 7
TARGET = 9
OTHER = 11


@dataclass
class Row:
    """Dòng giá tối giản — đúng hai thuộc tính mà luật dọn nhìn tới."""

    unit_id: int | None
    slot: str


def _plan(rows: list[Row], *, base_unit_id: int | None) -> tuple[list[str], list[str]]:
    to_delete, to_base_unit = plan_unit_merge_cleanup(
        rows,
        base_unit_id=base_unit_id,
        unit_of=lambda row: row.unit_id,
        slot_of=lambda row: row.slot,
        source_id=SOURCE,
        target_id=TARGET,
    )
    return (
        [f"{row.unit_id}:{row.slot}" for row in to_delete],
        [f"{row.unit_id}:{row.slot}" for row in to_base_unit],
    )


def test_rows_on_other_units_are_left_alone() -> None:
    """Đơn vị không dính lần gộp thì không ai đụng tới — kể cả cùng ô."""
    rows = [Row(OTHER, "a"), Row(None, "a")]
    assert _plan(rows, base_unit_id=OTHER) == ([], [])


def test_the_target_row_wins_when_both_units_claim_one_slot() -> None:
    """Hai cách viết của cùng một dòng: bản ghi được **giữ lại** là bản quyết định."""
    rows = [Row(SOURCE, "a"), Row(TARGET, "a")]
    deleted, to_base = _plan(rows, base_unit_id=OTHER)
    assert deleted == [f"{SOURCE}:a"]
    assert to_base == []


def test_the_order_of_the_rows_does_not_change_who_wins() -> None:
    """Dòng của đơn vị đích thắng dù nó đứng sau trong danh sách.

    Không có phép sắp xếp trong hàm thì kết quả đổi theo thứ tự Postgres trả về —
    và một câu trả lời đổi theo thứ tự quét là thứ không ai gỡ được khi nó sai.
    """
    deleted_first, _ = _plan([Row(TARGET, "a"), Row(SOURCE, "a")], base_unit_id=OTHER)
    deleted_second, _ = _plan([Row(SOURCE, "a"), Row(TARGET, "a")], base_unit_id=OTHER)
    assert deleted_first == deleted_second == [f"{SOURCE}:a"]


def test_rows_in_different_slots_both_survive() -> None:
    """Hai ô khác nhau là hai câu hỏi khác nhau — không có gì để hợp nhất."""
    assert _plan([Row(SOURCE, "a"), Row(TARGET, "b")], base_unit_id=OTHER) == ([], [])


def test_a_row_becomes_the_base_unit_row_when_the_base_unit_is_merged_away() -> None:
    """Đơn vị chính là **nguồn**: sau lần gộp nó thành đích, nên dòng theo đích
    phải viết lại bằng `NULL`.

    Đây là hình dạng dễ bỏ sót nhất: không dòng nào trùng dòng nào, nhưng sau
    `UPDATE` của `merge_service` thì dòng ấy trỏ đúng đơn vị chính — trạng thái mà
    `ensure_unit_is_priceable` cấm ở mọi đường ghi.
    """
    deleted, to_base = _plan([Row(TARGET, "a")], base_unit_id=SOURCE)
    assert deleted == []
    assert to_base == [f"{TARGET}:a"]


def test_the_same_happens_when_the_base_unit_is_the_merge_target() -> None:
    """Đơn vị chính là **đích**: dòng theo nguồn cũng thành dòng của đơn vị chính."""
    deleted, to_base = _plan([Row(SOURCE, "a")], base_unit_id=TARGET)
    assert deleted == []
    assert to_base == [f"{SOURCE}:a"]


def test_a_row_is_dropped_only_when_the_base_unit_slot_is_already_taken() -> None:
    """Ô `NULL` đã có chủ thì không còn chỗ giữ — lúc đó mới xóa.

    Giá là dữ liệu người dùng khai; xóa nó khi còn chỗ để giữ là làm mất số mà
    lần gộp không hề nhắc tới.
    """
    deleted, to_base = _plan([Row(None, "a"), Row(TARGET, "a")], base_unit_id=SOURCE)
    assert deleted == [f"{TARGET}:a"]
    assert to_base == []


def test_both_merged_units_collapse_into_one_base_unit_row() -> None:
    """Cả hai cùng khai một ô **và** đơn vị chính bị gộp: một dòng về `NULL`, một bỏ."""
    deleted, to_base = _plan([Row(SOURCE, "a"), Row(TARGET, "a")], base_unit_id=TARGET)
    assert to_base == [f"{TARGET}:a"]
    assert deleted == [f"{SOURCE}:a"]


def test_a_taken_base_slot_drops_both_merged_rows() -> None:
    """Ô `NULL` đã có chủ và cả hai đơn vị cùng khai ô ấy: cả hai bỏ đi."""
    deleted, to_base = _plan(
        [Row(None, "a"), Row(SOURCE, "a"), Row(TARGET, "a")], base_unit_id=SOURCE
    )
    assert to_base == []
    assert sorted(deleted) == sorted([f"{SOURCE}:a", f"{TARGET}:a"])


def test_slots_are_independent_of_each_other() -> None:
    """Ô `NULL` của ô `a` đã có chủ không nói gì về ô `b`."""
    deleted, to_base = _plan(
        [Row(None, "a"), Row(TARGET, "a"), Row(TARGET, "b")], base_unit_id=SOURCE
    )
    assert deleted == [f"{TARGET}:a"]
    assert to_base == [f"{TARGET}:b"]
