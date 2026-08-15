/**
 * Client REST duy nhất nói chuyện với app server.
 *
 * Ràng buộc kiến trúc (luật phụ thuộc #6, docs/system-architecture.md):
 *   - UI chỉ biết REST + type sinh từ OpenAPI của server. Không gọi API Tauri
 *     trong luồng nghiệp vụ — giữ đường lên chế độ trình duyệt LAN ở v1.x.
 *   - **Không có phép tính tiền ở đây.** Mọi cộng/trừ/làm tròn/quy đổi tỷ giá
 *     làm ở server bằng `Decimal`. Client chỉ hiển thị và định dạng.
 *
 * Lát 2B-2b nối type sinh từ OpenAPI vào đây: hình dạng phản hồi và thân lỗi
 * lấy thẳng từ `@api-types`, nên đổi model ở server mà quên sinh lại type sẽ
 * làm `tsc` đỏ chứ không lộ ra lúc chạy.
 *
 * Phần còn lại của lát client (2C): auth, bắt tay schema-version (LD-05),
 * retry, khóa idempotency cho lệnh ghi.
 */

import type { ProblemDetails, Schemas } from '@api-types'

export interface ApiClientOptions {
  /** Gốc URL app server, ví dụ `https://host:5443`. */
  readonly baseUrl: string
}

/**
 * Lỗi mang **mã ổn định** của server (FR-NFR-050).
 *
 * Client dựng câu hiển thị từ `errorCode`, không in `detail` ra màn hình:
 * `detail` là câu dành cho người vận hành và cho log, còn thông điệp cho người
 * dùng thuộc về i18n phía client.
 */
export class ApiError extends Error {
  readonly status: number
  readonly errorCode: string
  readonly problem: ProblemDetails

  constructor(problem: ProblemDetails) {
    super(problem.detail)
    this.name = 'ApiError'
    this.status = problem.status
    this.errorCode = problem.error_code
    this.problem = problem
  }
}

/** Trạng thái máy chủ — kiểu lấy từ đặc tả, không khai lại bằng tay. */
export type HealthStatus = Schemas['HealthResponse']

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
      throw await this.toError(response, path)
    }

    return (await response.json()) as T
  }

  /** `/health` — dùng cho màn hình chẩn đoán kết nối tới máy chủ. */
  async health(): Promise<HealthStatus> {
    return this.get<HealthStatus>('/health')
  }

  /**
   * Dựng lỗi từ phản hồi.
   *
   * Máy chủ trả RFC 7807 cho **mọi** lỗi nghiệp vụ, nhưng một proxy ngược hoặc
   * một máy chủ đang khởi động lại thì không — nên nhánh dự phòng phải có, và
   * nó vẫn cho ra cùng một loại lỗi để chỗ gọi chỉ phải bắt một thứ.
   */
  private async toError(response: Response, path: string): Promise<ApiError> {
    try {
      const problem = (await response.json()) as ProblemDetails
      if (typeof problem.error_code === 'string') {
        return new ApiError(problem)
      }
    } catch {
      // Thân không phải JSON — rơi xuống nhánh dự phòng bên dưới.
    }
    return new ApiError({
      type: 'https://konek.vn/errors/transport.unexpected_response',
      title: 'transport.unexpected_response',
      status: response.status,
      detail: `Máy chủ trả phản hồi không đọc được: ${String(response.status)} ${path}`,
      error_code: 'transport.unexpected_response',
    })
  }
}
