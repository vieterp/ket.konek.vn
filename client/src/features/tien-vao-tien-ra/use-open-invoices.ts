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
 * bắt chỗ gọi nhớ ba bộ tham số. Router `/gl/journal-vouchers/open-invoices`
 * (7H-2b) hỏi cho MỘT DÒNG định khoản: chiều không gửi lên mà server suy từ
 * BÊN của dòng (`on_debit`) + TK (`account_id`) qua chính luật 7C-4, nên đường
 * này mang hai tham số riêng và bỏ `side`.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type OpenInvoice = Schemas['OpenInvoiceOut']
export type SettlementSide = 'receivable' | 'payable'

export interface OpenInvoicesQuery {
  readonly basePath:
    | '/api/v1/cash-book'
    | '/api/v1/bank'
    | '/api/v1/purchase'
    | '/api/v1/sales'
    | '/api/v1/gl/journal-vouchers'
  readonly side: SettlementSide
  readonly partnerKind: number
  readonly partnerId: number
  readonly branchId: number
  readonly asOf: string
  /** Chỉ đường GLE: TK của dòng, bên đang ghi (Nợ/Có) và tiền tệ của dòng. */
  readonly accountId?: number
  readonly onDebit?: boolean
  readonly currencyCode?: string
}

function openInvoicesUrl(query: OpenInvoicesQuery): string {
  const tail = `branch_id=${String(query.branchId)}&as_of=${query.asOf}`
  if (query.basePath === '/api/v1/purchase') {
    return `${query.basePath}/open-invoices?vendor_id=${String(query.partnerId)}&${tail}`
  }
  if (query.basePath === '/api/v1/sales') {
    return `${query.basePath}/open-invoices?customer_id=${String(query.partnerId)}&${tail}`
  }
  if (query.basePath === '/api/v1/gl/journal-vouchers') {
    return (
      `${query.basePath}/open-invoices?partner_kind=${String(query.partnerKind)}` +
      `&partner_id=${String(query.partnerId)}&account_id=${String(query.accountId ?? 0)}` +
      `&on_debit=${query.onDebit === true ? 'true' : 'false'}` +
      `&currency_code=${encodeURIComponent(query.currencyCode ?? 'VND')}&${tail}`
    )
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
