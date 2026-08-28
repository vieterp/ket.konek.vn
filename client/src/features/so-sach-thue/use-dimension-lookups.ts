/**
 * Tra mã chiều hạch toán trên lưới chứng từ (đối tượng, đối tượng THCP, công
 * trình, hợp đồng, khoản mục chi phí, vật tư hàng hóa, kho) bằng `search=`/
 * `ids=` SERVER-SIDE — trả nợ M-B (review 6F-1): bản cũ nạp trọn danh mục qua
 * `useLookupOptions` nên đứt ở trần trang 200; danh mục đối tác/vật tư thật
 * vượt trần đó ngay năm đầu.
 *
 * Ba lượt đọc, cùng khuôn `use-master-search-lookup` của nhóm 03:
 *
 * 1. **Seed** một trang đầu mỗi danh mục — mã phổ biến tra được ngay không chờ
 *    mạng, và danh mục nhỏ (kho, khoản mục) thì seed đã là trọn bộ.
 * 2. **Tra bù theo `ids=`** cho chứng từ đang SỬA — bản ghi của dòng cũ có thể
 *    xếp sau trang đầu; thiếu lượt này ô chiều hiện trống và lượt Cất kế tiếp
 *    xóa lặng lẽ tham chiếu (cùng họ review 4E H-1).
 * 3. **`resolveMissingCodes`** lúc Cất: mã người dùng gõ không có trong hai
 *    lượt trên được tra `search=` theo TỪNG mã rồi rà lại — hai-lượt-rà nằm ở
 *    form, hook chỉ hứa "tra xong trả bản đồ đã gộp". Mã sai thật sự thì lượt
 *    rà thứ hai báo đúng lỗi cũ, không có đường lỗi mới.
 *
 * Khóa truy vấn giữ tiền tố `['catalog', dataset, slug, …]` nên một lệnh ghi
 * danh mục ở màn hình khác vẫn tự làm mới seed ở đây.
 */

