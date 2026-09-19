/**
 * Tra bảng quyết định xử lý sai sót (`GET /einvoices/error-flows`) — hai hàm
 * thuần wizard U4 dùng, tách riêng để test không cần React.
 */

import type { Schemas } from '@api-types'

export type ErrorFlowOut = Schemas['ErrorFlowOut']

/** Dòng bảng quyết định khớp hai câu trả lời — `null` khi chưa trả lời đủ. */
export function matchFlow(
  flows: readonly ErrorFlowOut[],
  errorKind: number | null,
  buyerDeclared: boolean | null,
): ErrorFlowOut | null {
  if (errorKind === null) {
    return null
  }
  const candidates = flows.filter((flow) => flow.error_kind === errorKind)
  const asks = candidates.some((flow) => flow.buyer_declared !== null && flow.buyer_declared !== undefined)
  if (!asks) {
    return candidates[0] ?? null
  }
  if (buyerDeclared === null) {
    return null
  }
  return candidates.find((flow) => flow.buyer_declared === buyerDeclared) ?? null
}

/** Nhánh này có hỏi "khách đã kê khai chưa" không — đọc từ bảng, không viết cứng. */
export function asksBuyerDeclared(flows: readonly ErrorFlowOut[], errorKind: number | null): boolean {
  return flows.some(
    (flow) =>
      flow.error_kind === errorKind && flow.buyer_declared !== null && flow.buyer_declared !== undefined,
  )
}
