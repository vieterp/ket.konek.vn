/**
 * Khối "Mở rộng" thu gọn — nơi chứa trường phụ, ít dùng của một form (U2:
 * chỉ ba thứ thiết yếu hiện sẵn, phần còn lại vào khối này).
 *
 * `CatalogEditDrawer` từng tự vẽ đúng mẫu này bằng tay (nút `aria-expanded` +
 * state cục bộ). Gom vào design system vì hành vi bàn phím/ARIA đáng giữ đúng
 * ở một chỗ — form chứng từ (lát 4E) là nơi thứ hai cần y hệt.
 *
 * Không tự đóng khi mất focus hay khi form gửi lỗi: người dùng mở "Mở rộng"
 * để sửa tỷ giá thì khối đó biến mất ngay sau khi submit lỗi là mất định
 * hướng, phải mở lại từ đầu.
 */

import type { ReactElement, ReactNode } from 'react'
import { useId, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

export interface AdvancedSectionProps {
  readonly label: string
  readonly children: ReactNode
  /** Mở sẵn khi vẽ lần đầu — dùng khi form sửa đã có giá trị khác mặc định. */
  readonly defaultOpen?: boolean
}

export function AdvancedSection({
  label,
  children,
  defaultOpen = false,
}: AdvancedSectionProps): ReactElement {
  const [open, setOpen] = useState(defaultOpen)
  const panelId = useId()

  return (
    <div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => {
          setOpen((current) => !current)
        }}
        className="flex items-center gap-1 text-sm font-medium text-secondary hover:underline"
      >
        {open ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
        {label}
      </button>
      {open && (
        <div id={panelId} className="mt-3 flex flex-col gap-4">
          {children}
        </div>
      )}
    </div>
  )
}
