/**
 * Tra nghiệp vụ định khoản tự động (`GET /api/v1/auto-posting/operations`,
 * FR-SYS-025): chọn nghiệp vụ trên form phiếu → điền sẵn cặp Nợ/Có theo gói
 * cấu hình hiệu lực tại ngày hạch toán. Đổi ngày là tra lại — gói hiệu lực
 * theo ngày, cùng luật với `/accounts`.
 */

import { useQuery } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import { useSession } from '@/lib/session'

export type AutoPostingOperation = Schemas['AutoPostingOperationResponse']

export function useAutoPostingOperations(documentType: string | null, onDate: string) {
  const { client, datasetCode } = useSession()

  return useQuery({
    queryKey: ['auto-posting', datasetCode, documentType, onDate],
    enabled: datasetCode !== null && documentType !== null && onDate !== '',
    queryFn: () =>
      client.get<Schemas['AutoPostingOperationsResponse']>(
        `/api/v1/auto-posting/operations?document_type=${encodeURIComponent(documentType ?? '')}&on_date=${onDate}`,
        { datasetCode },
      ),
  })
}
