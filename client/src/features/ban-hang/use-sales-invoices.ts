/**
 * Đọc/ghi hóa đơn bán hàng qua router module `/api/v1/sales/*` (SRS 06).
 *
 * Màn hình bán hàng đọc MỘT module nên gọi thẳng router của nó, không BFF
 * (docs/design-guidelines.md §3); tab "việc còn thiếu" là ngoại lệ có chủ đích —
 * nó đọc ba nơi (`vouchers`, `einvoices`, dataset công nợ) nên đi qua BFF
 * `pending-issues` của lát 7G-4. Ghi sổ / bỏ ghi sổ / xóa dùng
 * `so-sach-thue/use-voucher-actions` — một đường ghi sổ cho mọi loại chứng từ.
 *
 * Mọi khóa truy vấn mang tiền tố `['sales', dataset]` để một lượt ghi sổ ở bất
 * kỳ đâu làm mới cả lưới lẫn tab bằng một lệnh invalidate.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type SalesInvoiceOut = Schemas['SalesInvoiceOut']
export type SalesInvoiceIn = Schemas['SalesInvoiceIn']
export type SalesInvoiceUpdate = Schemas['SalesInvoiceUpdate']
export type SalesInvoiceListResponse = Schemas['SalesInvoiceListResponse']
export type SalesInvoiceListItem = Schemas['SalesInvoiceListItem']
export type TradePendingIssues = Schemas['TradePendingIssuesResponse']

export const SALES_PAGE_SIZE = 50

/** Tham số lọc lưới — khóa nào không lọc thì BỎ HẲN (exactOptionalPropertyTypes). */
export interface SalesInvoiceListQuery {
  readonly page: number
  readonly status?: number
  /** `missing` = đã ghi sổ, loại cần hóa đơn, không tờ HĐĐT còn sống — đúng nhóm BFF. */
  readonly einvoice?: 'missing'
  readonly overdue?: boolean
  readonly customerId?: number
  readonly kind?: number
  /** Chứng từ điều chỉnh (kind 5/6) CỦA một chứng từ gốc — wizard sai sót HĐĐT. */
  readonly adjustsVoucherId?: string
}

function listUrl(query: SalesInvoiceListQuery): string {
  const params = new URLSearchParams()
  params.set('page', String(query.page))
  params.set('page_size', String(SALES_PAGE_SIZE))
  if (query.status !== undefined) {
    params.set('status', String(query.status))
  }
  if (query.einvoice !== undefined) {
    params.set('einvoice', query.einvoice)
  }
  if (query.overdue === true) {
    params.set('overdue', 'true')
  }
  if (query.customerId !== undefined) {
    params.set('customer_id', String(query.customerId))
  }
  if (query.kind !== undefined) {
    params.set('kind', String(query.kind))
  }
  if (query.adjustsVoucherId !== undefined) {
    params.set('adjusts_voucher_id', query.adjustsVoucherId)
  }
  return `/api/v1/sales/invoices?${params.toString()}`
}

export function useSalesInvoiceList(query: SalesInvoiceListQuery) {
  const { client, datasetCode } = useSession()
  const url = listUrl(query)
  return useQuery({
    queryKey: ['sales', datasetCode, 'invoices', url],
    enabled: datasetCode !== null,
    queryFn: () => client.get<SalesInvoiceListResponse>(url, { datasetCode }),
  })
}

export function useSalesPendingIssues() {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['sales', datasetCode, 'pending-issues'],
    enabled: datasetCode !== null,
    queryFn: () => client.get<TradePendingIssues>('/api/v1/sales/pending-issues', { datasetCode }),
  })
}

export function useSalesInvoice(id: string | null) {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['sales', datasetCode, 'invoice', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<SalesInvoiceOut>(`/api/v1/sales/invoices/${String(id)}`, { datasetCode }),
  })
}

export function useCreateSalesInvoice() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
      acknowledgeWarnings,
    }: {
      readonly body: SalesInvoiceIn
      readonly idempotencyKey: string
      readonly acknowledgeWarnings?: boolean
    }) =>
      client.post<SalesInvoiceOut>(
        `/api/v1/sales/invoices${acknowledgeWarnings === true ? '?acknowledge_warnings=true' : ''}`,
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['sales', datasetCode] })
    },
  })
}

export function useUpdateSalesInvoice(id: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SalesInvoiceUpdate) =>
      client.put<SalesInvoiceOut>(
        `/api/v1/sales/invoices/${id}`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
    onSuccess: (voucher) => {
      void queryClient.invalidateQueries({ queryKey: ['sales', datasetCode] })
      queryClient.setQueryData(['sales', datasetCode, 'invoice', voucher.id], voucher)
    },
  })
}
