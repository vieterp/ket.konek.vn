/**
 * Rút danh sách vi phạm (`violations`) khỏi thân lỗi 422 của chứng từ.
 *
 * `posting.invalid` mang **toàn bộ** vi phạm validator ghi sổ tìm thấy trong
 * `problem_extra()` (server, `PostingValidationError`) — không nằm trong
 * `details` (chỉ nhận vô hướng) mà là một trường mở rộng ngang hàng, nên
 * `ProblemDetails` sinh từ OpenAPI chỉ khai nó qua chỉ số `[key: string]:
 * unknown`. Đọc thủ công ở đây, một chỗ duy nhất.
 */

import type { ProblemDetails } from '@api-types'

export interface Violation {
  readonly code: string
  readonly message: string
  /** 1-based — khớp số dòng hiện trên lưới. */
  readonly line_no?: number
}

function isViolation(value: unknown): value is Violation {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as Record<string, unknown>).message === 'string'
  )
}

export function extractViolations(problem: ProblemDetails): readonly Violation[] {
  const raw = (problem as unknown as Record<string, unknown>).violations
  if (!Array.isArray(raw)) {
    return []
  }
  return raw.filter(isViolation)
}
