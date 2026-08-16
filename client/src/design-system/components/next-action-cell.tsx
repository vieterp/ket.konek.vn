/**
 * Ô "Việc tiếp theo" trong bảng danh sách (nguyên tắc U1).
 *
 * Cột này là điểm khác biệt lớn nhất giữa một bảng liệt kê và một bảng làm
 * được việc: nó không nói chứng từ *đang ở trạng thái nào*, nó nói *người đọc
 * phải làm gì tiếp theo* — và cho bấm vào để làm ngay tại chỗ.
 *
 * `action === null` nghĩa là **không còn việc**, và ô hiện `doneLabel` mờ đi
 * chứ không để trống: ô trống là thứ người dùng phải tự đoán ("chưa tải xong?
 * hay xong rồi?").
 *
 * Không nhận `href`: điều hướng là việc của tầng ứng dụng (`react-router`), và
 * design system phải nhập được vào bất kỳ ngữ cảnh nào — kể cả trang duyệt
 * `/kitchen-sink` và bài test không có router.
 */

import type { ReactElement } from 'react'

export interface NextActionCellProps {
  /** Câu việc cần làm. `null` = không còn việc. */
  readonly action: string | null
  /** Chữ hiện khi không còn việc, ví dụ "Đã xong". */
  readonly doneLabel: string
  /** Nhãn nút làm việc đó. Thiếu nút thì ô chỉ hiện câu việc. */
  readonly actionLabel?: string
  readonly onAction?: () => void
}

export function NextActionCell({
  action,
  doneLabel,
  actionLabel,
  onAction,
}: NextActionCellProps): ReactElement {
  if (action === null) {
    return <span className="text-sm text-text-muted">{doneLabel}</span>
  }

  return (
    <span className="flex items-center gap-2">
      <span className="text-sm text-text-default">{action}</span>
      {actionLabel !== undefined && onAction !== undefined && (
        <button
          type="button"
          onClick={onAction}
          className="rounded px-2 py-0.5 text-xs font-semibold text-secondary underline underline-offset-2 hover:bg-secondary/10"
        >
          {actionLabel}
        </button>
      )}
    </span>
  )
}