import { useCallback, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import type { LookupOption } from '@/design-system/components'
import { useSession } from '@/lib/session'

import type { CatalogPage, CatalogRow } from '@/features/danh-muc-thiet-lap/catalog-types'

/** Trần một lượt đọc — MAX_PAGE_SIZE phía server (H52). */
const SEED_LIMIT = 200
const SEARCH_LIMIT = 20

/** Các danh mục chiều — khớp `DIMENSION_CATALOG_SLUG` của `dimension-config`. */
const DIMENSION_SLUGS = [
  'partners',
  'employees',
  'cost_objects',
  'projects',
  'contracts',
  'expense_items',
  'items',
  'warehouses',
] as const

type DimensionSlug = (typeof DIMENSION_SLUGS)[number]

/** Id bản ghi chiều chứng từ đang sửa tham chiếu, gom theo slug danh mục. */
export type RequiredDimensionIds = Partial<Record<DimensionSlug, readonly number[]>>

/** Một mã gõ trên lưới chưa tra được bằng dữ liệu đang có — đầu vào của lượt tra bù. */
export interface MissingDimensionCode {
  readonly slug: string
  readonly code: string
}

export type DimensionOptionsBySlug = Readonly<
  Record<string, readonly LookupOption[] | undefined>
>

export interface DimensionLookups {
  /** Khóa theo slug danh mục (`DIMENSION_CATALOG_SLUG`), không theo tên chiều. */
  readonly options: DimensionOptionsBySlug
  readonly isLoading: boolean
  /**
   * Tra server các mã chưa có rồi trả bản đồ ĐÃ GỘP để rà lại NGAY — không đọc
   * lại `options` sau await (setState chưa kịp chảy về trong cùng lượt gọi).
   */
  readonly resolveMissingCodes: (
    missing: readonly MissingDimensionCode[],
  ) => Promise<DimensionOptionsBySlug>
}

/**
 * Gom id chiều mà các dòng đã lưu tham chiếu — đầu vào `requiredIds` khi mở
 * form SỬA. Dòng của GLE lẫn phiếu quỹ/ngân hàng cùng hình dạng trường chiều
 * nên dùng chung một hàm.
 */
export interface DimensionIdCarrier {
  readonly partner_kind?: number | null
  readonly partner_id?: number | null
  readonly cost_object_id?: number | null
  readonly project_id?: number | null
  readonly contract_id?: number | null
  readonly expense_item_id?: number | null
  readonly item_id?: number | null
  readonly warehouse_id?: number | null
}

const CARRIER_FIELD_SLUG = {
  cost_object_id: 'cost_objects',
  project_id: 'projects',
  contract_id: 'contracts',
  expense_item_id: 'expense_items',
  item_id: 'items',
  warehouse_id: 'warehouses',
} as const

export function requiredDimensionIdsOf(
  lines: readonly DimensionIdCarrier[],
): RequiredDimensionIds {
  const bySlug = new Map<DimensionSlug, Set<number>>()
  const add = (slug: DimensionSlug, id: number | null | undefined): void => {
    if (id === null || id === undefined) {
      return
    }
    const existing = bySlug.get(slug)
    if (existing === undefined) {
      bySlug.set(slug, new Set([id]))
    } else {
      existing.add(id)
    }
  }
  for (const line of lines) {
    // `partner_kind` 0/1 (khách hàng/NCC) sống ở danh mục đối tác gộp, 2 ở
    // danh mục nhân viên — khớp `PARTNER_KIND_BY_DIMENSION`.
    if (line.partner_id !== null && line.partner_id !== undefined) {
      add(line.partner_kind === 2 ? 'employees' : 'partners', line.partner_id)
    }
    for (const [field, slug] of Object.entries(CARRIER_FIELD_SLUG)) {
      add(slug, line[field as keyof typeof CARRIER_FIELD_SLUG])
    }
  }
  const result: Partial<Record<DimensionSlug, readonly number[]>> = {}
  for (const [slug, ids] of bySlug) {
    result[slug] = [...ids]
  }
  return result
}

function toOptions(rows: readonly CatalogRow[]): LookupOption[] {
  // Nút nhóm bị loại (không phải đích tham chiếu) — cùng luật `useLookupOptions`.
  return rows
    .filter((row) => !row.is_group)
    .map((row) => ({ id: row.id, code: row.code, label: row.name }))
}

interface SlugLookup {
  readonly options: readonly LookupOption[]
  readonly isPending: boolean
  /** Tra `search=` cho từng mã, gộp vào state, trả phần vừa tra được. */
  readonly fetchCodes: (codes: readonly string[]) => Promise<readonly LookupOption[]>
}

function useSlugLookup(slug: DimensionSlug, requiredIds: readonly number[]): SlugLookup {
  const { client, datasetCode } = useSession()
  // Kết quả tra bù theo mã — gắn dataset để đổi bộ dữ liệu không mang lộn
  // bản ghi của bộ cũ (cùng lý do review 6F-1 M-C gắn slug).
  const [extra, setExtra] = useState<{
    readonly datasetCode: string | null
    readonly map: ReadonlyMap<number, LookupOption>
  }>({ datasetCode: null, map: new Map() })
  const extraMap = extra.datasetCode === datasetCode ? extra.map : new Map<number, LookupOption>()

  const seed = useQuery({
    queryKey: ['catalog', datasetCode, slug, 'dim-lookup-seed'],
    enabled: datasetCode !== null,
    queryFn: async () => {
      const page = await client.get<CatalogPage>(
        `/api/v1/master/${slug}?limit=${String(SEED_LIMIT)}`,
        { datasetCode },
      )
      return toOptions(page.items)
    },
  })

  const seededIds = new Set((seed.data ?? []).map((option) => option.id))
  const missingIds = seed.isPending
    ? []
    : [...new Set(requiredIds)].filter((id) => !seededIds.has(id) && !extraMap.has(id))
  const hydrate = useQuery({
    queryKey: ['catalog', datasetCode, slug, 'dim-lookup-ids', missingIds.join(',')],
    enabled: datasetCode !== null && missingIds.length > 0,
    queryFn: async () => {
      const page = await client.get<CatalogPage>(
        `/api/v1/master/${slug}?limit=${String(SEED_LIMIT)}&` +
          missingIds.map((id) => `ids=${String(id)}`).join('&'),
        { datasetCode },
      )
      return toOptions(page.items)
    },
  })

  const merged = new Map<number, LookupOption>()
  for (const option of seed.data ?? []) {
    merged.set(option.id, option)
  }
  for (const option of hydrate.data ?? []) {
    merged.set(option.id, option)
  }
  for (const option of extraMap.values()) {
    merged.set(option.id, option)
  }

  const fetchCodes = useCallback(
    async (codes: readonly string[]): Promise<readonly LookupOption[]> => {
      if (datasetCode === null || codes.length === 0) {
        return []
      }
      const settled = await Promise.all(
        codes.map(async (code) => {
          try {
            const page = await client.get<CatalogPage>(
              `/api/v1/master/${slug}?search=${encodeURIComponent(code)}&limit=${String(SEARCH_LIMIT)}`,
              { datasetCode },
            )
            return toOptions(page.items)
          } catch {
            // Mạng đứt giữa lượt tra bù: trả rỗng để lượt rà thứ hai báo "mã
            // không tra được" — một thông điệp, một đường xử lý.
            return []
          }
        }),
      )
      const fetched = settled.flat()
      if (fetched.length > 0) {
        setExtra((current) => {
          const base =
            current.datasetCode === datasetCode ? current.map : new Map<number, LookupOption>()
          const next = new Map(base)
          for (const option of fetched) {
            next.set(option.id, option)
          }
          return { datasetCode, map: next }
        })
      }
      return fetched
    },
    [client, datasetCode, slug],
  )

  return {
    options: [...merged.values()],
    isPending: seed.isPending || (missingIds.length > 0 && hydrate.isPending),
    fetchCodes,
  }
}

export function useDimensionLookups(requiredIds: RequiredDimensionIds = {}): DimensionLookups {
  const partners = useSlugLookup('partners', requiredIds.partners ?? [])
  const employees = useSlugLookup('employees', requiredIds.employees ?? [])
  const costObjects = useSlugLookup('cost_objects', requiredIds.cost_objects ?? [])
  const projects = useSlugLookup('projects', requiredIds.projects ?? [])
  const contracts = useSlugLookup('contracts', requiredIds.contracts ?? [])
  const expenseItems = useSlugLookup('expense_items', requiredIds.expense_items ?? [])
  const items = useSlugLookup('items', requiredIds.items ?? [])
  const warehouses = useSlugLookup('warehouses', requiredIds.warehouses ?? [])

  const bySlug: Record<DimensionSlug, SlugLookup> = {
    partners,
    employees,
    cost_objects: costObjects,
    projects,
    contracts,
    expense_items: expenseItems,
    items,
    warehouses,
  }

  const options: DimensionOptionsBySlug = {
    partners: partners.options,
    employees: employees.options,
    cost_objects: costObjects.options,
    projects: projects.options,
    contracts: contracts.options,
    expense_items: expenseItems.options,
    items: items.options,
    warehouses: warehouses.options,
  }

  const resolveMissingCodes = async (
    missing: readonly MissingDimensionCode[],
  ): Promise<DimensionOptionsBySlug> => {
    const codesBySlug = new Map<DimensionSlug, Set<string>>()
    for (const entry of missing) {
      const slug = DIMENSION_SLUGS.find((known) => known === entry.slug)
      if (slug === undefined) {
        continue
      }
      const existing = codesBySlug.get(slug)
      if (existing === undefined) {
        codesBySlug.set(slug, new Set([entry.code]))
      } else {
        existing.add(entry.code)
      }
    }
    const fetchedBySlug = await Promise.all(
      [...codesBySlug].map(async ([slug, codes]) => {
        const fetched = await bySlug[slug].fetchCodes([...codes])
        return [slug, fetched] as const
      }),
    )
    // Bản đồ gộp trả THẲNG cho lượt rà thứ hai — state đã merge cho các lượt
    // render sau, nhưng lượt gọi hiện tại không chờ được vòng render đó.
    const snapshot: Record<string, readonly LookupOption[] | undefined> = { ...options }
    for (const [slug, fetched] of fetchedBySlug) {
      if (fetched.length === 0) {
        continue
      }
      const mergedMap = new Map<number, LookupOption>()
      for (const option of bySlug[slug].options) {
        mergedMap.set(option.id, option)
      }
      for (const option of fetched) {
        mergedMap.set(option.id, option)
      }
      snapshot[slug] = [...mergedMap.values()]
    }
    return snapshot
  }

  return {
    options,
    isLoading: Object.values(bySlug).some((lookup) => lookup.isPending),
    resolveMissingCodes,
  }
}
