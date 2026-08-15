/**
 * Client REST duy nhất nói chuyện với app server.
 *
 * Ràng buộc kiến trúc (luật phụ thuộc #6, docs/system-architecture.md):
 *   - UI chỉ biết REST + type sinh từ OpenAPI của server. Không gọi API Tauri
 *     trong luồng nghiệp vụ — giữ đường lên chế độ trình duyệt LAN ở v1.x.
 *   - **Không có phép tính tiền ở đây.** Mọi cộng/trừ/làm tròn/quy đổi tỷ giá
 *     làm ở server bằng `Decimal`. Client chỉ hiển thị và định dạng.
 *
 * Phase 1 chỉ khai hình dạng; auth, bắt tay schema-version (LD-05), retry,
 * idempotency key làm ở phase 2.
 */

export interface ApiClientOptions {
  /** Gốc URL app server, ví dụ `https://host:5443`. */
  readonly baseUrl: string
}

export class ApiClient {
  private readonly baseUrl: string

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '')
  }

  /** GET một tài nguyên JSON. Kiểu trả về do caller khai từ `@api-types`. */
  async get<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      method: 'GET',
      headers: { Accept: 'application/json', ...init?.headers },
    })

    if (!response.ok) {
      // Phase 2: đổi thành lỗi nghiệp vụ có `error_code` (RFC 7807).
      throw new Error(`Yêu cầu thất bại: ${String(response.status)} ${path}`)
    }

    return (await response.json()) as T
  }
}
