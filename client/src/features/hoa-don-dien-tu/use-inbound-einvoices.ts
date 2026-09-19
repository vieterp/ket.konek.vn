/**
 * Hook hóa đơn điện tử ĐẦU VÀO (`/api/v1/einvoices/inbound/*`, 7F-2b /
 * FR-EIV-040): nạp tệp XML chuẩn TCT, danh sách, chi tiết dòng, lập chứng từ
 * mua từ tờ hóa đơn, xóa tờ chưa vào sổ.
 *
 * Lượt nạp là multipart (`postForm`) — cùng đường nhập Excel danh mục; khóa
 * idempotency sinh mỗi lần CHỌN tệp ở chỗ gọi, vì vân tay server gồm hash nội
 * dung tệp: cùng tệp gửi lại là 200 "đã nạp", không phải 409.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type InboundEInvoiceOut = Schemas['InboundEInvoiceOut']
export type InboundEInvoiceListOut = Schemas['InboundEInvoiceListOut']
export type InboundPurchaseIn = Schemas['InboundPurchaseIn']

export const INBOUND_PAGE_SIZE = 50
const BASE = '/api/v1/einvoices/inbound'

export function useInboundList(pendingOnly: boolean, page: number) {
  const { client, datasetCode } = useSession()
  const url = `${BASE}?pending_only=${pendingOnly ? 'true' : 'false'}&limit=${String(INBOUND_PAGE_SIZE)}&offset=${String((page - 1) * INBOUND_PAGE_SIZE)}`
  return useQuery({
    queryKey: ['einvoices-inbound', datasetCode, 'list', url],
    enabled: datasetCode !== null,
    queryFn: () => client.get<InboundEInvoiceListOut>(url, { datasetCode }),
  })
}

export function useInboundInvoice(id: string | null) {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: ['einvoices-inbound', datasetCode, 'one', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () => client.get<InboundEInvoiceOut>(`${BASE}/${String(id)}`, { datasetCode }),
  })
}

export function useInboundActions() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey: ['einvoices-inbound', datasetCode] })
  }

  const importXml = useMutation({
    mutationFn: ({ file, idempotencyKey }: { readonly file: File; readonly idempotencyKey: string }) => {
      const form = new FormData()
      form.append('file', file)
      return client.postForm<InboundEInvoiceOut>(`${BASE}/import`, form, {
        datasetCode,
        idempotencyKey,
      })
    },
    onSuccess: invalidate,
  })

  const createPurchase = useMutation({
    mutationFn: ({
      id,
      body,
      idempotencyKey,
    }: {
      readonly id: string
      readonly body: InboundPurchaseIn
      readonly idempotencyKey: string
    }) =>
      client.post<InboundEInvoiceOut>(
        `${BASE}/${id}/actions/create-purchase`,
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      invalidate()
      void queryClient.invalidateQueries({ queryKey: ['purchase', datasetCode] })
    },
  })

  const remove = useMutation({
    mutationFn: (id: string) => client.delete<void>(`${BASE}/${id}`, { datasetCode }),
    onSuccess: invalidate,
  })

  return { importXml, createPurchase, remove }
}

export function inboundXmlPath(id: string): string {
  return `${BASE}/${id}/xml`
}
