/**
 * BFF "việc còn thiếu" (U1) cho màn hình chứng từ — nguồn của các tab trạng
 * thái và của băng cảnh báo "kỳ đang chờ tính lại".
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type PendingIssues = Schemas['PendingIssuesResponse']

export function usePendingIssues() {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['vouchers', datasetCode, 'pending-issues'],
    enabled: datasetCode !== null,
    queryFn: () =>
      client.get<PendingIssues>('/api/v1/vouchers/pending-issues', { datasetCode }),
  })
}
