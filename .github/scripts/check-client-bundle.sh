#!/usr/bin/env bash
#
# Cổng phát hành: bundle client đã dựng KHÔNG được chứa công cụ dev.
#
# Vì sao phải grep bản dựng thật thay vì tin một bài test: cổng "chỉ có ở bản
# dev" là một hằng do bộ dựng thay thế, nên thứ quyết định nó không phải mã
# nguồn mà là CẤU HÌNH DỰNG và MÔI TRƯỜNG chạy lệnh dựng. Một bài test đơn vị
# ép cờ rồi khẳng định nhánh chỉ chứng minh mã phản ứng đúng với cờ — nó vẫn
# xanh trong khi bản dựng thật đặt cờ sai.
#
# Đã từng sai thật: hồi cổng còn là `import.meta.env.DEV`, chạy
# `NODE_ENV=development pnpm build` cho ra bundle có route `/kitchen-sink` —
# một đường vào ứng dụng không qua `SessionGate` — kèm sourcemap toàn bộ mã
# nguồn client. Test đơn vị vẫn xanh suốt.
#
# Script dựng vào một thư mục tạm để không giẫm lên `dist/` của người đang chạy
# dev, và cố ý dựng với `NODE_ENV=development` — chính môi trường từng phá được
# cổng cũ. Qua được phép thử này nghĩa là cổng không còn treo trên biến môi
# trường nữa.

set -euo pipefail

CLIENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../client" && pwd)"
OUT_DIR="$(mktemp -d)"
trap 'rm -rf "$OUT_DIR"' EXIT

cd "$CLIENT_DIR"

echo "Dựng thử với NODE_ENV=development (môi trường thù địch)…"
NODE_ENV=development pnpm exec vite build --outDir "$OUT_DIR" --emptyOutDir >/dev/null

# Chuỗi chỉ có thể đến từ trang duyệt dev. Thêm chuỗi mới vào đây khi thêm công
# cụ dev mới — danh sách này là hợp đồng, không phải gợi ý.
FORBIDDEN=(
  'kitchen-sink'
  'Design system — trang duyệt'
  # Trang đo lưới nhập liệu (spike S3). Ngoài việc bỏ qua `SessionGate`, nó còn
  # phơi một kênh đo trên `window` (`__gridBench`).
  'bench/data-grid'
  '__gridBench'
)

status=0

# Sourcemap mang theo `sourcesContent` — toàn bộ mã nguồn client — nên một bản
# dựng có `.map` là một installer phát tán mã nguồn cho khách. Điều kiện sinh
# sourcemap từng cũng treo trên `NODE_ENV`, đúng cùng một lỗi.
if find "$OUT_DIR" -name '*.map' -print -quit | grep -q .; then
  echo "  ✗ bản dựng có sourcemap (.map) — mã nguồn client sẽ đi theo installer"
  status=1
else
  echo "  ✓ không có sourcemap"
fi

for needle in "${FORBIDDEN[@]}"; do
  if grep -rqF -- "$needle" "$OUT_DIR"; then
    echo "  ✗ TÌM THẤY \"$needle\" trong bundle giao khách"
    status=1
  else
    echo "  ✓ không có \"$needle\""
  fi
done

# Đối chứng: nếu phép grep tự nó hỏng (sai đường dẫn, bundle rỗng) thì mọi kiểm
# tra ở trên đều "đạt" một cách vô nghĩa. Một chuỗi CHẮC CHẮN phải có mặt sẽ
# phát hiện điều đó.
if grep -rqF -- 'tien-vao-tien-ra' "$OUT_DIR"; then
  echo "  ✓ đối chứng: bundle có nội dung ứng dụng thật"
else
  echo "  ✗ đối chứng TRƯỢT — phép kiểm này không đọc được bundle, kết quả trên vô nghĩa"
  status=1
fi

if [ "$status" -ne 0 ]; then
  echo ""
  echo "Bundle giao khách chứa công cụ chỉ dành cho dev."
  echo "Cổng nằm ở \`__DEV_TOOLS__\` trong client/vite.config.ts (command === 'serve')."
  exit 1
fi

echo "Bundle sạch."
