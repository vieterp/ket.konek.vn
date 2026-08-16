/**
 * Khóa phiên theo **địa chỉ máy chủ** (quyết định H5).
 *
 * Đột biến "bỏ `baseUrl` khỏi khóa" sống sót qua toàn bộ bộ test ở review lát
 * 2C-1 — bất biến này chỉ tồn tại trong một đoạn docstring. Hậu quả thật: một
 * máy trạm mở song song bản cài thật và bản demo (chuyện thường ở giai đoạn
 * triển khai) sẽ giẫm phiên của nhau, và triệu chứng là "tự nhiên bị đá ra".
 */

import { describe, expect, it } from 'vitest'

import { clearStoredSession, readStoredSession, writeStoredSession } from '@/lib/session-storage'

const THAT = 'https://ket.cong-ty.lan:5443'
const DEMO = 'https://demo.cong-ty.lan:5443'

const SESSION = { token: 'abc', expiresAt: '2026-08-17T00:00:00Z', datasetCode: 'alpha' }

describe('kho phiên', () => {
  it('hai máy chủ khác nhau không dùng chung phiên', () => {
    writeStoredSession(THAT, SESSION)
    writeStoredSession(DEMO, { ...SESSION, token: 'xyz', datasetCode: 'beta' })

    expect(readStoredSession(THAT)?.token).toBe('abc')
    expect(readStoredSession(DEMO)?.token).toBe('xyz')
  })

  it('xóa phiên của máy chủ này không đụng máy chủ kia', () => {
    writeStoredSession(THAT, SESSION)
    writeStoredSession(DEMO, { ...SESSION, token: 'xyz' })

    clearStoredSession(THAT)

    expect(readStoredSession(THAT)).toBeNull()
    expect(readStoredSession(DEMO)?.token).toBe('xyz')
  })

  it('giá trị hỏng đọc thành "chưa đăng nhập", không làm trắng màn hình', () => {
    localStorage.setItem(`ket.session:${THAT}`, '{ khong-phai-json')

    expect(readStoredSession(THAT)).toBeNull()
  })
})
