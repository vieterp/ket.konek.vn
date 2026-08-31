/**
 * Cấu hình các chiều hạch toán (`detail_tracking`, FR-SYS-021) cho lưới nhập
 * chứng từ — cột nào hiện, danh mục nào tra mã, trường nào trên `JournalLineIn`
 * nhận id đã tra được.
 *
 * `customer`/`vendor`/`employee` gộp chung MỘT cột "Mã đối tượng": một TK chỉ
 * khai đúng một trong ba (TK phải thu gộp `customer`, TK phải trả gộp
 * `vendor`…), nên không có dòng nào cần hai cột đối tượng cùng lúc — gộp cột
 * đỡ người dùng phải phân biệt "đối tượng nào vào cột nào".
 */

import type { TranslationKey } from '@/locales/vi'

/** Vai trò đối tượng khớp `PartnerKind` phía server (0 khách hàng · 1 nhà cung cấp · 2 nhân viên). */
export const PARTNER_KIND_BY_DIMENSION: Readonly<Record<string, 0 | 1 | 2>> = {
  customer: 0,
  vendor: 1,
  employee: 2,
}

/**
 * Danh mục tra cứu cho từng giá trị `detail_tracking`.
 *
 * `order` (đơn hàng) cố ý KHÔNG có ở đây: module Đơn hàng thuộc phase 7, chưa
 * có danh mục nào để tra. Cột vẫn hiện nếu một TK khai chiều này, nhưng mọi mã
 * gõ vào cột đó luôn bị chặn ở bước lưu (`journal-line-resolve.ts`) vì không
 * có gì để đối chiếu — thà chặn rõ ràng còn hơn gửi lên server một
 * `order_id` đoán mò.
 */
export const DIMENSION_CATALOG_SLUG: Readonly<Partial<Record<string, string>>> = {
  customer: 'partners',
  vendor: 'partners',
  employee: 'employees',
  cost_object: 'cost_objects',
  project: 'projects',
  contract: 'contracts',
  expense_item: 'expense_items',
  item: 'items',
  warehouse: 'warehouses',
  bank_account: 'company_bank_accounts',
}

/** Trường trên `JournalLineIn` nhận id đã tra được — trừ đối tượng (dùng `partner_id`/`partner_kind`). */
export const DIMENSION_LINE_FIELD: Readonly<Partial<Record<string, string>>> = {
  cost_object: 'cost_object_id',
  project: 'project_id',
  order: 'order_id',
  contract: 'contract_id',
  expense_item: 'expense_item_id',
  item: 'item_id',
  warehouse: 'warehouse_id',
  bank_account: 'bank_account_id',
}

export interface DimensionColumn {
  /** Khóa cột trên lưới — cũng là khóa trong `LineRow.dims`. */
  readonly key: string
  readonly headerKey: TranslationKey
  /** Giá trị `detail_tracking` mà cột này gộp chung — một TK chỉ khai đúng một trong số đó. */
  readonly values: readonly string[]
}

/** Thứ tự cột chiều trên lưới — cố định, không đổi theo TK đang gõ (chỉ ẩn/hiện). */
export const DIMENSION_COLUMNS: readonly DimensionColumn[] = [
  { key: 'partner', headerKey: 'gl.dim.partner', values: ['customer', 'vendor', 'employee'] },
  { key: 'cost_object', headerKey: 'gl.dim.costObject', values: ['cost_object'] },
  { key: 'project', headerKey: 'gl.dim.project', values: ['project'] },
  { key: 'order', headerKey: 'gl.dim.order', values: ['order'] },
  { key: 'contract', headerKey: 'gl.dim.contract', values: ['contract'] },
  { key: 'expense_item', headerKey: 'gl.dim.expenseItem', values: ['expense_item'] },
  { key: 'item', headerKey: 'gl.dim.item', values: ['item'] },
  { key: 'warehouse', headerKey: 'gl.dim.warehouse', values: ['warehouse'] },
  { key: 'bank_account', headerKey: 'gl.dim.bankAccount', values: ['bank_account'] },
]
