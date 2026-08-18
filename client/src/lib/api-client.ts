/**
 * Client REST duy nhất nói chuyện với app server.
 *
 * Ràng buộc kiến trúc (luật phụ thuộc #6, docs/system-architecture.md):
 *   - UI chỉ biết REST + type sinh từ OpenAPI của server. Không gọi API Tauri
 *     trong luồng nghiệp vụ — giữ đường lên chế độ trình duyệt LAN ở v1.x.
 *   - **Không có phép tính tiền ở đây.** Mọi cộng/trừ/làm tròn/quy đổi tỷ giá
 *     làm ở server bằng `Decimal`. Client chỉ hiển thị và định dạng.
 *
 * Bốn header mà lớp này gắn, mỗi cái là một hợp đồng đã chốt ở server:
 *
 * | Header | Khi nào | Quyết định |
 * | --- | --- | --- |
 * | `Authorization: Bearer` | có phiên | phiên lưu DB, thu hồi được ngay (2B-1a) |
 * | `X-Client-Version` | **mọi** request | thiếu → lệnh ghi trả `426` (H2) |
 * | `X-Dataset` | đã chọn dữ liệu kế toán | E1 — không có mặc định phía server |
 * | `X-Idempotency-Key` | do **chỗ gọi** truyền vào | RT-12 — xem `newIdempotencyKey` |
 *
 * `X-Client-Version` gắn cho cả lệnh đọc dù server chỉ kiểm ở lệnh ghi: gắn có
 * điều kiện nghĩa là có một nhánh mã nữa để hỏng, và log của server thì mất
 * thông tin "máy trạm nào đang chạy bản nào" đúng lúc cần nó nhất.
 */

import type { ProblemDetails, Schemas } from '@api-types'

import { APP_VERSION } from '@/lib/app-version'

export const CLIENT_VERSION_HEADER = 'X-Client-Version'
export const DATASET_HEADER = 'X-Dataset'
export const BRANCH_HEADER = 'X-Branch'
export const IDEMPOTENCY_HEADER = 'X-Idempotency-Key'

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

  /** Máy chủ từ chối vì bản client quá cũ (bước 19) — màn hình cập nhật, không phải lỗi nhập liệu. */
  get isClientTooOld(): boolean {
    return this.status === 426
  }

  /**
   * Phiên **không dùng được nữa**: token thiếu, hết hạn, hoặc đã bị thu hồi.
   *
   * Nhận theo **mã lỗi**, không theo `401`. Server dùng `401` cho hai chuyện
   * khác hẳn nhau: "phiên của bạn không còn giá trị" và "thứ bạn vừa gõ sai"
   * (`auth.invalid_credentials` khi nhập lại mật khẩu, `auth.totp_code_invalid`
   * khi xác nhận thiết bị). Ở nhóm sau, phiên **vẫn sống**.
   *
   * Gộp cả hai lại là lỗi đã đo được qua giao diện: gõ nhầm một chữ ở ô "mật
   * khẩu hiện tại" thì người dùng bị đá về màn hình đăng nhập, **không một dòng
   * giải thích** — và nếu đang đăng ký 2FA thì bí mật vừa sinh cũng mất, lần
   * sau phải quét lại mã QR.
   */
  get isSessionLost(): boolean {
    return this.errorCode === SESSION_LOST_CODE
  }
}

const SESSION_LOST_CODE = 'auth.not_authenticated'

/** Trạng thái máy chủ — kiểu lấy từ đặc tả, không khai lại bằng tay. */
export type HealthStatus = Schemas['HealthResponse']
export type Handshake = Schemas['HandshakeResponse']

/**
 * Khóa chống trùng cho một **thao tác nghiệp vụ** (RT-12, FR-NFR-004).
 *
 * Sinh ở chỗ gọi chứ không sinh tự động trong `post()`, và đây là điểm dễ làm
 * sai nhất của cả hợp đồng ghi: khóa phải **giống nhau** giữa lần gửi đầu và
 * lần gửi lại của cùng một thao tác. Sinh mới mỗi lần gọi thì server thấy hai
 * thao tác khác nhau — tức là đúng cái mà cơ chế này sinh ra để chặn (một
 * chứng từ bị ghi hai lần vì người dùng bấm Lưu hai lần).
 *
 * Cách dùng: màn hình sinh khóa **một lần** khi mở form, rồi truyền cùng khóa
 * đó cho mọi lần thử lưu.
 */
export function newIdempotencyKey(): string {
  return crypto.randomUUID()
}

