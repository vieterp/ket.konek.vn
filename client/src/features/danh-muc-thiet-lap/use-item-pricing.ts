/**
 * Dữ liệu màn khai giá của một mã hàng (7H-2b): mức giá (FR-SYS-042), bậc
 * chiết khấu (FR-SYS-045) và đơn vị quy đổi (đọc để làm cột ĐVT).
 *
 * Ba bảng con của `items`, ba đường ghi riêng của lát 7C-1 —
 * `/master/items/{id}/{prices,discount-tiers,units}`. Khóa truy vấn của mỗi
 * bảng con nằm dưới tiền tố riêng (không dưới `['catalog', …, 'items']`): sửa
 * tên mã hàng qua drawer không đổi giá của nó, và ngược lại — cùng lối
 * `partner-bank-accounts` ở `use-partner`.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import type { Schemas } from '@api-types'

import type { SelectOption } from '@/design-system/components'
import { ApiError, useSession } from '@/lib/session'

import { useMasterSearchLookup } from './use-master-search-lookup'

export type ItemPriceLevel = Schemas['ItemPriceLevelResponse']
export type ItemPriceLevelBody = Schemas['ItemPriceLevelCreateRequest']
export type ItemDiscountTier = Schemas['ItemDiscountTierResponse']
export type ItemDiscountTierBody = Schemas['ItemDiscountTierCreateRequest']
export type ItemUnit = Schemas['ItemUnitResponse']

const ITEMS_BASE = '/api/v1/master/items'

/** Bộ hook thêm/sửa/xóa cho MỘT bảng con — ba bảng con của lát này cùng hình dạng. */
export interface SubTableMutations<TRow, TBody> {
  readonly create: ReturnType<typeof useMutation<TRow, Error, { readonly body: TBody; readonly idempotencyKey: string }>>
  readonly update: ReturnType<typeof useMutation<TRow, Error, { readonly rowId: number; readonly body: TBody & { readonly row_version: number } }>>
  readonly remove: ReturnType<typeof useMutation<void, Error, number>>
}

/**
 * Thêm/sửa/xóa trên một đường `…/{base}` + `…/{base}/{rowId}` — thân request
 * đã có kiểu ở nơi dựng, hook chỉ lo đường đi và làm mới bộ nhớ đệm.
 */
export function useSubTableMutations<TRow, TBody>(
  base: string,
  queryKey: readonly unknown[],
): SubTableMutations<TRow, TBody> {
  const { client, datasetCode } = useSession()
  const queryClient = useQueryClient()
  const invalidate = (): void => {
    void queryClient.invalidateQueries({ queryKey })
  }
  const create = useMutation({
    mutationFn: ({ body, idempotencyKey }: { readonly body: TBody; readonly idempotencyKey: string }) =>
      client.post<TRow>(base, body as Record<string, unknown>, { datasetCode, idempotencyKey }),
    onSuccess: invalidate,
  })
  const update = useMutation({
    mutationFn: ({
      rowId,
      body,
    }: {
      readonly rowId: number
      readonly body: TBody & { readonly row_version: number }
    }) => client.put<TRow>(`${base}/${String(rowId)}`, body as Record<string, unknown>, { datasetCode }),
    onSuccess: invalidate,
    // 409: người khác vừa sửa dòng — nạp lại để lượt mở form kế tiếp cầm
    // `row_version` mới, thay vì 409 mãi tới khi tải lại trang (review 7H-2b M-4).
    onError: (caught) => {
      if (caught instanceof ApiError && caught.status === 409) {
        invalidate()
      }
    },
  })
  const remove = useMutation({
    mutationFn: (rowId: number) => client.delete<void>(`${base}/${String(rowId)}`, { datasetCode }),
    onSuccess: invalidate,
  })
  return { create, update, remove }
}

function useSubTable<TRow>(key: string, base: string | null) {
  const { client, datasetCode } = useSession()
  return useQuery({
    queryKey: [key, datasetCode, base],
    enabled: datasetCode !== null && base !== null,
    queryFn: async () => {
      const response = await client.get<{ readonly items: readonly TRow[] }>(base ?? '', { datasetCode })
      return response.items
    },
  })
}

export function useItemPriceLevels(itemId: number | null) {
  return useSubTable<ItemPriceLevel>('item-prices', itemId === null ? null : `${ITEMS_BASE}/${String(itemId)}/prices`)
}

export function useItemPriceLevelMutations(itemId: number) {
  const { datasetCode } = useSession()
  const base = `${ITEMS_BASE}/${String(itemId)}/prices`
  return useSubTableMutations<ItemPriceLevel, ItemPriceLevelBody>(base, ['item-prices', datasetCode, base])
}

export function useItemDiscountTiers(itemId: number | null) {
  return useSubTable<ItemDiscountTier>(
    'item-discount-tiers',
    itemId === null ? null : `${ITEMS_BASE}/${String(itemId)}/discount-tiers`,
  )
}

export function useItemDiscountTierMutations(itemId: number) {
  const { datasetCode } = useSession()
  const base = `${ITEMS_BASE}/${String(itemId)}/discount-tiers`
  return useSubTableMutations<ItemDiscountTier, ItemDiscountTierBody>(base, ['item-discount-tiers', datasetCode, base])
}

export function useItemUnits(itemId: number | null) {
  return useSubTable<ItemUnit>('item-units', itemId === null ? null : `${ITEMS_BASE}/${String(itemId)}/units`)
}

export interface ItemUnitChoices {
  /** Tùy chọn cho ô ĐVT: `''` = đơn vị chính (server nhận `unit_id: null`), còn lại là id đơn vị quy đổi. */
  readonly options: readonly SelectOption[]
  /** Tên hiển thị của một `unit_id` đã lưu (`null` = đơn vị chính). */
  readonly labelOf: (unitId: number | null) => string
  readonly isLoading: boolean
}

/**
 * Bộ chọn ĐVT của một mã hàng cho hai bảng giá — đơn vị chính viết bằng `NULL`
 * (7C-1: gửi id đơn vị chính lên bị từ chối), đơn vị quy đổi theo `item_units`.
 * Tên đơn vị tra `units_of_measure` theo `ids=` vì `ItemUnitResponse` chỉ mang id.
 */
export function useItemUnitChoices(
  itemId: number | null,
  baseUnitId: number | null,
  baseUnitLabel: string,
): ItemUnitChoices {
  const units = useItemUnits(itemId)
  const unitIds = [...(baseUnitId === null ? [] : [baseUnitId]), ...(units.data ?? []).map((row) => row.unit_id)]
  const unitLookup = useMasterSearchLookup('units_of_measure', unitIds)
  const nameOf = (unitId: number): string => {
    const option = unitLookup.byId.get(unitId)
    return option === undefined ? `#${String(unitId)}` : `${option.code} — ${option.label}`
  }
  const baseLabel = baseUnitId === null ? baseUnitLabel : `${baseUnitLabel} (${nameOf(baseUnitId)})`
  return {
    options: [
      { value: '', label: baseLabel },
      ...(units.data ?? []).map((row) => ({ value: String(row.unit_id), label: nameOf(row.unit_id) })),
    ],
    labelOf: (unitId) => (unitId === null ? baseLabel : nameOf(unitId)),
    isLoading: units.isPending || unitLookup.isLoading,
  }
}
