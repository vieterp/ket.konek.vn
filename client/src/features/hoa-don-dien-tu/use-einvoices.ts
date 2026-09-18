/**
 * Hook đọc/ghi hóa đơn điện tử (`/api/v1/einvoices/*`, lát 7H-3) — lưới U3,
 * hộp phát hành, xem trước, wizard sai sót, panel outbox.
 *
 * Bề mặt ghi là bảng chuyển trạng thái (7D: không có `PUT`): lập nháp →
 * phát hành → … mỗi cạnh một endpoint. Hai cạnh `confirm`/`reject` KHÔNG lên
 * UI — đó là câu trả lời của nhà cung cấp mà worker ghi vào, không phải việc
 * người dùng bấm. Khóa idempotency cho `create`/`issue` sinh ở chỗ gọi lúc mở
 * hộp (cùng luật 6F): bấm hai lần là một lượt.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type EInvoiceOut = Schemas['EInvoiceOut']
export type EInvoiceListItem = Schemas['EInvoiceListItem']
export type EInvoiceListOut = Schemas['EInvoiceListOut']
export type OutboxListOut = Schemas['OutboxListOut']
export type OutboxRowOut = Schemas['OutboxRowOut']
export type ErrorFlowOut = Schemas['ErrorFlowOut']
export type ResolveErrorIn = Schemas['ResolveErrorIn']
export type ResolveErrorOut = Schemas['ResolveErrorOut']
export type ErrorNoticeIn = Schemas['ErrorNoticeIn']
export type ErrorNoticeOut = Schemas['ErrorNoticeOut']

export const EINVOICE_PAGE_SIZE = 50

/** Bốn tab của lưới — định nghĩa ở server (`EInvoiceListTab`), client chỉ nhắc lại tên. */
export type EInvoiceListTab =
  | 'cho-phat-hanh'
  | 'can-xu-ly'
  | 'da-thay-the-dieu-chinh'
  | 'khach-chua-nhan'

export interface EInvoiceListQuery {
  readonly page: number
  readonly tab?: EInvoiceListTab
  readonly q?: string
  readonly sourceVoucherId?: string
}

function listUrl(query: EInvoiceListQuery): string {
  const params = new URLSearchParams()
  params.set('page', String(query.page))
  params.set('page_size', String(EINVOICE_PAGE_SIZE))
  if (query.tab !== undefined) {
    params.set('tab', query.tab)
  }
  if (query.q !== undefined && query.q.trim() !== '') {
    params.set('q', query.q.trim())
  }
  if (query.sourceVoucherId !== undefined) {
    params.set('source_voucher_id', query.sourceVoucherId)
  }
  return `/api/v1/einvoices?${params.toString()}`
}

export function useEInvoiceList(query: EInvoiceListQuery, enabled = true) {
  const { client, datasetCode } = useSession()
  const url = listUrl(query)
  return useQuery({
    queryKey: ['einvoices', datasetCode, 'list', url],
    enabled: datasetCode !== null && enabled,
    queryFn: () => client.get<EInvoiceListOut>(url, { datasetCode }),
  })
}

export function useEInvoice(id: string | null) {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['einvoices', datasetCode, 'one', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () => client.get<EInvoiceOut>(`/api/v1/einvoices/${String(id)}`, { datasetCode }),
  })
}

/**
 * Tờ gốc dưới dạng HÀNG LƯỚI (có khách, tổng, ký hiệu chữ) — `GET /{id}` chỉ
 * trả tờ trần, nên wizard đọc đúng một hàng của lưới lọc theo chứng từ gốc.
 */
export function useEInvoiceRow(id: string | null) {
  const one = useEInvoice(id)
  const sourceVoucherId = one.data?.source_voucher_id
  const rows = useEInvoiceList(
    { page: 1, sourceVoucherId: sourceVoucherId ?? '' },
    sourceVoucherId !== undefined,
  )
  const row = rows.data?.items.find((item) => item.id === id) ?? null
  return {
    row,
    isPending: one.isPending || (sourceVoucherId !== undefined && rows.isPending),
    error: one.error ?? rows.error,
  }
}

export function useOutbox() {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['einvoices', datasetCode, 'outbox'],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<OutboxListOut>(`/api/v1/einvoices/outbox?page_size=${String(EINVOICE_PAGE_SIZE)}`, {
        datasetCode,
      }),
  })
}

export function useErrorFlows() {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['einvoices', datasetCode, 'error-flows'],
    enabled: datasetCode !== null,
    queryFn: () => client.get<ErrorFlowOut[]>('/api/v1/einvoices/error-flows', { datasetCode }),
  })
}

export function useEInvoiceActions() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: ['einvoices', datasetCode] })
    // Lưới bán đọc cột hóa đơn từ tờ còn sống — lập/phát hành ở đây đổi nó.
    void queryClient.invalidateQueries({ queryKey: ['sales', datasetCode] })
  }

  const create = useMutation({
    mutationFn: ({
      sourceVoucherId,
      invoiceFormId,
      idempotencyKey,
    }: {
      readonly sourceVoucherId: string
      readonly invoiceFormId: number
      readonly idempotencyKey: string
    }) =>
      client.post<EInvoiceOut>(
        '/api/v1/einvoices',
        { source_voucher_id: sourceVoucherId, invoice_form_id: invoiceFormId },
        { datasetCode, idempotencyKey },
      ),
    onSuccess: invalidate,
  })

  const issue = useMutation({
    mutationFn: ({
      id,
      invoiceDate,
      idempotencyKey,
    }: {
      readonly id: string
      readonly invoiceDate: string
      readonly idempotencyKey: string
    }) =>
      client.post<EInvoiceOut>(
        `/api/v1/einvoices/${id}/actions/issue`,
        { invoice_date: invoiceDate },
        { datasetCode, idempotencyKey },
      ),
    onSuccess: invalidate,
  })

  const markSent = useMutation({
    mutationFn: ({ id, sentTo }: { readonly id: string; readonly sentTo: string }) =>
      client.post<EInvoiceOut>(
        `/api/v1/einvoices/${id}/actions/mark-sent`,
        { sent_to: sentTo, sent_at: null },
        { datasetCode },
      ),
    onSuccess: invalidate,
  })

  const remove = useMutation({
    mutationFn: (id: string) => client.delete<void>(`/api/v1/einvoices/${id}`, { datasetCode }),
    onSuccess: invalidate,
  })

  const pump = useMutation({
    mutationFn: () =>
      client.post<{ readonly job_id: string }>('/api/v1/einvoices/outbox/actions/pump', {}, {
        datasetCode,
      }),
    onSuccess: invalidate,
  })

  const addNotice = useMutation({
    mutationFn: ({ id, body }: { readonly id: string; readonly body: ErrorNoticeIn }) =>
      client.post<ErrorNoticeOut>(
        `/api/v1/einvoices/${id}/notices`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
  })

  const resolveError = useMutation({
    mutationFn: ({ id, body }: { readonly id: string; readonly body: ResolveErrorIn }) =>
      client.post<ResolveErrorOut>(
        `/api/v1/einvoices/${id}/actions/resolve-error`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
    onSuccess: invalidate,
  })

  return { create, issue, markSent, remove, pump, addNotice, resolveError }
}
