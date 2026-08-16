/**
 * Điều khiển phân đoạn — chọn MỘT trong vài phương án ngắn, luôn thấy hết mọi
 * phương án. Nguồn: `.seg` trong design "Konek Screens 2a" ("Chưa kê khai / Đã
 * kê khai", "Trả sau / Trả ngay").
 *
 * Khác `Tabs` ở chỗ nào — hai component này trông giống nhau nên rất dễ dùng
 * nhầm:
 *
 * | | `Tabs` | `Seg` |
 * | --- | --- | --- |
 * | Đổi cái gì | **nội dung** cả khung bên dưới | **một giá trị** trên form/bộ lọc |
 * | Có panel riêng | có (`role="tabpanel"`) | không |
 * | Số lượng | nhiều, có thể tràn dòng | 2–4, chia đều bề ngang |
 * | Kích hoạt | bằng tay (Enter) vì mỗi tab là một truy vấn | ngay khi chọn |
 *
 * Vì `Seg` là "chọn một giá trị" chứ không phải "đi tới một chỗ khác", nó dùng
 * `role="radiogroup"` — trình đọc màn hình đọc "2 trong 3", đúng thứ người dùng
 * cần biết. Mũi tên chuyển **và chọn** luôn, đúng khuôn radio gốc của nền tảng:
 * ở đây đổi lựa chọn không tốn một vòng gọi server như `Tabs`.
 */

import type { KeyboardEvent, ReactElement } from 'react'
import { useRef } from 'react'

export interface SegOption {
  readonly value: string
  readonly label: string
}

export interface SegProps {
  readonly options: readonly SegOption[]
  readonly value: string
  readonly onChange: (value: string) => void
  /** Nhãn cả nhóm cho trình đọc màn hình, ví dụ "Tình trạng kê khai". */
  readonly label: string
}

export function Seg({ options, value, onChange, label }: SegProps): ReactElement {
  const groupRef = useRef<HTMLDivElement>(null)

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number): void {
    const last = options.length - 1
    let next: number | null = null
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      next = index === last ? 0 : index + 1
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      next = index === 0 ? last : index - 1
    } else if (event.key === 'Home') {
      next = 0
    } else if (event.key === 'End') {
      next = last
    }
    if (next === null) {
      return
    }
    event.preventDefault()
    const option = options[next]
    if (option === undefined) {
      return
    }
    onChange(option.value)
    groupRef.current?.querySelectorAll<HTMLButtonElement>('[role="radio"]')[next]?.focus()
  }

  return (
    <div
      ref={groupRef}
      role="radiogroup"
      aria-label={label}
      className="inline-flex overflow-hidden rounded border border-border-default"
    >
      {options.map((option, index) => {
        const selected = option.value === value
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            // Roving tabindex: cả nhóm là MỘT điểm dừng của phím Tab, giống
            // hệt cách một nhóm `<input type="radio">` gốc hoạt động.
            tabIndex={selected ? 0 : -1}
            onClick={() => {
              onChange(option.value)
            }}
            onKeyDown={(event) => {
              handleKeyDown(event, index)
            }}
            // `whitespace-nowrap`: nhãn phân đoạn là hai, ba chữ và phải đọc
            // được trên MỘT dòng — "Chưa kê khai" gãy làm đôi biến một điều
            // khiển gọn thành một khối chữ lộn xộn.
            className={`flex-1 whitespace-nowrap border-r border-border-default px-3 py-2 text-center text-control last:border-r-0 transition-colors ${
              selected
                ? 'bg-navy-700 font-semibold text-white'
                : 'text-text-default hover:bg-primary/10'
            }`}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}
