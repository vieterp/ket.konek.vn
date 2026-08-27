/**
 * Hàng đợi + sổ quỹ thủ quỹ (`/api/v1/treasurer/*` — API lát 6C, UI lát 6F-2).
 *
 * Ghi sổ quỹ hàng loạt là MỘT transaction phía server (50 phiếu cùng sống cùng
 * chết) và có khóa idempotency route-khai — client gửi khóa mới cho mỗi lượt
 * bấm, giữ nguyên khóa khi gửi lại sau lỗi mạng.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type TreasurerQueueItem = Schemas['TreasurerQueueItem']
export type TreasurerCashBookRow = Schemas['TreasurerCashBookRowOut']
export type TreasurerBookRequest = Schemas['TreasurerBookRequest']

export function useTreasurerQueue() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['treasurer', datasetCode, 'queue'],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<Schemas['TreasurerQueueResponse']>('/api/v1/treasurer/queue', { datasetCode }),
  })
}

export const TREASURER_BOOK_PAGE_SIZE = 50

export interface TreasurerCashBookFilters {
  readonly cashAccountId?: number | undefined
  readonly fromDate?: string | undefined
  readonly toDate?: string | undefined
  readonly page: number
}

export function useTreasurerCashBook(filters: TreasurerCashBookFilters) {
  const { client, datasetCode } = useSession()

  const params = new URLSearchParams()
  if (filters.cashAccountId !== undefined) {
    params.set('cash_account_id', String(filters.cashAccountId))
  }
  if (filters.fromDate !== undefined && filters.fromDate !== '') {
    params.set('from_date', filters.fromDate)
  }
  if (filters.toDate !== undefined && filters.toDate !== '') {
    params.set('to_date', filters.toDate)
  }
  params.set('limit', String(TREASURER_BOOK_PAGE_SIZE))
  params.set('offset', String((filters.page - 1) * TREASURER_BOOK_PAGE_SIZE))

  return useQuery({
    queryKey: ['treasurer', datasetCode, 'cash-book', params.toString()],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<Schemas['TreasurerCashBookResponse']>(
        `/api/v1/treasurer/cash-book?${params.toString()}`,
        { datasetCode },
      ),
  })
}

export function useBookVouchers() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
    }: {
      readonly body: TreasurerBookRequest
      readonly idempotencyKey: string
    }) =>
      client.post<Schemas['TreasurerBookResponse']>(
        '/api/v1/treasurer/queue/actions/book',
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['treasurer', datasetCode] })
      // Trạng thái thủ quỹ hiện trên lưới giao dịch của màn chính.
      void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
    },
  })
}
