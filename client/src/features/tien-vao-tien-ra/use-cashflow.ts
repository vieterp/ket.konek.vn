/**
 * Đọc BFF màn hình "Tiền vào tiền ra" (`/api/v1/cashflow/*`, lát 6E-1).
 *
 * Chỉ ĐỌC — ghi thì gọi API của module tương ứng (`/cash-book/*`, `/bank/*`).
 * Khóa truy vấn nằm dưới tiền tố `['cashflow', dataset]` nên mọi lệnh ghi
 * chứng từ chỉ cần invalidate tiền tố đó là cả hàng thẻ lẫn lưới cùng làm mới.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type CashflowOverview = Schemas['CashflowOverviewResponse']
export type CashAccountCard = Schemas['CashAccountCard']
export type BankAccountCard = Schemas['BankAccountCard']
export type CashflowTransactions = Schemas['CashflowTransactionsResponse']
export type CashflowTransaction = Schemas['CashflowTransaction']

/** Thẻ đang chọn trên hàng thẻ — quỹ theo SỐ HIỆU TK (hợp đồng 6E-1 M-4), ngân hàng theo id danh mục. */
export type SelectedCard =
  | { readonly source: 'cash'; readonly accountCode: string }
  | { readonly source: 'bank'; readonly bankAccountId: number }

export function useCashflowOverview() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['cashflow', datasetCode, 'overview'],
    enabled: datasetCode !== null,
    queryFn: () => client.get<CashflowOverview>('/api/v1/cashflow/overview', { datasetCode }),
  })
}

export const TRANSACTION_PAGE_SIZE = 50

export interface TransactionFilters {
  readonly card: SelectedCard
  readonly fromDate?: string
  readonly toDate?: string
  readonly status?: number
  readonly page: number
}

export function useCashflowTransactions(filters: TransactionFilters | null) {
  const { client, datasetCode } = useSession()

  const params = new URLSearchParams()
  if (filters !== null) {
    params.set('source', filters.card.source)
    if (filters.card.source === 'cash') {
      params.set('cash_account_code', filters.card.accountCode)
    } else {
      params.set('bank_account_id', String(filters.card.bankAccountId))
    }
    if (filters.fromDate !== undefined && filters.fromDate !== '') {
      params.set('from_date', filters.fromDate)
    }
    if (filters.toDate !== undefined && filters.toDate !== '') {
      params.set('to_date', filters.toDate)
    }
    if (filters.status !== undefined) {
      params.set('status', String(filters.status))
    }
    params.set('limit', String(TRANSACTION_PAGE_SIZE))
    params.set('offset', String((filters.page - 1) * TRANSACTION_PAGE_SIZE))
  }

  return useQuery({
    queryKey: ['cashflow', datasetCode, 'transactions', params.toString()],
    enabled: datasetCode !== null && filters !== null,
    queryFn: () =>
      client.get<CashflowTransactions>(`/api/v1/cashflow/transactions?${params.toString()}`, {
        datasetCode,
      }),
  })
}
