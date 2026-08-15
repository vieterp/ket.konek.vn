/**
 * Type sinh từ OpenAPI của app server — KHÔNG viết tay vào thư mục này.
 *
 * `schema.d.ts` do `openapi-typescript` sinh từ chính đặc tả của server
 * (`make api-types` ở gốc repo) và **được commit**: CI sinh lại rồi so
 * `git diff --exit-code`, nên đổi response model ở server mà quên sinh lại thì
 * cổng đỏ ngay — và người review nhìn thấy hợp đồng đổi gì trong chính diff đó.
 *
 * Hợp đồng client↔server là REST + OpenAPI (LD-03): UI **không** gọi API Tauri
 * cho luồng nghiệp vụ, để đường lên chế độ trình duyệt LAN ở v1.x còn mở.
 *
 * Cách dùng:
 *
 * ```ts
 * import type { Schemas, ApiPaths } from '@api-types'
 * type Job = Schemas['JobResponse']
 * ```
 */

export type { components, operations, paths } from './schema'

import type { components, paths } from './schema'

/** Mọi model thân request/response, tra theo tên lớp Pydantic của server. */
export type Schemas = components['schemas']

/** Bảng đường dẫn — dùng khi cần suy kiểu tham số/phản hồi của một endpoint. */
export type ApiPaths = paths

/**
 * Thân lỗi RFC 7807 mà mọi endpoint trả về khi có lỗi nghiệp vụ (FR-NFR-050).
 *
 * Khai riêng vì client bắt nó ở **một** chỗ (`api-client.ts`) chứ không theo
 * từng lời gọi: server trả `error_code` ổn định, client dựng câu tiếng Việt/Anh
 * từ mã đó.
 */
export type ProblemDetails = Schemas['ProblemDetails']
