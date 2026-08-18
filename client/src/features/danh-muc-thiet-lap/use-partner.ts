/**
 * Dữ liệu màn hình đối tác.
 *
 * Đọc **thẳng** router của module (`/master/partners/{id}` + `…/bank-accounts`)
 * chứ không BFF: thẻ công nợ cần `ar_ap_ledger` của module `receivables`
 * (phase 7), nên BFF `partners/{id}/overview` hoãn tới đó (H56) — dựng bây giờ
 * là một BFF đọc một module, trái RT-21.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type Partner = Schemas['PartnersResponse']
export type PartnerBankAccount = Schemas['PartnerBankAccountResponse']
export type PartnerBankAccountBody = Schemas['PartnerBankAccountCreateRequest']

export function usePartner(id: number | null) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['catalog', datasetCode, 'partners', 'record', id],
    enabled: datasetCode !== null && id !== null,
    queryFn: () =>
      client.get<Partner>(`/api/v1/master/partners/${String(id)}`, { datasetCode }),
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
