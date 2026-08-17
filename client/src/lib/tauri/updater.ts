/**
 * Cầu nối tới đường tự cập nhật của shell (LD-05, FR-NFR-054, bước 18).
 *
 * **Đây là một trong những tệp duy nhất được import `@tauri-apps/*`** — luật
 * phụ thuộc #6, ép bằng `no-restricted-imports` trong `eslint.config.js`. Lý do
 * của luật: bundle web này còn phải mở được bằng trình duyệt thường trong LAN ở
 * v1.x, nên không luồng nghiệp vụ nào được gọi API Tauri. Tự cập nhật thì đúng
 * là việc chỉ desktop mới làm được, và cách giữ cả hai chế độ cùng chạy là gom
 * nó vào đây rồi trả `unsupported` khi không có shell.
 *
 * Việc thật nằm ở tầng Rust (`src-tauri/src/lib.rs`), không ở đây. Chỗ này chỉ
 * gọi lệnh và dịch kết quả: địa chỉ máy chủ cập nhật phải do Rust quyết, và
 * khóa công khai xác minh chữ ký thì ghim lúc dựng — hai điều đó là thứ làm cho
 * việc truyền địa chỉ từ JavaScript xuống vẫn an toàn.
 */

import { invoke } from '@tauri-apps/api/core'

export type UpdateOutcome =
  /** Đã tải và cài xong — phải khởi động lại để dùng bản mới. */
  | 'installed'
  /** Máy chủ không có bản nào mới hơn bản đang chạy. */
  | 'up-to-date'
  /** Đang chạy trong trình duyệt, không phải shell desktop. */
  | 'unsupported'

/**
 * Có đang chạy trong shell Tauri không.
 *
 * Kiểm `__TAURI_INTERNALS__` chứ không `navigator.userAgent`: webview của Tauri
 * dùng đúng user agent của engine hệ thống (WebKit trên macOS, WebView2 trên
 * Windows), nên không có gì trong đó phân biệt được với một trình duyệt thường.
 */
export function isDesktopShell(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

/**
 * Hỏi máy chủ đang dùng xem có bản mới không, và cài nếu có.
 *
 * Ném khi tải hoặc cài hỏng — chỗ gọi phải hiện lỗi đó cho người dùng, vì "bấm
 * cập nhật rồi không thấy gì xảy ra" là trạng thái tệ nhất ở màn hình này:
 * người dùng đang bị chặn ghi và không biết mình còn phải làm gì.
 */
export async function checkAndInstallUpdate(serverBaseUrl: string): Promise<UpdateOutcome> {
  if (!isDesktopShell()) {
    return 'unsupported'
  }
  const installed = await invoke<boolean>('check_and_install_update', {
    serverBaseUrl,
  })
  return installed ? 'installed' : 'up-to-date'
}

/**
 * Khởi động lại ứng dụng sau khi đã cài xong.
 *
 * Lệnh riêng, không gộp vào lệnh cài: tiến trình thoát thì Promise của lệnh cài
 * không bao giờ hoàn tất, nên màn hình không kịp báo "đã cập nhật xong". Tách
 * ra thì người dùng đọc được kết quả rồi tự bấm.
 */
export async function restartApp(): Promise<void> {
  if (!isDesktopShell()) {
    return
  }
  await invoke('restart_app')
}
