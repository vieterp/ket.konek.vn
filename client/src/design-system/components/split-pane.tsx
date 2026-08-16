/**
 * Chia đôi trái/phải, kéo được — bố cục của màn hình đối chiếu (nguyên tắc U5:
 * sao kê ngân hàng bên trái ↔ sổ kế toán bên phải).
 *
 * Vì sao phải kéo được: hai bên có số cột rất khác nhau tùy ngân hàng, và người
 * đối chiếu ngồi cả buổi với đúng một tỉ lệ hợp mắt mình. Tỉ lệ đó nhớ lại được
 * (`storageKey`) nên mở lại màn hình không phải kéo lại từ đầu.
 *
 * **Kéo được bằng bàn phím**, không chỉ bằng chuột: thanh chia là một
 * `role="separator"` có `tabIndex`, mũi tên trái/phải xê dịch từng nấc, Home/End
 * về hai biên. Một thanh chia chỉ kéo được bằng chuột là một tính năng không
 * tồn tại với người dùng bàn phím — mà đây là phần mềm nhập liệu, phần lớn thao
 * tác trong ngày không rời bàn phím.
 *
 * Trạng thái tỉ lệ nằm **trong** component: chỗ gọi không có việc gì với con số
 * này, và bắt nó giữ hộ chỉ tạo thêm một `useState` ở mọi màn hình đối chiếu.
 */

import type { KeyboardEvent, PointerEvent as ReactPointerEvent, ReactElement, ReactNode } from 'react'
import { useCallback, useRef, useState } from 'react'

/** Nấc xê dịch mỗi lần bấm mũi tên, tính theo phần trăm bề rộng. */
const KEYBOARD_STEP = 2

/**
 * `localStorage` **ném** khi trình duyệt chặn site-data (Safari riêng tư, chính
 * sách doanh nghiệp) — không phải trả `null`. Đọc trong `useState` initializer
 * là đọc trong lúc render, nên một exception ở đây làm trắng màn hình cả cây
 * React phía trên.
 *
 * Tầng ứng dụng có `lib/safe-storage.ts` làm đúng việc này, nhưng component
 * design system **không được phụ thuộc `src/lib/`** (xem `components/index.ts`)
 * — nên chỗ này giữ bản riêng. Sáu dòng trùng lặp là cái giá có chủ đích để
 * tầng design system vẫn là tầng lá.
 */
function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Cố ý im lặng: mất tỉ lệ khung đã lưu không ngăn người dùng làm việc.
  }
}

export interface SplitPaneProps {
  readonly left: ReactNode
  readonly right: ReactNode
  /** Nhãn thanh chia cho trình đọc màn hình, ví dụ "Kéo để đổi bề rộng hai khung". */
  readonly separatorLabel: string
  /** Phần trăm bề rộng dành cho khung trái (mặc định 50). */
  readonly initialPercent?: number
  readonly minPercent?: number
  readonly maxPercent?: number
  /** Có thì tỉ lệ được nhớ trong `localStorage` dưới khóa `ket.split.<khóa>`. */
  readonly storageKey?: string
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

export function SplitPane({
  left,
  right,
  separatorLabel,
  initialPercent = 50,
  minPercent = 20,
  maxPercent = 80,
  storageKey,
}: SplitPaneProps): ReactElement {
  const containerRef = useRef<HTMLDivElement>(null)

  const [percent, setPercentState] = useState<number>(() => {
    if (storageKey === undefined) {
      return clamp(initialPercent, minPercent, maxPercent)
    }
    const stored = Number(readStored(`ket.split.${storageKey}`))
    // `Number('')` là 0 và `Number(null)` cũng là 0 — không phân biệt được với
    // một giá trị đã lưu hợp lệ, nên phải kiểm hữu hạn VÀ khác 0 trước khi tin.
    return Number.isFinite(stored) && stored > 0
      ? clamp(stored, minPercent, maxPercent)
      : clamp(initialPercent, minPercent, maxPercent)
  })

  // Biên có thể đổi sau khi đã dựng (màn hình đối chiếu nới `minPercent` khi
  // sao kê có thêm cột). Giá trị cũ nằm ngoài biên mới thì `aria-valuenow` vượt
  // `aria-valuemax` — hợp đồng ARIA sai — và một khung tràn quá giới hạn vừa
  // đặt ra. Siết ngay lúc vẽ thay vì trong `useEffect`: sửa trong effect nghĩa
  // là có đúng một khung hình vẽ ra với giá trị sai.
  const bounded = clamp(percent, minPercent, maxPercent)

  const setPercent = useCallback(
    (next: number) => {
      const next_ = clamp(next, minPercent, maxPercent)
      setPercentState(next_)
      if (storageKey !== undefined) {
        writeStored(`ket.split.${storageKey}`, String(Math.round(next_)))
      }
    },
    [minPercent, maxPercent, storageKey],
  )

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>): void {
    const separator = event.currentTarget
    // Bắt con trỏ để việc kéo không đứt khi chuột lướt qua một khung con có
    // `overflow` riêng — triệu chứng kinh điển: kéo nhanh thì thanh chia "rớt".
    separator.setPointerCapture(event.pointerId)

    const move = (moveEvent: PointerEvent): void => {
      const rect = containerRef.current?.getBoundingClientRect()
      if (rect === undefined || rect.width === 0) {
        return
      }
      setPercent(((moveEvent.clientX - rect.left) / rect.width) * 100)
    }
    const up = (): void => {
      separator.removeEventListener('pointermove', move)
      separator.removeEventListener('pointerup', up)
    }
    separator.addEventListener('pointermove', move)
    separator.addEventListener('pointerup', up)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'ArrowLeft') {
      event.preventDefault()
      setPercent(bounded - KEYBOARD_STEP)
    } else if (event.key === 'ArrowRight') {
      event.preventDefault()
      setPercent(bounded + KEYBOARD_STEP)
    } else if (event.key === 'Home') {
      event.preventDefault()
      setPercent(minPercent)
    } else if (event.key === 'End') {
      event.preventDefault()
      setPercent(maxPercent)
    }
  }

  return (
    <div ref={containerRef} className="flex h-full w-full items-stretch">
      <div className="min-w-0 overflow-auto" style={{ width: `${String(bounded)}%` }}>
        {left}
      </div>
      <div
        role="separator"
        aria-orientation="vertical"
        aria-label={separatorLabel}
        aria-valuenow={Math.round(bounded)}
        aria-valuemin={minPercent}
        aria-valuemax={maxPercent}
        tabIndex={0}
        onPointerDown={handlePointerDown}
        onKeyDown={handleKeyDown}
        className="w-2 shrink-0 cursor-col-resize bg-border-default outline-none hover:bg-ocean-300 focus:bg-ocean-500"
      />
      <div className="min-w-0 flex-1 overflow-auto">{right}</div>
    </div>
  )
}
