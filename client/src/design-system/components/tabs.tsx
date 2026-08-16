/**
 * Tab trạng thái — bộ xương của mọi màn hình danh sách (nguyên tắc U1).
 *
 * Tab ở sản phẩm này nêu **việc còn thiếu** ("Chờ duyệt", "Thiếu hóa đơn"), kèm
 * số lượng, chứ không nêu trạng thái kỹ thuật. Vì vậy `count` là một phần của
 * hợp đồng chứ không phải trang trí: một tab không có số là một tab người dùng
 * phải bấm vào mới biết có việc hay không.
 *
 * **Kích hoạt bằng tay, không tự động.** Mũi tên trái/phải chỉ **di chuyển
 * focus**; Enter hoặc Space mới đổi tab. WAI-ARIA cho phép cả hai kiểu và
 * khuyên kiểu tự động khi nội dung hiện ra tức thì — ở đây thì không: mỗi tab
 * là một truy vấn danh sách xuống server. Với kiểu tự động, người dùng bàn
 * phím lướt qua 5 tab sẽ bắn 5 truy vấn và thấy màn hình nhấp nháy bốn lần
 * trước khi tới chỗ mình muốn.
 *
 * Component vẽ luôn panel bọc `children` để `id`/`aria-controls`/
 * `aria-labelledby` khớp nhau từ bên trong — chỗ gọi không phải tự nghĩ ra id
 * duy nhất, và cũng không thể nối sai.
 */

import type { KeyboardEvent, ReactElement, ReactNode } from 'react'
import { useId, useRef } from 'react'

export interface TabItem {
  readonly id: string
  readonly label: string
  /** Số việc trong tab. `undefined` = không đếm được (chưa tải xong). */
  readonly count?: number
}

export interface TabsProps {
  readonly tabs: readonly TabItem[]
  readonly activeId: string
  readonly onChange: (id: string) => void
  /** Nhãn của cả dải tab cho trình đọc màn hình, ví dụ "Lọc theo trạng thái". */
  readonly label: string
  readonly children: ReactNode
}

export function Tabs({ tabs, activeId, onChange, label, children }: TabsProps): ReactElement {
  const baseId = useId()
  const listRef = useRef<HTMLDivElement>(null)

  const tabId = (id: string): string => `${baseId}-tab-${id}`
  const panelId = (id: string): string => `${baseId}-panel-${id}`

  // `activeId` không khớp tab nào là chuyện xảy ra thật: tab được lọc theo
  // quyền hoặc theo kỳ rồi tab đang chọn biến mất; `activeId` khôi phục từ một
  // dấu trang cũ; danh sách tab về sau `activeId`. Không xử lý thì MỌI tab đều
  // `tabIndex=-1` — cả dải biến mất khỏi thứ tự Tab và người dùng bàn phím mất
  // hẳn đường tới bộ lọc, đồng thời panel trỏ `aria-labelledby` vào một id
  // không tồn tại. Rơi về tab đầu.
  const activeIndex = Math.max(
    0,
    tabs.findIndex((tab) => tab.id === activeId),
  )
  const resolvedActiveId = tabs[activeIndex]?.id ?? activeId

  function focusTabAt(index: number): void {
    const buttons = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    buttons?.[index]?.focus()
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    const last = tabs.length - 1
    if (event.key === 'ArrowRight') {
      event.preventDefault()
      focusTabAt(index === last ? 0 : index + 1)
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault()
      focusTabAt(index === 0 ? last : index - 1)
    } else if (event.key === 'Home') {
      event.preventDefault()
      focusTabAt(0)
    } else if (event.key === 'End') {
      event.preventDefault()
      focusTabAt(last)
    }
  }

  return (
    <div className="flex flex-col">
      <div
        ref={listRef}
        role="tablist"
        aria-label={label}
        className="flex flex-wrap border-b border-border-default"
      >
        {tabs.map((tab, index) => {
          const active = tab.id === resolvedActiveId
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={tabId(tab.id)}
              aria-selected={active}
              aria-controls={panelId(tab.id)}
              // Roving tabindex: cả dải tab là MỘT điểm dừng của phím Tab. Nếu
              // mọi tab đều dừng được thì người dùng bàn phím phải bấm Tab qua
              // hết dải mới tới được bảng dữ liệu bên dưới.
              tabIndex={active ? 0 : -1}
              onClick={() => {
                onChange(tab.id)
              }}
              onKeyDown={(event) => {
                handleKeyDown(event, index)
              }}
              className={`-mb-px flex items-center gap-2 border-b-2 px-4 py-3 text-control transition-colors ${
                active
                  ? 'border-primary font-semibold text-primary'
                  : 'border-transparent text-text-muted hover:border-primary/40 hover:text-primary'
              }`}
            >
              {tab.label}
              {tab.count !== undefined && (
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-semibold ${
                    active ? 'bg-navy-700 text-white' : 'bg-surface text-text-muted'
                  }`}
                >
                  {tab.count}
                </span>
              )}
            </button>
          )
        })}
      </div>
      <div
        role="tabpanel"
        id={panelId(resolvedActiveId)}
        aria-labelledby={tabId(resolvedActiveId)}
        tabIndex={0}
        className="pt-4 outline-none"
      >
        {children}
      </div>
    </div>
  )
}
