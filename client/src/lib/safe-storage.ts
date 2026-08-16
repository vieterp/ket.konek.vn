/**
 * `localStorage` nuốt lỗi — vì `localStorage` **ném** chứ không trả `null`.
 *
 * Không phải chuyện lý thuyết. Trình duyệt chặn site-data làm chính lời gọi
 * `localStorage.getItem` ném `SecurityError`: Safari ở chế độ riêng tư, chính
 * sách nhóm của doanh nghiệp, `dom.storage.enabled=false`. Sản phẩm này còn
 * phải chạy ở chế độ **trình duyệt trong LAN** (v1.x), tức là trên đúng những
 * máy trạm hay bị cấu hình chặt như vậy.
 *
 * Vì sao nó nguy hiểm hơn vẻ ngoài: `ThemeProvider` và `SplitPane` đọc
 * `localStorage` **trong `useState` initializer**, tức là trong lúc render.
 * Một exception lúc render lan tới `createRoot`, và `ThemeProvider` lại là
 * provider ngoài cùng → **trắng màn hình cả ứng dụng**, không thông báo, không
 * lối ra. Người dùng chỉ thấy một trang trắng và không có cách nào đoán ra
 * nguyên nhân là một ô cấu hình trình duyệt.
 *
 * Mất thiết lập giao diện là phiền; không mở được phần mềm kế toán là hỏng
 * việc. Nên ở đây hỏng = **quay về mặc định**, không phải ném tiếp.
 */

/** Đọc một khóa. Trả `null` khi không có khóa HOẶC khi kho bị chặn. */
export function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** Ghi một khóa. Bị chặn thì bỏ qua — chỗ gọi không có việc gì để xử lý. */
export function writeStored(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // Cố ý im lặng: thiết lập không lưu được không ngăn người dùng làm việc.
  }
}

/** Xóa một khóa. Bị chặn thì bỏ qua. */
export function removeStored(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    // Cố ý im lặng — xem ghi chú ở `writeStored`.
  }
}
