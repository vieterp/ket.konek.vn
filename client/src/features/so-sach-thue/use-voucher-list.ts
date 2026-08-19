/**
 * Danh sách chứng từ (`GET /api/v1/vouchers`) cho màn hình U1 — lọc theo loại
 * + trạng thái khi người dùng đứng ở một tab "việc còn thiếu", đọc mọi loại/
 * trạng thái ở tab "Tất cả".
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type VoucherListResponse = Schemas['VoucherListResponse']

/** `page` 1-based, khớp hợp đồng `list_vouchers` phía server (`Query(ge=1)`). */
export const VOUCHER_PAGE_SIZE = 50

export interface VoucherListParams {
  readonly documentType?: string
  readonly status?: number
  readonly page: number
}

function listPath(params: VoucherListParams): string {
  const query = new URLSearchParams()
  if (params.documentType !== undefined) {
    query.set('type', params.documentType)
  }
  if (params.status !== undefined) {
    query.set('status', String(params.status))
  }
  query.set('page', String(params.page))
  query.set('page_size', String(VOUCHER_PAGE_SIZE))
  return `/api/v1/vouchers?${query.toString()}`
}

export function useVoucherList(params: VoucherListParams) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['vouchers', datasetCode, 'list', params],
    enabled: datasetCode !== null,
    queryFn: () => client.get<VoucherListResponse>(listPath(params), { datasetCode }),
  })
}