export interface RequestOptions {
  /** Dữ liệu kế toán của request. Thiếu → server trả `dataset.header_missing`. */
  readonly datasetCode?: string | null
  /** Chi nhánh đang thao tác, khi tài khoản được gán nhiều chi nhánh. */
  readonly branchId?: number | null
  /** Khóa chống trùng — xem `newIdempotencyKey`. */
  readonly idempotencyKey?: string
  readonly signal?: AbortSignal
}

export interface ApiClientOptions {
  /** Gốc URL app server, ví dụ `https://host:5443`. */
  readonly baseUrl: string
  /**
   * Gọi khi phiên **không dùng được nữa** (`auth.not_authenticated`), để lớp
   * phiên quay về màn hình đăng nhập. Không gọi cho `401` "nhập sai" — xem
   * `ApiError.isSessionLost`.
   */
  readonly onSessionLost?: () => void
  /**
   * Gọi khi server trả `426`, tức bản client này không ghi được nữa.
   *
   * Tồn tại vì `min_client_version` đổi được **trong lúc** máy trạm đang mở:
   * người quản trị nâng cấp máy chủ giữa ngày làm việc thì lượt bắt tay lúc
   * khởi động không còn đúng, và người dùng chỉ biết khi bấm Lưu. Lớp phiên
   * bật cờ chỉ-đọc ngay từ lần từ chối đầu tiên.
   */
  readonly onClientTooOld?: () => void
}

type Body = Readonly<Record<string, unknown>>

export class ApiClient {
  private readonly baseUrl: string
  private readonly onSessionLost: () => void
  private readonly onClientTooOld: () => void
  private token: string | null = null

