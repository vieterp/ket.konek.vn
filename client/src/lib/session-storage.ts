/**
 * Giữ phiên đăng nhập qua các lần tải lại trang (quyết định H5).
 *
 * `localStorage` khóa theo **địa chỉ máy chủ**, không phải một khóa cố định:
 * một máy trạm mở song song bản cài thật và bản demo là chuyện có thật ở giai
 * đoạn triển khai, và hai bản đó không được giẫm phiên của nhau — triệu chứng
 * sẽ là "tự nhiên bị đá ra" mà không ai lần được nguyên nhân.
 *
 * Vì sao không `sessionStorage`: đây là ứng dụng dùng cả ngày, và đóng nhầm thẻ
 * rồi phải đăng nhập lại (kèm mã 2FA) là ma sát vô nghĩa. Hạn phiên do **server**
 * quyết (`session_ttl_minutes`, mặc định 12 giờ) và thu hồi được ngay từ phía
 * server — thứ nằm ở đây chỉ là bản sao của token, không phải nguồn quyền.
 *
 * Kho bí mật của Tauri là việc của lát 2C-4. Không thay được lớp này: ở chế độ
 * trình duyệt trong LAN (v1.x) thì `localStorage` vẫn là đường duy nhất, nên nó
 * phải chạy đúng trước.
 */

const PREFIX = 'ket.session'

interface StoredSession {
  readonly token: string
  readonly expiresAt: string
  /** Dữ liệu kế toán đang mở, để lần vào sau không phải chọn lại. */
  readonly datasetCode: string | null
}

function keyFor(baseUrl: string): string {
  return `${PREFIX}:${baseUrl}`
}

export function readStoredSession(baseUrl: string): StoredSession | null {
  const raw = localStorage.getItem(keyFor(baseUrl))
  if (raw === null) {
    return null
  }
  try {
    const parsed = JSON.parse(raw) as Partial<StoredSession>
    if (typeof parsed.token !== 'string' || typeof parsed.expiresAt !== 'string') {
      return null
    }
    return {
      token: parsed.token,
      expiresAt: parsed.expiresAt,
      datasetCode: typeof parsed.datasetCode === 'string' ? parsed.datasetCode : null,
    }
  } catch {
    // Giá trị hỏng (người dùng nghịch devtools, bản cũ ghi khuôn khác) không
    // được làm ứng dụng trắng màn hình — coi như chưa đăng nhập.
    return null
  }
}

export function writeStoredSession(baseUrl: string, session: StoredSession): void {
  localStorage.setItem(keyFor(baseUrl), JSON.stringify(session))
}

export function clearStoredSession(baseUrl: string): void {
  localStorage.removeItem(keyFor(baseUrl))
}

export type { StoredSession }
