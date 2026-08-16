/**
 * Kho bị chặn phải thành "quay về mặc định", không phải thành trắng màn hình.
 *
 * Cách kiểm: thay `localStorage` bằng một bản luôn ném `SecurityError`, đúng
 * như Safari riêng tư và máy bị chính sách doanh nghiệp khóa DOM storage làm.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { readStored, removeStored, writeStored } from './safe-storage'

function blockStorage(): void {
  const throwing = {
    getItem: () => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    },
    setItem: () => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    },
    removeItem: () => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    },
  }
  vi.stubGlobal('localStorage', throwing)
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('safe-storage', () => {
  it('đọc/ghi/xóa bình thường khi kho chạy được', () => {
    writeStored('ket.demo', 'giá trị')
    expect(readStored('ket.demo')).toBe('giá trị')

    removeStored('ket.demo')
    expect(readStored('ket.demo')).toBeNull()
  })

  it('khóa không tồn tại trả null', () => {
    expect(readStored('ket.không-có')).toBeNull()
  })

  it('kho bị chặn: đọc trả null thay vì ném', () => {
    blockStorage()

    // Đây là phép khẳng định quan trọng nhất của tệp: `readStored` được gọi
    // trong `useState` initializer, tức là TRONG LÚC RENDER. Một exception ở đó
    // lan tới `createRoot` và xóa sạch cây React — người dùng thấy trang trắng,
    // không thông báo, không lối ra.
    expect(() => readStored('ket.theme')).not.toThrow()
    expect(readStored('ket.theme')).toBeNull()
  })

  it('kho bị chặn: ghi và xóa im lặng bỏ qua', () => {
    blockStorage()

    // Mất thiết lập giao diện là phiền; không mở được phần mềm kế toán là hỏng
    // việc. Nên hỏng ở đây = không làm gì.
    expect(() => {
      writeStored('ket.theme', 'dark')
    }).not.toThrow()
    expect(() => {
      removeStored('ket.theme')
    }).not.toThrow()
  })
})
