/**
 * Bảng cân đối tài khoản của một (kỳ, sổ) — `stale=true` khi kỳ còn dấu bẩn
 * chưa tính lại snapshot, số khi đó tính thẳng từ sổ cái.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type TrialBalance = Schemas['TrialBalanceResponse']
export type TrialBalanceRow = Schemas['TrialBalanceRowResponse']

export function useTrialBalance(periodId: number | null, ledger: number) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['ledger', datasetCode, 'trial-balance', periodId, ledger],
    enabled: datasetCode !== null && periodId !== null,
    queryFn: () =>
      client.get<TrialBalance>(
        `/api/v1/ledger/trial-balance?period_id=${String(periodId)}&ledger=${String(ledger)}`,
        { datasetCode },
      ),
  })
}
