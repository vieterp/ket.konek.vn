/**
 * Dòng của một bảng giá (FR-SAL-020) — bảng con của `price_lists`, đường
 * `/master/price_lists/{id}/lines` (lát 7C-1). Không phân trang: server trả
 * trọn bộ dòng của một bảng giá.
 */

import type { Schemas } from '@api-types'

import { useQuery } from '@tanstack/react-query'

import { useSession } from '@/lib/session'

import { useSubTableMutations } from './use-item-pricing'

export type PriceListLine = Schemas['PriceListLineResponse']
export type PriceListLineBody = Schemas['PriceListLineCreateRequest']

function linesPath(priceListId: number): string {
  return `/api/v1/master/price_lists/${String(priceListId)}/lines`
}

export function usePriceListLines(priceListId: number | null) {
  const { client, datasetCode } = useSession()
  const base = priceListId === null ? null : linesPath(priceListId)
  return useQuery({
    queryKey: ['price-list-lines', datasetCode, base],
    enabled: datasetCode !== null && base !== null,
    queryFn: async () => {
      const response = await client.get<Schemas['PriceListLineListResponse']>(base ?? '', { datasetCode })
      return response.items
    },
  })
}

export function usePriceListLineMutations(priceListId: number) {
  const { datasetCode } = useSession()
  const base = linesPath(priceListId)
  return useSubTableMutations<PriceListLine, PriceListLineBody>(base, ['price-list-lines', datasetCode, base])
}
