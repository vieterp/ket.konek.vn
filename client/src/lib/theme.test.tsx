/**
 * Chế độ sáng/tối — kiểm đúng cái bảng ba dòng trong docstring của `theme.tsx`.
 *
 * Bất biến quan trọng nhất: `system` phải **gỡ** `data-theme` chứ không đặt
 * `data-theme="system"`. Sai chỗ này thì chế độ tối của hệ điều hành ngừng có
 * tác dụng mà không có lỗi nào hiện ra ở đâu — đúng loại hỏng chỉ khách hàng
 * nhìn thấy.
 */

import type { ReactElement } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'

import { THEME_PREFERENCES, ThemeProvider, useTheme } from './theme'

function Probe(): ReactElement {
  const { preference, setPreference } = useTheme()
  return (
    <div>
      <span data-testid="preference">{preference}</span>
      {THEME_PREFERENCES.map((value) => (
        <button
          key={value}
          type="button"
          onClick={() => {
            setPreference(value)
          }}
        >
          {value}
        </button>
      ))}
    </div>
  )
}

function renderTheme(): void {
  render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  )
}

afterEach(() => {
  document.documentElement.removeAttribute('data-theme')
})

describe('ThemeProvider', () => {
  it('mặc định theo hệ thống và KHÔNG đặt data-theme', () => {
    renderTheme()

    expect(screen.getByTestId('preference')).toHaveTextContent('system')
    // `tokens.css` để media query `prefers-color-scheme` tự quyết khi không có
    // thuộc tính; một giá trị lạ nằm đó sẽ loại media query mà không bật gì thay.
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })

  it('chọn tối thì đặt data-theme="dark"', async () => {
    const user = userEvent.setup()
    renderTheme()

    await user.click(screen.getByRole('button', { name: 'dark' }))

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(localStorage.getItem('ket.theme')).toBe('dark')
  })

  it('chọn sáng thì đặt data-theme="light" — khóa media query lại', async () => {
    const user = userEvent.setup()
    renderTheme()

    await user.click(screen.getByRole('button', { name: 'light' }))

    // Cần thuộc tính thật (không phải chỉ gỡ dark): `:root:not([data-theme='light'])`
    // là cách `tokens.css` cho người dùng ép sáng dù hệ điều hành đang tối.
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
  })

  it('quay về "theo hệ thống" thì gỡ hẳn thuộc tính', async () => {
    const user = userEvent.setup()
    renderTheme()

    await user.click(screen.getByRole('button', { name: 'dark' }))
    await user.click(screen.getByRole('button', { name: 'system' }))

    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    expect(localStorage.getItem('ket.theme')).toBe('system')
  })

  it('áp lại lựa chọn đã lưu ngay lần vẽ đầu', () => {
    localStorage.setItem('ket.theme', 'dark')

    renderTheme()

    // Thiếu bước áp lúc khởi động thì mở lại ứng dụng luôn ra chế độ sáng dù
    // lần trước đã chọn tối — state đúng mà màn hình sai.
    expect(screen.getByTestId('preference')).toHaveTextContent('dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  })

  it('bỏ qua giá trị lạ trong localStorage', () => {
    localStorage.setItem('ket.theme', 'neon')

    renderTheme()

    expect(screen.getByTestId('preference')).toHaveTextContent('system')
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })

  it('dùng useTheme ngoài provider là lỗi ồn ào, không phải hỏng âm thầm', () => {
    expect(() => render(<Probe />)).toThrow(/ThemeProvider/)
  })
})
