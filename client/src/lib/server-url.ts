/**
 * Địa chỉ app server — **thiết lập của máy trạm, đổi được lúc chạy**.
 *
 * Trước lát 2C-4 địa chỉ này chỉ đến từ `VITE_KET_SERVER_URL` (đọc lúc `vite
 * build`) hoặc chính origin đang phục vụ trang. Hệ quả: một bản đóng gói Tauri
 * phải được **dựng riêng cho từng khách hàng**, vì máy host của mỗi nơi một
 * địa chỉ. Và khi địa chỉ ấy đổi — máy host đổi IP, đổi tên miền nội bộ, dọn
 * sang máy khác — thì cách duy nhất là dựng lại và cài lại trên từng máy trạm.
 *
 * Ba nguồn, theo thứ tự ưu tiên:
 *
 * 1. **Giá trị người dùng đã khai** (lưu ở máy trạm). Thắng mọi thứ khác — đây
 *    là điểm của cả module này.
 * 2. `VITE_KET_SERVER_URL` lúc dựng. Giờ là **giá trị mặc định cho lần chạy đầu**,
 *    không còn là thứ chốt hạ: bản đóng gói vẫn ghim được địa chỉ thường gặp để
 *    người dùng không phải gõ gì, nhưng đổi được mà không cần dựng lại.
 * 3. `window.location.origin`. Đúng cho chế độ trình duyệt trong LAN và chế độ
 *    một máy (app server tự phục vụ bundle). **Sai trong webview Tauri**, nơi
 *    origin là `tauri://localhost` — xem `isUsableServerUrl`.
 *
 * Lưu ở `localStorage` chứ không ở kho bí mật của Tauri: đây không phải bí mật,
 * và chế độ trình duyệt LAN ở v1.x cũng phải đổi được địa chỉ.
 */

import { readStored, removeStored, writeStored } from '@/lib/safe-storage'

const STORAGE_KEY = 'ket.serverUrl'

/**
 * Địa chỉ có gọi API được không.
 *
 * `tauri://localhost` là ca thật chứ không phải phòng xa: đó là origin của
 * webview Tauri trên macOS, và nó là giá trị `window.location.origin` trả về
 * trong bản đóng gói. Gọi API tới đó không bao giờ chạy, và updater còn từ chối
 * thẳng ở tầng Rust. Nhận ra sớm thì hiện được màn hình khai địa chỉ; không
 * nhận ra thì người dùng nhìn một màn hình "không tới được máy chủ" nói rằng nó
 * đang gọi `tauri://localhost`, và không hiểu vì sao.
 */
export function isUsableServerUrl(raw: string): boolean {
  try {
    const url = new URL(raw)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

/** Bỏ dấu `/` cuối để mọi chỗ ghép đường dẫn ra cùng một chuỗi. */
export function normalizeServerUrl(raw: string): string {
  return raw.trim().replace(/\/+$/, '')
}

/** Địa chỉ người dùng đã khai ở máy trạm này, `null` nếu chưa khai. */
export function storedServerUrl(): string | null {
  const stored = readStored(STORAGE_KEY)
  return stored !== null && isUsableServerUrl(stored) ? stored : null
}

export function storeServerUrl(raw: string): void {
  writeStored(STORAGE_KEY, normalizeServerUrl(raw))
}

export function forgetServerUrl(): void {
  removeStored(STORAGE_KEY)
}

/** Giá trị ghim lúc dựng, nếu bản dựng này có khai. */
export function buildTimeServerUrl(): string | null {
  const configured: unknown = import.meta.env.VITE_KET_SERVER_URL
  return typeof configured === 'string' && configured.length > 0
    ? normalizeServerUrl(configured)
    : null
}

/**
 * Địa chỉ app server sẽ dùng cho **cả** lời gọi API lẫn đường tự cập nhật.
 *
 * Trả `null` khi không có nguồn nào cho ra một địa chỉ gọi được — khi ấy chỗ
 * gọi phải hiện màn hình khai địa chỉ thay vì thử một lượt bắt tay chắc chắn
 * hỏng. Đó chính là tình huống của bản đóng gói Tauri chưa cấu hình gì.
 */
export function resolveServerUrl(): string | null {
  const stored = storedServerUrl()
  if (stored !== null) {
    return stored
  }
  const built = buildTimeServerUrl()
  if (built !== null && isUsableServerUrl(built)) {
    return built
  }
  const origin = normalizeServerUrl(window.location.origin)
  return isUsableServerUrl(origin) ? origin : null
}
