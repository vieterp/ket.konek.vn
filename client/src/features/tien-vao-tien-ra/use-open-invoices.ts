/**
 * Chứng từ công nợ còn nợ của một đối tác cho picker đối trừ (`docs/srs/03` §4).
 *
 * Hai router cùng hợp đồng (`/cash-book/open-invoices` và `/bank/open-invoices`)
 * nhưng cổng quyền khác nhau (theo loại phiếu sẽ lập) — form nào gọi đường của
 * module đó qua `basePath`.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type OpenInvoice = Schemas['OpenInvoiceOut']
export type SettlementSide = 'receivable' | 'payable'

export interface OpenInvoicesQuery {
  readonly basePath: '/api/v1/cash-book' | '/api/v1/bank'
  readonly side: SettlementSide
  readonly partnerKind: number
  readonly partnerId: number
  readonly branchId: number
  readonly asOf: string
}

export function useOpenInvoices(query: OpenInvoicesQuery | null) {
  const { client, datasetCode } = useSession()

  const url =
    query === null
      ? ''
      : `${query.basePath}/open-invoices?side=${query.side}&partner_kind=${String(query.partnerKind)}` +
        `&partner_id=${String(query.partnerId)}&branch_id=${String(query.branchId)}&as_of=${query.asOf}`

  return useQuery({
    queryKey: ['open-invoices', datasetCode, url],
    enabled: datasetCode !== null && query !== null,
    queryFn: () => client.get<Schemas['OpenInvoicesResponse']>(url, { datasetCode }),
  })
}
