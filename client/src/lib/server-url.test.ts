/**
 * Địa chỉ app server đổi được lúc chạy.
 *
 * Bất biến đắt nhất ở đây là **thứ tự ưu tiên**: giá trị người dùng khai phải
 * thắng giá trị ghim lúc dựng. Đảo lại thì bản đóng gói quay về chỗ cũ — mỗi
 * khách hàng một installer, và đổi địa chỉ máy host là dựng lại rồi cài lại
 * trên từng máy trạm.
 *
 * Bất biến thứ hai: `tauri://localhost` **không** phải một địa chỉ gọi được.
 * Đó là `window.location.origin` thật trong webview Tauri, nên nếu nó lọt qua
 * thì bản đóng gói chưa cấu hình sẽ hiện màn hình "không tới được máy chủ" kèm
 * một địa chỉ vô nghĩa, thay vì mời người dùng khai địa chỉ.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  forgetServerUrl,
  isUsableServerUrl,
  normalizeServerUrl,
  resolveServerUrl,
  storeServerUrl,
} from './server-url'

/** Đặt `window.location.origin` — jsdom không cho gán thẳng. */
function pretendOrigin(origin: string): void {
  Object.defineProperty(window, 'location', {
    value: { ...window.location, origin, reload: vi.fn() },
    configurable: true,
  })
}

const ORIGINAL_ORIGIN = window.location.origin

beforeEach(() => {
  forgetServerUrl()
  vi.unstubAllEnvs()
})

afterEach(() => {
  forgetServerUrl()
  pretendOrigin(ORIGINAL_ORIGIN)
  vi.unstubAllEnvs()
})

describe('isUsableServerUrl', () => {
  it('nhận http và https', () => {
    expect(isUsableServerUrl('http://127.0.0.1:5443')).toBe(true)
    expect(isUsableServerUrl('https://may-chu.noi-bo:5443')).toBe(true)
  })

  it('TỪ CHỐI `tauri://localhost` — origin thật của webview Tauri', () => {
    // Đây là ca thật, không phải phòng xa: đó là giá trị `window.location.origin`
    // trả về trong bản đóng gói macOS, và tầng Rust của updater cũng từ chối nó.
    expect(isUsableServerUrl('tauri://localhost')).toBe(false)
  })

  it('từ chối chuỗi không phải URL và sơ đồ lạ', () => {
    expect(isUsableServerUrl('may-chu.noi-bo:5443')).toBe(false)
    expect(isUsableServerUrl('file:///etc/passwd')).toBe(false)
    expect(isUsableServerUrl('')).toBe(false)
  })
})

describe('normalizeServerUrl', () => {
  it('bỏ khoảng trắng và dấu / cuối để mọi chỗ ghép ra cùng một chuỗi', () => {
    expect(normalizeServerUrl('  https://host:5443///  ')).toBe('https://host:5443')
  })
})

describe('resolveServerUrl — thứ tự ưu tiên', () => {
  it('giá trị người dùng khai THẮNG giá trị ghim lúc dựng', () => {
    vi.stubEnv('VITE_KET_SERVER_URL', 'https://ghim-luc-dung:5443')
    storeServerUrl('https://nguoi-dung-khai:5443')

    // Đảo thứ tự này là bản đóng gói quay về "mỗi khách một installer".
    expect(resolveServerUrl()).toBe('https://nguoi-dung-khai:5443')
  })

  it('chưa khai gì thì dùng giá trị ghim lúc dựng', () => {
    vi.stubEnv('VITE_KET_SERVER_URL', 'https://ghim-luc-dung:5443')

    expect(resolveServerUrl()).toBe('https://ghim-luc-dung:5443')
  })

  it('không ghim gì thì dùng chính origin đang phục vụ trang', () => {
    vi.stubEnv('VITE_KET_SERVER_URL', '')
    pretendOrigin('https://may-chu-lan:5443')

    // Đây là hình dạng của chế độ trình duyệt trong LAN và chế độ một máy.
    expect(resolveServerUrl()).toBe('https://may-chu-lan:5443')
  })

  it('trong webview Tauri chưa cấu hình gì → `null`, không phải một địa chỉ hỏng', () => {
    vi.stubEnv('VITE_KET_SERVER_URL', '')
    pretendOrigin('tauri://localhost')

    // `null` là tín hiệu để hiện màn hình khai địa chỉ. Trả `tauri://localhost`
    // thay vào đó là để người dùng nhìn một lỗi mạng họ không thể hiểu.
    expect(resolveServerUrl()).toBeNull()
  })

  it('giá trị đã lưu mà hỏng thì bị bỏ qua, không làm kẹt ứng dụng', () => {
    vi.stubEnv('VITE_KET_SERVER_URL', 'https://ghim-luc-dung:5443')
    localStorage.setItem('ket.serverUrl', 'khong-phai-url')

    // Người dùng gõ nhầm rồi đóng ứng dụng: lần mở sau phải rơi về mặc định
    // chứ không kẹt vĩnh viễn ở một địa chỉ không gọi được.
    expect(resolveServerUrl()).toBe('https://ghim-luc-dung:5443')
  })
})
