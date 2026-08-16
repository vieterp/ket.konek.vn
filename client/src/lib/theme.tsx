/**
 * Chế độ sáng/tối — ba lựa chọn: `light`, `dark`, `system`.
 *
 * `tokens.css` đã dựng sẵn cả ba đường từ lát 2C-1, và cách bật khớp đúng với
 * cách nó được viết ở đó:
 *
 * | Lựa chọn | Thuộc tính trên `<html>` | Kết quả |
 * | --- | --- | --- |
 * | `system` | **không có** `data-theme` | `@media (prefers-color-scheme: dark)` tự quyết |
 * | `light`  | `data-theme="light"`      | media query bị loại (`:root:not([data-theme='light'])`) → luôn sáng |
 * | `dark`   | `data-theme="dark"`       | khối `:root[data-theme='dark']` → luôn tối |
 *
 * Vì vậy `system` phải **gỡ** thuộc tính chứ không phải đặt `data-theme="system"`:
 * một giá trị lạ nằm đó sẽ loại media query đi mà không bật gì thay thế, và kết
 * quả là chế độ tối của hệ điều hành ngừng có tác dụng — hỏng âm thầm, không
 * báo lỗi ở đâu cả.
 *
 * Lựa chọn lưu ở `localStorage` (khóa `ket.theme`) chứ không lưu trên server:
 * đây là thiết lập của **máy trạm** — cùng một kế toán viên ngồi máy ngoài sáng
 * và máy trong kho tối muốn hai chế độ khác nhau.
 */

import type { ReactElement, ReactNode } from 'react'
import { createContext, use, useCallback, useEffect, useMemo, useState } from 'react'

import { readStored, writeStored } from '@/lib/safe-storage'

export type ThemePreference = 'light' | 'dark' | 'system'

export const THEME_PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark']

const THEME_STORAGE_KEY = 'ket.theme'

interface Theme {
  readonly preference: ThemePreference
  readonly setPreference: (preference: ThemePreference) => void
}

const ThemeContext = createContext<Theme | null>(null)

function initialPreference(): ThemePreference {
  const stored = readStored(THEME_STORAGE_KEY)
  return stored === 'light' || stored === 'dark' ? stored : 'system'
}

/** Áp lựa chọn lên phần tử gốc — xem bảng ở đầu tệp. */
function applyPreference(preference: ThemePreference): void {
  if (preference === 'system') {
    document.documentElement.removeAttribute('data-theme')
  } else {
    document.documentElement.setAttribute('data-theme', preference)
  }
}

export function ThemeProvider({ children }: { children: ReactNode }): ReactElement {
  const [preference, setPreferenceState] = useState<ThemePreference>(initialPreference)

  // Áp cả lần đầu: giá trị đọc từ `localStorage` mới chỉ nằm trong state, chưa
  // có trên `<html>` — thiếu bước này thì mở lại ứng dụng luôn ra chế độ sáng
  // dù lần trước đã chọn tối.
  useEffect(() => {
    applyPreference(preference)
  }, [preference])

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next)
    writeStored(THEME_STORAGE_KEY, next)
  }, [])

  const value = useMemo<Theme>(() => ({ preference, setPreference }), [preference, setPreference])

  return <ThemeContext value={value}>{children}</ThemeContext>
}

export function useTheme(): Theme {
  const value = use(ThemeContext)
  if (value === null) {
    throw new Error('useTheme phải nằm trong <ThemeProvider>')
  }
  return value
}
