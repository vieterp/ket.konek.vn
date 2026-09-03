#!/usr/bin/env python3
"""Cổng đếm bài cho job test chia shard — bắt kiểu hỏng tệ nhất: bài rơi mất.

Job `server · test có PostgreSQL` chạy trên ba shard song song. Hai shard nhận
danh sách tệp tường minh, shard thứ ba chạy phần còn lại bằng `--ignore`. Cơ
chế ấy có đúng một cách hỏng nghiêm trọng, và nó **im lặng**: nếu danh sách
tường minh và danh sách `--ignore` lệch nhau — một tệp đổi tên, một dòng chép
thiếu — thì bài của tệp đó không chạy ở shard nào cả. Ba shard đều xanh, PR
được merge, và một vùng mã mất cổng canh mà không ai biết. (Trường hợp ngược
lại, một tệp chạy hai lần, chỉ tốn thời gian — nhưng cùng một cổng bắt được cả
hai, vì cả hai đều làm tổng lệch.)

Cổng này so **số bài thật sự chạy** (đọc từ junit XML mà chính ba shard xuất
ra) với **số bài pytest thu thập được trên toàn bộ** khi không chia. Không có
con số nào viết cứng trong tệp này: thêm bài mới thì cả hai vế cùng tăng. Một
cổng phải tự cập nhật, nếu không nó sẽ bị người ta sửa số cho hết đỏ.

Chạy:
    python3 .github/scripts/check_test_shards.py --expected 1917 junit-shard*.xml
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path


def count_tests(report: Path) -> int:
    """Số bài ghi trong một junit XML, gồm cả bài bị bỏ qua.

    Cộng cả `skipped`: một bài bị bỏ qua vẫn là một bài **được chọn**, và cổng
    này hỏi "shard nào chọn nó", không hỏi "nó có chạy tới nơi không". Trừ bài
    bỏ qua ra sẽ khiến cổng đỏ oan mỗi lần một bài được bỏ qua hợp lệ.
    """
    root = ElementTree.parse(report).getroot()
    # pytest bọc `<testsuite>` trong `<testsuites>`; `iter` xử được cả hai hình
    # dạng, nên cổng không vỡ nếu pytest đổi cách bọc ở bản sau.
    return sum(int(suite.get("tests", "0")) for suite in root.iter("testsuite"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected",
        type=int,
        required=True,
        help="số bài pytest thu thập trên TOÀN BỘ bộ test khi không chia shard",
    )
    parser.add_argument("reports", nargs="+", type=Path, help="junit XML của từng shard")
    args = parser.parse_args()

    missing = [str(path) for path in args.reports if not path.is_file()]
    if missing:
        # Thiếu báo cáo nghĩa là một shard không xuất được kết quả. Đó không
        # phải "chưa đủ dữ liệu để kết luận" — đó là chính cái hỏng cần bắt.
        print(f"::error::Thiếu junit XML của shard: {', '.join(missing)}")
        return 1

    per_shard = {path.name: count_tests(path) for path in sorted(args.reports)}
    actual = sum(per_shard.values())

    for name, count in per_shard.items():
        print(f"  {name}: {count} bài")
    print(f"  tổng các shard: {actual}   |   thu thập không chia: {args.expected}")

    if actual != args.expected:
        drift = actual - args.expected
        direction = "chạy hai lần ở nhiều shard" if drift > 0 else "KHÔNG chạy ở shard nào"
        print(
            f"::error::Chia shard làm lệch {abs(drift)} bài — có bài {direction}. "
            "Đối chiếu SHARD_1_FILES/SHARD_2_FILES trong ci.yml với các tệp có thật "
            "trong server/tests/ (tệp mới đổi tên là nguyên nhân thường gặp nhất)."
        )
        return 1

    print("Cổng đếm bài đạt: các shard cộng lại đúng bằng bộ test đầy đủ.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
