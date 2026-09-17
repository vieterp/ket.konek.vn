/**
 * Dữ liệu màn hình đối tác.
 *
 * Lượt đọc đầu tiên đi qua BFF `partners/{id}/overview` (nợ H56, trả ở lát
 * 7G-4): hồ sơ + thẻ công nợ là một màn hình đọc hai module (danh mục +
 * công nợ), đúng điều kiện RT-21. Khóa truy vấn nằm dưới tiền tố
 * `['catalog', datasetCode, 'partners']` để mọi lượt ghi danh mục (sửa qua
 * drawer, gộp bản ghi) làm mới cả thẻ — BFF chỉ đọc, ghi vẫn đi router module.
 * Tài khoản ngân hàng vẫn đọc thẳng `…/bank-accounts`: đó là bảng con có đường
 * ghi riêng, không phải phần của thẻ.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type PartnerOverview = Schemas['PartnerOverviewResponse']
export type Partner = PartnerOverview['partner']
export type PartnerDebt = PartnerOverview['debt']
export type PartnerBankAccount = Schemas['PartnerBankAccountResponse']
export type PartnerBankAccountBody = Schemas['PartnerBankAccountCreateRequest']

export function usePartnerOverview(id: number | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['catalog', datasetCode, 'partners', 'overview', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<PartnerOverview>(`/api/v1/partners/${String(id)}/overview`, { datasetCode }),
  })
}

export function usePartnerBankAccounts(partnerId: number | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['partner-bank-accounts', datasetCode, partnerId],
    enabled: datasetCode !== null && partnerId !== null,
    queryFn: async () => {
      const response = await client.get<Schemas['PartnerBankAccountListResponse']>(
        `/api/v1/master/partners/${String(partnerId)}/bank-accounts`,
        { datasetCode },
      )
      return response.items
    },
  })
}

export function usePartnerBankAccountMutations(partnerId: number) {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  const base = `/api/v1/master/partners/${String(partnerId)}/bank-accounts`

  const invalidate = (): void => {
    void queryClient.invalidateQueries({
      queryKey: ['partner-bank-accounts', datasetCode, partnerId],
    })
  }

  const create = useMutation({
    mutationFn: ({
      body,
      idempotencyKey,
    }: {
      readonly body: Readonly<Record<string, unknown>>
      readonly idempotencyKey: string
    }) => client.post<PartnerBankAccount>(base, body, { datasetCode, idempotencyKey }),
    onSuccess: invalidate,
  })

  const update = useMutation({
    mutationFn: ({
      accountId,
      body,
    }: {
      readonly accountId: number
      readonly body: Readonly<Record<string, unknown>>
    }) => client.put<PartnerBankAccount>(`${base}/${String(accountId)}`, body, { datasetCode }),
    onSuccess: invalidate,
  })

  const remove = useMutation({
    mutationFn: (accountId: number) =>
      client.delete<void>(`${base}/${String(accountId)}`, { datasetCode }),
    onSuccess: invalidate,
  })

  return { create, update, remove }
}
