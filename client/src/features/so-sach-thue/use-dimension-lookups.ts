/**
 * Tra mã chiều hạch toán trên lưới chứng từ (đối tượng, đối tượng THCP, công
 * trình, hợp đồng, khoản mục chi phí, vật tư hàng hóa, kho) bằng `search=`/
 * `ids=` SERVER-SIDE — trả nợ M-B (review 6F-1): bản cũ nạp trọn danh mục qua
 * `useLookupOptions` nên đứt ở trần trang 200; danh mục đối tác/vật tư thật
 * vượt trần đó ngay năm đầu.
 *
 * Ba lượt đọc, do CHÍNH `use-master-search-lookup` lo (lát 6G-2 gộp hai bản
 * gần-giống thành một — tệp này nay chỉ còn phần ghép chín chiều lại):
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

import type { LookupOption } from '@/design-system/components'

import {
  useMasterSearchLookup,
  type MasterSearchLookup,
} from '@/features/danh-muc-thiet-lap/use-master-search-lookup'
import { DIMENSION_CATALOG_SLUG, DIMENSION_LINE_FIELD } from './dimension-config'

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
  'company_bank_accounts',
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
  readonly bank_account_id?: number | null
}

/**
 * Trường-trên-dòng → slug danh mục, DỰNG TỪ hai bảng của `dimension-config`
 * thay vì chép tay lần thứ ba.
 *
 * Bản chép tay đã bỏ sót chiều thứ mười một (`bank_account`) và lỗi ấy im lặng
 * theo đúng kiểu tệ nhất: không có lượt `?ids=` nào ⇒ TK ngân hàng ngoài trang
 * seed (ngừng theo dõi, hoặc danh mục > 200) không tra được ⇒ ô hiện TRỐNG ⇒
 * PUT thay-trọn-bộ gửi dòng KHÔNG có `bank_account_id`, tức xóa dữ liệu mà
 * không một thông báo nào (review 6G-1 H-3, cùng họ 6F-1 C-1).
 *
 * `order` bị loại vì không có danh mục để tra (module Đơn hàng thuộc phase 7) —
 * chính `DIMENSION_CATALOG_SLUG` là nơi nói điều đó, nên phép lọc dưới đây tự
 * đúng theo nó. Ba chiều đối tượng đi đường riêng (`partner_id`+`partner_kind`).
 */
const CARRIER_FIELD_SLUG: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(DIMENSION_LINE_FIELD).flatMap(([dimension, field]) => {
    const slug = DIMENSION_CATALOG_SLUG[dimension]
    return field !== undefined && slug !== undefined ? [[field, slug] as const] : []
  }),
)

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
      add(slug as DimensionSlug, line[field as keyof DimensionIdCarrier])
    }
  }
  const result: Partial<Record<DimensionSlug, readonly number[]>> = {}
  for (const [slug, ids] of bySlug) {
    result[slug] = [...ids]
  }
  return result
}

export function useDimensionLookups(requiredIds: RequiredDimensionIds = {}): DimensionLookups {
  const partners = useMasterSearchLookup('partners', requiredIds.partners ?? [])
  const employees = useMasterSearchLookup('employees', requiredIds.employees ?? [])
  const costObjects = useMasterSearchLookup('cost_objects', requiredIds.cost_objects ?? [])
  const projects = useMasterSearchLookup('projects', requiredIds.projects ?? [])
  const contracts = useMasterSearchLookup('contracts', requiredIds.contracts ?? [])
  const expenseItems = useMasterSearchLookup('expense_items', requiredIds.expense_items ?? [])
  const items = useMasterSearchLookup('items', requiredIds.items ?? [])
  const warehouses = useMasterSearchLookup('warehouses', requiredIds.warehouses ?? [])
  const companyBankAccounts = useMasterSearchLookup(
    'company_bank_accounts',
    requiredIds.company_bank_accounts ?? [],
  )

  const bySlug: Record<DimensionSlug, MasterSearchLookup> = {
    partners,
    employees,
    cost_objects: costObjects,
    projects,
    contracts,
    expense_items: expenseItems,
    items,
    warehouses,
    company_bank_accounts: companyBankAccounts,
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
    company_bank_accounts: companyBankAccounts.options,
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
    isLoading: Object.values(bySlug).some((lookup) => lookup.isLoading),
    resolveMissingCodes,
  }
}
