"""Materialized path — phép toán thuần, không cần DB (FR-SYS-011).

Bốn hàm nhỏ ở `kernel/master_data/tree_path.py` là nơi mọi truy vấn cây dựa
vào. Chúng đủ nhỏ để trông như không cần test, và đó chính là lý do cần: một
dấu chấm thiếu ở cuối tiền tố biến `'1.4'` thành một mẫu khớp luôn `'1.40'`, và
sai đó chỉ hiện ra dưới dạng "báo cáo theo nhóm cộng dư một nhánh".
"""

from __future__ import annotations

import re

import pytest

from ket.kernel.master_data.tree_path import (
    PATH_PATTERN,
    child_path,
    is_descendant_of,
    level_of,
    like_prefix,
    move_subtree_params,
    move_subtree_sql,
)


def test_a_root_node_path_is_its_own_id() -> None:
    assert child_path(None, 7) == "7."
    assert level_of("7.") == 1


def test_a_child_path_extends_the_parent_path() -> None:
    assert child_path("1.4.", 17) == "1.4.17."
    assert level_of("1.4.17.") == 3


def test_paths_always_match_the_check_constraint_pattern() -> None:
    """Hàm sinh path và ràng buộc `CHECK` của DB phải nói cùng một thứ.

    Lệch nhau thì `INSERT` đổ ở tầng driver với một thông điệp về ràng buộc —
    ở đúng thao tác đầu tiên của người dùng, không ở CI.
    """
    pattern = re.compile(PATH_PATTERN)
    path = child_path(child_path(child_path(None, 1), 42), 999)
    assert pattern.match(path)


def test_a_node_id_must_be_positive() -> None:
    with pytest.raises(ValueError, match="node_id"):
        child_path(None, 0)


def test_the_like_prefix_does_not_match_a_sibling_with_a_longer_id() -> None:
    """`'1.4.'` **không** được khớp `'1.40.'` — đây là lý do có dấu chấm cuối."""
    assert like_prefix("1.4.") == "1.4.%"
    assert not "1.40.".startswith("1.4.")


def test_descendant_check_includes_the_node_itself() -> None:
    assert is_descendant_of("1.4.17.", "1.4.")
    assert is_descendant_of("1.4.", "1.4.")
    assert not is_descendant_of("1.5.", "1.4.")


def test_move_parameters_describe_the_shift_in_prefix_and_level() -> None:
    params = move_subtree_params(old_path="1.4.17.", new_path="2.17.")

    assert params["old_prefix"] == "1.4.17."
    assert params["new_prefix"] == "2.17."
    assert params["old_prefix_length"] == len("1.4.17.")
    assert params["level_delta"] == -1
    assert params["old_prefix_pattern"] == "1.4.17.%"


def test_the_move_statement_uses_bound_parameters_for_every_value() -> None:
    """Chỉ **tên bảng** được ghép vào SQL; mọi giá trị là tham số ràng buộc.

    Không phải chuyện thẩm mỹ: có tham số thì psycopg dùng extended protocol,
    và PostgreSQL từ chối nhiều câu lệnh trong một lần gửi — thứ chặn đường leo
    sang dataset khác (xem `tests/test_no_sql_string_interpolation.py`).
    """
    statement = move_subtree_sql("cost_objects")

    assert statement.startswith("UPDATE cost_objects ")
    for name in ("new_prefix", "old_prefix_length", "level_delta", "old_prefix_pattern"):
        assert f":{name}" in statement
    # `replace(...)` nói "thay ở bất kỳ đâu" trong khi việc cần làm là "cắt đúng
    # tiền tố ở đầu" — xem `move_subtree_sql`.
    assert "replace(" not in statement


def test_a_table_name_that_is_not_an_identifier_is_refused() -> None:
    with pytest.raises(ValueError):
        move_subtree_sql("cost_objects; DROP TABLE branches")
