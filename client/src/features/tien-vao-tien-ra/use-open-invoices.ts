/**
 * Chứng từ công nợ còn nợ của một đối tác cho picker đối trừ (`docs/srs/03` §4).
 *
 * Hai router cùng hợp đồng (`/cash-book/open-invoices` và `/bank/open-invoices`)
 * nhưng cổng quyền khác nhau (theo loại phiếu sẽ lập) — form nào gọi đường của
 * module đó qua `basePath`. Router `/purchase/open-invoices` (lát 7B, picker
 * hóa đơn gốc cho chứng từ trả lại hàng) trả CÙNG hình dạng nhưng khóa cứng
 * chiều phải trả + loại NCC nên chỉ nhận `vendor_id/branch_id/as_of`; router
 * `/sales/open-invoices` (7C-2, hóa đơn gốc cho trả lại / giảm giá / điều chỉnh
 * giảm) đối xứng với `customer_id` — hook dựng query theo `basePath` thay vì
 * bắt chỗ gọi nhớ ba bộ tham số.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type OpenInvoice = Schemas['OpenInvoiceOut']
export type SettlementSide = 'receivable' | 'payable'

export interface OpenInvoicesQuery {
  readonly basePath: '/api/v1/cash-book' | '/api/v1/bank' | '/api/v1/purchase' | '/api/v1/sales'
  readonly side: SettlementSide
  readonly partnerKind: number
  readonly partnerId: number
  readonly branchId: number
  readonly asOf: string
}

function openInvoicesUrl(query: OpenInvoicesQuery): string {
  const tail = `branch_id=${String(query.branchId)}&as_of=${query.asOf}`
  if (query.basePath === '/api/v1/purchase') {
    return `${query.basePath}/open-invoices?vendor_id=${String(query.partnerId)}&${tail}`
  }
  if (query.basePath === '/api/v1/sales') {
    return `${query.basePath}/open-invoices?customer_id=${String(query.partnerId)}&${tail}`
  }
  return (
    `${query.basePath}/open-invoices?side=${query.side}&partner_kind=${String(query.partnerKind)}` +
    `&partner_id=${String(query.partnerId)}&${tail}`
  )
}

export function useOpenInvoices(query: OpenInvoicesQuery | null) {
  const { client, datasetCode } = useSession()

  const url = query === null ? '' : openInvoicesUrl(query)

  return useQuery({
    queryKey: ['open-invoices', datasetCode, url],
    enabled: datasetCode !== null && query !== null,
    queryFn: () => client.get<Schemas['OpenInvoicesResponse']>(url, { datasetCode }),
  })
}
