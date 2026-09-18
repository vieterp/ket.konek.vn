/**
 * Thẻ `.w2-card` + đầu thẻ `.w2-ch` của design (viền 1px, tiêu đề navy đậm 14px,
 * ô phụ bên phải) — khung của mọi khối trên form hóa đơn mua. Chỉ là bố cục,
 * không state.
 */

import type { ReactElement, ReactNode } from 'react'

export function FormCard({
  title,
  aside,
  children,
  className = '',
}: {
  readonly title: string
  /** Chữ/nút nhỏ bên phải đầu thẻ ("3 trường bắt buộc", "Thêm dòng"). */
  readonly aside?: ReactNode
  readonly children: ReactNode
  readonly className?: string
}): ReactElement {
  return (
    <section
      aria-label={title}
      className={`flex flex-col rounded border border-border-default bg-background ${className}`}
    >
      <header className="flex items-center gap-2 border-b border-border-default px-3.5 py-2.5">
        <h2 className="text-sm font-semibold text-primary">{title}</h2>
        {aside !== undefined && <div className="ml-auto text-xs text-text-muted">{aside}</div>}
      </header>
      {children}
    </section>
  )
}
