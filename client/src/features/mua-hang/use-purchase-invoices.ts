/**
 * Đọc/ghi hóa đơn mua hàng qua router module `/api/v1/purchase/*` (SRS 05).
 *
 * Màn hình mua hàng đọc MỘT module nên gọi thẳng router của nó, không BFF
 * (docs/design-guidelines.md §3); tab "việc còn thiếu" là ngoại lệ có chủ đích —
 * nó đọc ba nơi (`vouchers`, `einvoices`, dataset công nợ) nên đi qua BFF
 * `pending-issues` của lát 7G-4. Ghi sổ / bỏ ghi sổ / xóa dùng
 * `so-sach-thue/use-voucher-actions` — một đường ghi sổ cho mọi loại chứng từ.
 *
 * Mọi khóa truy vấn mang tiền tố `['purchase', dataset]` để một lượt ghi sổ ở
 * bất kỳ đâu làm mới cả lưới lẫn tab bằng một lệnh invalidate.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type PurchaseInvoiceOut = Schemas['PurchaseInvoiceOut']
export type PurchaseInvoiceIn = Schemas['PurchaseInvoiceIn']
export type PurchaseInvoiceUpdate = Schemas['PurchaseInvoiceUpdate']
export type PurchaseInvoiceListResponse = Schemas['PurchaseInvoiceListResponse']
export type PurchaseInvoiceListItem = Schemas['PurchaseInvoiceListItem']
export type TradePendingIssues = Schemas['TradePendingIssuesResponse']

export const PURCHASE_PAGE_SIZE = 50

/** Tham số lọc lưới — khóa nào không lọc thì BỎ HẲN (exactOptionalPropertyTypes). */
export interface PurchaseInvoiceListQuery {
  readonly page: number
  readonly status?: number
  readonly vendorInvoiceStatus?: number
  readonly overdue?: boolean
  readonly vendorId?: number
  readonly kind?: number
}

function listUrl(query: PurchaseInvoiceListQuery): string {
  const params = new URLSearchParams()
  params.set('page', String(query.page))
  params.set('page_size', String(PURCHASE_PAGE_SIZE))
  if (query.status !== undefined) {
    params.set('status', String(query.status))
  }
  if (query.vendorInvoiceStatus !== undefined) {
    params.set('vendor_invoice_status', String(query.vendorInvoiceStatus))
  }
  if (query.overdue === true) {
    params.set('overdue', 'true')
  }
  if (query.vendorId !== undefined) {
    params.set('vendor_id', String(query.vendorId))
  }
  if (query.kind !== undefined) {
    params.set('kind', String(query.kind))
  }
  return `/api/v1/purchase/invoices?${params.toString()}`
}

export function usePurchaseInvoiceList(query: PurchaseInvoiceListQuery) {
  const { client, datasetCode } = useSession()
  const url = listUrl(query)
  return useQuery({
    queryKey: ['purchase', datasetCode, 'invoices', url],
    enabled: datasetCode !== null,
    queryFn: () => client.get<PurchaseInvoiceListResponse>(url, { datasetCode }),
  })
}

export function usePurchasePendingIssues() {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['purchase', datasetCode, 'pending-issues'],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<TradePendingIssues>('/api/v1/purchase/pending-issues', { datasetCode }),
  })
}

export function usePurchaseInvoice(id: string | null) {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['purchase', datasetCode, 'invoice', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<PurchaseInvoiceOut>(`/api/v1/purchase/invoices/${String(id)}`, { datasetCode }),
  })
}

export function useCreatePurchaseInvoice() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
      acknowledgeWarnings,
    }: {
      readonly body: PurchaseInvoiceIn
      readonly idempotencyKey: string
      readonly acknowledgeWarnings?: boolean
    }) =>
      client.post<PurchaseInvoiceOut>(
        `/api/v1/purchase/invoices${acknowledgeWarnings === true ? '?acknowledge_warnings=true' : ''}`,
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['purchase', datasetCode] })
    },
  })
}

export function useUpdatePurchaseInvoice(id: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: PurchaseInvoiceUpdate) =>
      client.put<PurchaseInvoiceOut>(
        `/api/v1/purchase/invoices/${id}`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
    onSuccess: (voucher) => {
      void queryClient.invalidateQueries({ queryKey: ['purchase', datasetCode] })
      queryClient.setQueryData(['purchase', datasetCode, 'invoice', voucher.id], voucher)
    },
  })
}
