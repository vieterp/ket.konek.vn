/**
 * Nạp trọn các danh mục nhỏ dùng để tra mã chiều hạch toán trên lưới chứng từ
 * (đối tượng, đối tượng THCP, công trình, hợp đồng, khoản mục chi phí, vật tư
 * hàng hóa, kho).
 *
 * Dùng lại đúng cơ chế `useLookupOptions` của nhóm Danh mục & Thiết lập (đọc,
 * không sửa file đó): cùng khóa truy vấn `['catalog', dataset, slug, …]` nên
 * một lệnh ghi danh mục ở màn hình kia tự làm mới danh sách ở đây, không cần
 * tự tay đồng bộ hai nơi.
 */

import { useLookupOptions } from '@/features/danh-muc-thiet-lap/use-lookup-options'
import type { LookupOption } from '@/design-system/components'

export interface DimensionLookups {
  /** Khóa theo slug danh mục (`DIMENSION_CATALOG_SLUG`), không theo tên chiều. */
  readonly options: Readonly<Record<string, readonly LookupOption[] | undefined>>
  readonly isLoading: boolean
}

export function useDimensionLookups(): DimensionLookups {
  const partners = useLookupOptions('partners')
  const employees = useLookupOptions('employees')
  const costObjects = useLookupOptions('cost_objects')
  const projects = useLookupOptions('projects')
  const contracts = useLookupOptions('contracts')
  const expenseItems = useLookupOptions('expense_items')
  const items = useLookupOptions('items')
  const warehouses = useLookupOptions('warehouses')

  return {
    options: {
      partners: partners.data,
      employees: employees.data,
      cost_objects: costObjects.data,
      projects: projects.data,
      contracts: contracts.data,
      expense_items: expenseItems.data,
      items: items.data,
      warehouses: warehouses.data,
    },
    isLoading:
      partners.isPending ||
      employees.isPending ||
      costObjects.isPending ||
      projects.isPending ||
      contracts.isPending ||
      expenseItems.isPending ||
      items.isPending ||
      warehouses.isPending,
  }
}
