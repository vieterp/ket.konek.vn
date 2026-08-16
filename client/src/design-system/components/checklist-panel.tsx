/**
 * Danh mục kiểm tra có kết quả từng mục — khung của màn hình khóa sổ
 * (nguyên tắc U11) và của mọi chỗ "chạy xong mới cho làm bước tiếp".
 *
 * Điều làm nó khác một danh sách lỗi thông thường: **mỗi mục hỏng dẫn thẳng
 * tới chỗ sửa**. Một bảng liệt kê "còn 3 chứng từ chưa ghi sổ" mà không nói ba
 * chứng từ nào thì người dùng phải tự đi tìm — và đó chính là công đoạn tốn
 * thời gian nhất của kỳ khóa sổ.
 *
 * Kết quả tổng không tự suy ra ở đây mà do chỗ gọi truyền vào qua `summary`:
 * "đủ điều kiện khóa sổ hay chưa" là một luật nghiệp vụ, và luật nghiệp vụ nằm
 * ở server (LD-03) chứ không nằm trong một component vỏ.
 */

import type { ReactElement } from 'react'
import { AlertCircle, CheckCircle2, Circle } from 'lucide-react'

import type { StatusTone } from './status-pill'
import { StatusPill } from './status-pill'

export type ChecklistStatus = 'passed' | 'failed' | 'pending'

/**
 * Ánh xạ sang ba tông của design. `pending` ("đang chạy", "chưa lập") là **việc
 * còn phải làm** nên nó là `todo`, không phải một tông riêng — xem bảng nghĩa
 * trong `status-pill.tsx`.
 */
const STATUS_TONE: Record<ChecklistStatus, StatusTone> = {
  passed: 'ok',
  failed: 'bad',
  pending: 'todo',
}

const STATUS_ICON = {
  passed: CheckCircle2,
  failed: AlertCircle,
  pending: Circle,
} as const

/** Icon lấy màu từ chính tông trạng thái — không có bảng màu thứ hai để lệch. */
const STATUS_ICON_CLASS: Record<ChecklistStatus, string> = {
  passed: 'text-status-ok',
  failed: 'text-status-bad',
  pending: 'text-status-todo',
}

export interface ChecklistItem {
  readonly id: string
  readonly label: string
  readonly status: ChecklistStatus
  /** Nhãn trạng thái hiện trên pill, ví dụ "Đạt" / "Còn 3 chứng từ". */
  readonly statusLabel: string
  /** Câu giải thích thêm — vì sao hỏng, hỏng ở đâu. */
  readonly detail?: string
  /** Nhãn nút dẫn tới chỗ sửa. Chỉ có nghĩa khi kèm `onFix`. */
  readonly fixLabel?: string
  readonly onFix?: () => void
}

export interface ChecklistPanelProps {
  readonly title: string
  readonly items: readonly ChecklistItem[]
  /** Câu tổng kết do chỗ gọi quyết định ("Đã đủ điều kiện khóa sổ"). */
  readonly summary?: string
}

export function ChecklistPanel({ title, items, summary }: ChecklistPanelProps): ReactElement {
  return (
    <section className="rounded border border-border-default bg-background">
      <header className="border-b border-border-default px-4 py-3">
        <h2 className="text-sm font-semibold text-primary">{title}</h2>
        {summary !== undefined && <p className="mt-1 text-xs text-text-muted">{summary}</p>}
      </header>
      <ul className="divide-y divide-border-default">
        {items.map((item) => {
          const Icon = STATUS_ICON[item.status]
          return (
            <li key={item.id} className="flex items-start gap-3 px-4 py-3">
              <Icon size={18} aria-hidden className={`mt-0.5 shrink-0 ${STATUS_ICON_CLASS[item.status]}`} />
              <div className="min-w-0 flex-1">
                <p className="text-sm text-text-default">{item.label}</p>
                {item.detail !== undefined && (
                  <p className="mt-0.5 text-xs text-text-muted">{item.detail}</p>
                )}
              </div>
              <StatusPill tone={STATUS_TONE[item.status]}>{item.statusLabel}</StatusPill>
              {item.fixLabel !== undefined && item.onFix !== undefined && (
                <button
                  type="button"
                  onClick={item.onFix}
                  className="shrink-0 rounded px-2 py-0.5 text-xs font-semibold text-secondary underline underline-offset-2 hover:bg-secondary/10"
                >
                  {item.fixLabel}
                </button>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