  constructor(options: ApiClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, '')
    this.onSessionLost = options.onSessionLost ?? (() => undefined)
    this.onClientTooOld = options.onClientTooOld ?? (() => undefined)
  }

  /**
   * Đặt (hay xóa) token của phiên hiện tại.
   *
   * Token nằm **trong** client chứ không phải một hàm đọc từ ngoài: nó đổi ở
   * đúng ba thời điểm (đăng nhập, khôi phục phiên, đăng xuất/`401`), cả ba đều
   * là sự kiện — không phải trạng thái mà giao diện vẽ theo. Để nó thành state
   * của React sẽ khiến mỗi lần đổi token vẽ lại cả cây, và để nó thành `ref`
   * đọc lúc render là đúng thứ mà `react-hooks/refs` cấm.
   */
  setToken(token: string | null): void {
    this.token = token
  }

  async get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>('GET', path, undefined, options)
  }

  async post<T>(path: string, body?: Body, options?: RequestOptions): Promise<T> {
    return this.request<T>('POST', path, body, options)
  }

  async put<T>(path: string, body: Body, options?: RequestOptions): Promise<T> {
    return this.request<T>('PUT', path, body, options)
  }

  async delete<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>('DELETE', path, undefined, options)
  }

  /**
   * POST một tệp kèm trường phụ (multipart) — đường nhập Excel danh mục.
   *
   * Tách khỏi `post` thay vì cho `post` đoán theo kiểu body: `Content-Type`
   * của multipart phải do trình duyệt tự đặt (nó sinh `boundary`), nên nhánh
   * "đừng đặt Content-Type" là một hợp đồng khác hẳn, đáng có tên riêng.
   */
  async postForm<T>(path: string, form: FormData, options?: RequestOptions): Promise<T> {
    return this.request<T>('POST', path, form, options)
  }

  /**
   * GET một tệp nhị phân (tệp mẫu Excel, tệp xuất danh mục).
   *
   * Không đi qua `request` vì phản hồi không phải JSON; nhưng lỗi thì **vẫn
   * là** RFC 7807 — server từ chối trước khi sinh tệp — nên nhánh lỗi dùng
   * chung `toError` để chỗ gọi chỉ phải bắt một loại `ApiError`.
   */
  async getBlob(
    path: string,
    options?: RequestOptions,
  ): Promise<{ readonly blob: Blob; readonly fileName: string | null }> {
    const response = await this.send('GET', path, undefined, options)
    if (!response.ok) {
      const error = await this.toError(response, path)
      if (error.isSessionLost) {
        this.token = null
        this.onSessionLost()
      }
      if (error.isClientTooOld) {
        // Server hiện chỉ kiểm phiên bản ở lệnh ghi, nhưng đường tệp không
        // được phép lệch hành vi với đường JSON — nếu ngày nào lệnh đọc cũng
        // bị kiểm, cờ chỉ-đọc vẫn bật đúng.
        this.onClientTooOld()
      }
      throw error
    }
    return {
      blob: await response.blob(),
      fileName: fileNameFromDisposition(response.headers.get('Content-Disposition')),
    }
  }

  /** `/health` — dùng cho màn hình chẩn đoán kết nối tới máy chủ. */
  async health(): Promise<HealthStatus> {
    return this.get<HealthStatus>('/health')
  }

  /**
   * `/api/v1/system/handshake` — gọi **trước** khi đăng nhập (LD-05).
   *
   * Endpoint ẩn danh, nên nó cũng là phép thử kết nối đầu tiên: không tới được
   * máy chủ thì người dùng cần biết ngay ở màn hình đầu, chứ không phải sau khi
   * gõ xong mật khẩu.
   */
  async handshake(signal?: AbortSignal): Promise<Handshake> {
    return this.get<Handshake>(
      '/api/v1/system/handshake',
      signal === undefined ? undefined : { signal },
    )
  }

  /** Gửi request thô — dùng chung cho đường JSON (`request`) và đường tệp (`getBlob`). */
  private async send(
    method: string,
    path: string,
    body: Body | FormData | undefined,
    options?: RequestOptions,
  ): Promise<Response> {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      [CLIENT_VERSION_HEADER]: APP_VERSION,
    }
    if (this.token !== null) {
      headers.Authorization = `Bearer ${this.token}`
    }
    if (options?.datasetCode) {
      headers[DATASET_HEADER] = options.datasetCode
    }
    if (typeof options?.branchId === 'number') {
      headers[BRANCH_HEADER] = String(options.branchId)
    }
    if (options?.idempotencyKey !== undefined) {
      headers[IDEMPOTENCY_HEADER] = options.idempotencyKey
    }
    // FormData KHÔNG đặt Content-Type: trình duyệt tự đặt kèm `boundary`,
    // và một Content-Type đặt tay ở đây làm server đọc được 0 phần tử.
    if (body !== undefined && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json'
    }

    try {
      return await fetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        ...(body === undefined
          ? {}
          : { body: body instanceof FormData ? body : JSON.stringify(body) }),
        ...(options?.signal ? { signal: options.signal } : {}),
      })
    } catch (cause) {
      // Máy chủ tắt, dây mạng rút, tên miền LAN sai — cùng một tình huống với
      // người dùng ("không tới được máy chủ"), nên cùng một loại lỗi. Không có
      // thân RFC 7807 ở đây vì chưa có phản hồi nào cả.
      throw new ApiError({
        type: 'https://konek.vn/errors/transport.unreachable',
        title: 'transport.unreachable',
        status: 0,
        detail: cause instanceof Error ? cause.message : String(cause),
        error_code: 'transport.unreachable',
      })
    }
  }

  private async request<T>(
    method: string,
    path: string,
    body: Body | FormData | undefined,
    options?: RequestOptions,
  ): Promise<T> {
    const response = await this.send(method, path, body, options)

    if (!response.ok) {
      const error = await this.toError(response, path)
      if (error.isSessionLost) {
        // Quên token ở **đây**, không để chỗ gọi nhớ hộ: một truy vấn còn treo
        // sau khi màn hình đã quay về đăng nhập vẫn gửi token cũ, và trên máy
        // trạm dùng chung thì token đó thường vẫn còn hiệu lực phía server.
        this.token = null
        this.onSessionLost()
      }
      if (error.isClientTooOld) {
        this.onClientTooOld()
      }
      throw error
    }

    if (response.status === 204) {
      return undefined as T
    }
    return (await response.json()) as T
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

/**
 * Tên tệp server gợi ý trong `Content-Disposition`, nếu có.
 *
 * Ưu tiên dạng `filename*=UTF-8''…` (RFC 5987) vì tên tệp danh mục có dấu
 * tiếng Việt; rơi về `filename="…"` khi không có.
 */
export function fileNameFromDisposition(header: string | null): string | null {
  if (header === null) {
    return null
  }
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (star?.[1] !== undefined) {
    try {
      return decodeURIComponent(star[1])
    } catch {
      // Chuỗi mã hóa hỏng — rơi xuống dạng thường bên dưới.
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header)
  return plain?.[1] ?? null
}

/**
 * Địa chỉ app server.
 *
 * Mặc định là **chính origin đang phục vụ trang** — đó là hình dạng của chế độ
 * trình duyệt trong LAN (app server tự phục vụ bundle web tại `/app`) và cũng
 * là hình dạng của chế độ một máy. Bản Tauri trỏ tới máy host bằng biến môi
 * trường lúc dựng.
 */
export function resolveServerUrl(): string {
  const configured: unknown = import.meta.env.VITE_KET_SERVER_URL
  if (typeof configured === 'string' && configured.length > 0) {
    return configured
  }
  return window.location.origin
}
