/**
 * CRUD chứng từ tiền gửi (`/api/v1/bank/vouchers`) — SRS 04, cùng khuôn
 * `use-cash-voucher.ts` (hai module một hợp đồng dáng giống nhau, backend vẫn
 * hai router riêng).
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type BankVoucherOut = Schemas['BankVoucherOut']
export type BankVoucherIn = Schemas['BankVoucherIn']
export type BankVoucherUpdate = Schemas['BankVoucherUpdate']

export function useBankVoucher(id: string | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['bank-voucher', datasetCode, id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<BankVoucherOut>(`/api/v1/bank/vouchers/${String(id)}`, { datasetCode }),
  })
}

export function useCreateBankVoucher() {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
      acknowledgeWarnings,
    }: {
      readonly body: BankVoucherIn
      readonly idempotencyKey: string
      readonly acknowledgeWarnings?: boolean
    }) =>
      client.post<BankVoucherOut>(
        `/api/v1/bank/vouchers${acknowledgeWarnings === true ? '?acknowledge_warnings=true' : ''}`,
        body as unknown as Record<string, unknown>,
        { datasetCode, idempotencyKey },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
    },
  })
}

export function useUpdateBankVoucher(id: string) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (body: BankVoucherUpdate) =>
      client.put<BankVoucherOut>(
        `/api/v1/bank/vouchers/${id}`,
        body as unknown as Record<string, unknown>,
        { datasetCode },
      ),
    onSuccess: (voucher) => {
      void queryClient.invalidateQueries({ queryKey: ['cashflow', datasetCode] })
      queryClient.setQueryData(['bank-voucher', datasetCode, voucher.id], voucher)
    },
  })
}
