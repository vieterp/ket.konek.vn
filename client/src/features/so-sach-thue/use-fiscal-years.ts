/**
 * Năm tài chính + kỳ, để dựng bộ chọn của bảng cân đối tài khoản. Năm mới
 * nhất trước, kỳ trong mỗi năm xếp theo `period_no` (hợp đồng của server).
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type FiscalYear = Schemas['FiscalYearResponse']
export type Period = Schemas['PeriodResponse']

export function useFiscalYears() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['fiscal-years', datasetCode],
    enabled: datasetCode !== null,
    queryFn: async (): Promise<readonly FiscalYear[]> => {
      const response = await client.get<Schemas['FiscalYearListResponse']>(
        '/api/v1/fiscal-years',
        { datasetCode },
      )
      return response.items
    },
  })
}
