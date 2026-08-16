/**
 * Lưới an toàn cuối cùng — kiểm rằng lỗi lúc render thành MÀN HÌNH CÓ CHỮ.
 *
 * Không kiểm được bằng `tsc` và không ai gặp trong lúc phát triển: đường này
 * chỉ chạy khi một thứ khác đã hỏng. Nhưng đó chính là lúc nó phải đúng.
 */

import type { ReactElement } from 'react'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AppErrorBoundary } from './error-boundary'

function Boom(): ReactElement {
  throw new Error('localStorage bị chặn')
}

function Fine(): ReactElement {
  return <p>Nội dung ứng dụng</p>
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AppErrorBoundary', () => {
  it('không xen vào khi không có lỗi', () => {
    render(
      <AppErrorBoundary>
        <Fine />
      </AppErrorBoundary>,
    )

    expect(screen.getByText('Nội dung ứng dụng')).toBeInTheDocument()
  })

  it('lỗi lúc render thành màn hình có chữ, không phải trang trắng', () => {
    // React ghi lỗi ra console dù đã bắt; im nó đi để output test đọc được.
    vi.spyOn(console, 'error').mockImplementation(() => undefined)

    render(
      <AppErrorBoundary>
        <Boom />
      </AppErrorBoundary>,
    )

    // `role="alert"` để trình đọc màn hình đọc ngay — người dùng không nhìn
    // thấy màn hình cũng phải biết ứng dụng vừa hỏng.
    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText('Ứng dụng gặp lỗi không mong muốn')).toBeInTheDocument()
  })

  it('hiện chính câu lỗi để người dùng đọc cho bộ phận hỗ trợ', () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined)

    render(
      <AppErrorBoundary>
        <Boom />
      </AppErrorBoundary>,
    )

    // "Gọi hỗ trợ mà không mô tả được gì" là kịch bản tệ nhất của phần mềm chạy
    // trong LAN — phải luôn có một dòng chữ để đọc qua điện thoại.
    expect(screen.getByText('localStorage bị chặn')).toBeInTheDocument()
  })

  it('ghi lỗi ra console để còn chẩn đoán', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    render(
      <AppErrorBoundary>
        <Boom />
      </AppErrorBoundary>,
    )

    expect(spy.mock.calls.some((call) => String(call[0]).includes('[ket]'))).toBe(true)
  })
})
