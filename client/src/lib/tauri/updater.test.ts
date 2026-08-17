/**
 * Cầu nối updater — bất biến quan trọng nhất là **chạy trong trình duyệt thì
 * không nổ**.
 *
 * Bundle web này còn phải mở được bằng Chrome/Edge trong LAN ở v1.x (luật phụ
 * thuộc #6). Ở đó không có shell Tauri nào, nên một lời gọi `invoke` sẽ ném —
 * và nó ném ở đúng màn hình mà người dùng đang bị chặn ghi, tức là chỗ tệ nhất
 * để hiện một lỗi không ai hiểu.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { checkAndInstallUpdate, isDesktopShell, restartApp } from './updater'

const invoke = vi.hoisted(() => vi.fn())
vi.mock('@tauri-apps/api/core', () => ({ invoke }))

function pretendDesktopShell(): void {
  // Tauri v2 nhận diện bằng chính khóa này; user agent của webview trùng với
  // trình duyệt hệ thống nên không phân biệt được bằng nó.
  Object.defineProperty(window, '__TAURI_INTERNALS__', { value: {}, configurable: true })
}

afterEach(() => {
  invoke.mockReset()
  Reflect.deleteProperty(window, '__TAURI_INTERNALS__')
})

describe('cầu nối updater', () => {
  it('trong trình duyệt: trả `unsupported` và KHÔNG gọi xuống shell', async () => {
    expect(isDesktopShell()).toBe(false)

    await expect(checkAndInstallUpdate('https://host:5443')).resolves.toBe('unsupported')
    expect(invoke).not.toHaveBeenCalled()
  })

  it('trong trình duyệt: khởi động lại là lệnh rỗng, không ném', async () => {
    await expect(restartApp()).resolves.toBeUndefined()
    expect(invoke).not.toHaveBeenCalled()
  })

  it('truyền địa chỉ máy chủ ĐANG DÙNG xuống shell', async () => {
    pretendDesktopShell()
    invoke.mockResolvedValue(true)

    await expect(checkAndInstallUpdate('https://ketserver.noi-bo:5443')).resolves.toBe('installed')

    // Địa chỉ phải đi xuống Rust: ghim host lúc dựng làm mọi máy trạm trong LAN
    // đi hỏi chính nó xem có bản mới không.
    expect(invoke).toHaveBeenCalledExactlyOnceWith('check_and_install_update', {
      serverBaseUrl: 'https://ketserver.noi-bo:5443',
    })
  })

  it('shell trả `false` nghĩa là chưa có bản mới, không phải lỗi', async () => {
    pretendDesktopShell()
    invoke.mockResolvedValue(false)

    await expect(checkAndInstallUpdate('https://host:5443')).resolves.toBe('up-to-date')
  })

  it('lỗi từ shell đi thẳng lên chỗ gọi', async () => {
    pretendDesktopShell()
    invoke.mockRejectedValue(new Error('tải hoặc cài bản cập nhật không thành công: chữ ký sai'))

    // Nuốt lỗi ở đây là "bấm cập nhật rồi không thấy gì xảy ra" — trạng thái tệ
    // nhất ở màn hình này, vì người dùng đang bị chặn ghi và không biết làm gì tiếp.
    await expect(checkAndInstallUpdate('https://host:5443')).rejects.toThrow('chữ ký sai')
  })
})
