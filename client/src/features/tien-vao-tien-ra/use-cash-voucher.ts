/**
 * CRUD phiếu thu/chi tiền mặt (`/api/v1/cash-book/vouchers`) — SRS 03.
 *
 * `POST` nhận `?acknowledge_warnings=true` cho lượt gửi lại sau khi người dùng
 * bấm "Vẫn ghi sổ?" (FR-SYS-062 — chỉ có tác dụng trên lượt ghi sổ đi kèm khi
 * bật Cất-đồng-thời-ghi-sổ). `PUT` không đòi khóa chống trùng: khóa lạc quan
 * `row_version` đã chặn lượt gửi lặp bằng 409.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type CashVoucherOut = Schemas['CashVoucherOut']
export type CashVoucherIn = Schemas['CashVoucherIn']
export type CashVoucherUpdate = Schemas['CashVoucherUpdate']

export function useCashVoucher(id: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['cash-voucher', datasetCode, id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<CashVoucherOut>(`/api/v1/cash-book/vouchers/${String(id)}`, { datasetCode }),
  })
}

export function useCreateCashVoucher() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
      acknowledgeWarnings,
    }: {
      readonly body: CashVoucherIn
      readonly idempotencyKey: string
      readonly acknowledgeWarnings?: boolean
    }) =>
      client.post<CashVoucherOut>(
        `/api/v1/cash-book/vouchers${acknowledgeWarnings === true ? '?acknowledge_warnings=true' : ''}`,
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
    },
  })
}

export function useUpdateCashVoucher(id: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (body: CashVoucherUpdate) =>
      client.put<CashVoucherOut>(
        `/api/v1/cash-book/vouchers/${id}`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
    onSuccess: (voucher) => {
      void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
      queryClient.setQueryData(['cash-voucher', datasetCode, voucher.id], voucher)
    },
  })
}
