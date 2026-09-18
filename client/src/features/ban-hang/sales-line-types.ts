/**
 * Dòng của lưới "Bán cái gì" trên form hóa đơn bán (bộ xương `#mua-form` dùng
 * lại, lát 7H-2a).
 *
 * Cùng hình dạng dòng mua (`mua-hang/purchase-line-types.ts`) — mỗi dòng là
 * MỘT mặt hàng/dịch vụ; TK đối ứng (phải thu khách) nằm trên header — thêm hai
 * cột chiết khấu thương mại và cụm giá: đơn giá / %CK tự điền từ bộ định giá
 * (`use-price-quote.ts`) kèm NGUỒN, còn thành tiền, tiền CK, tiền thuế là ô GÕ
 * (H15: client không nhân chia tiền; server nhận đúng con số kế toán quyết —
 * quyết định user 2026-09-04).
 *
 * `priceTyped` là cờ của luật 6F-1 "không ghi đè thứ người dùng đã gõ": bật
 * khi người dùng sửa đơn giá / %CK; bộ định giá chỉ điền dòng cờ tắt. Đổi mã
 * hàng thì giá cũ vô nghĩa — cụm giá xóa, cờ tắt, dòng hỏi giá lại.
 */

import type { DataGridChange } from '@/design-system/components'

export interface SalesLineRow {
  /** Định danh ổn định của DÒNG cho `DataGrid.rowKey` — không phải id bản ghi. */
  readonly id: string
  readonly itemCode: string
  readonly unitCode: string
  readonly warehouseCode: string
  readonly description: string
  readonly quantity: string
  readonly unitPriceFc: string
  readonly discountPercent: string
  readonly discountAmountFc: string
  readonly amountFc: string
  readonly vatRate: string
  readonly vatAmountFc: string
  readonly accountCode: string
  readonly vatAccountCode: string
  /** Mã chiều đã gõ, khóa theo `DimensionColumn.key`. */
  readonly dims: Readonly<Record<string, string>>
  /** Chiều mở rộng của dòng đã lưu — vọng lại y nguyên khi PUT (cùng lý do 6F-1 M-A). */
  readonly extendedDimensions: Readonly<Record<string, number>> | null
  /** `order_id` đã lưu — chưa có danh mục đơn hàng để tra, vọng lại khi PUT (7H-1 M-5). */
  readonly orderId: number | null
  /** Quy cách đã lưu — lưới chưa có lookup quy cách theo mã hàng, vọng lại khi PUT. */
  readonly variantId: number | null
  /** Ba cột giá vốn phase 8 sẽ tính — lát này chỉ vọng lại. */
  readonly cogsAccountId: number | null
  readonly inventoryAccountId: number | null
  readonly unitCostFc: string | null
  /** Bảng giá + tầng đã trả lời cho dòng — chép từ bộ định giá, không tham gia phép tính nào. */
  readonly priceListId: number | null
  readonly priceSource: string | null
  /** Người dùng đã gõ tay đơn giá / %CK — bộ định giá không được ghi đè. */
  readonly priceTyped: boolean
}

/** Khóa cột lưới → trường trên `SalesLineRow`. Cột chiều đi riêng qua `dims`. */
const COLUMN_FIELD: Readonly<Record<string, keyof SalesLineRow>> = {
  item: 'itemCode',
  unit: 'unitCode',
  warehouse: 'warehouseCode',
  description: 'description',
  quantity: 'quantity',
  unit_price_fc: 'unitPriceFc',
  discount_percent: 'discountPercent',
  discount_amount_fc: 'discountAmountFc',
  amount_fc: 'amountFc',
  vat_rate: 'vatRate',
  vat_amount_fc: 'vatAmountFc',
  account: 'accountCode',
  vat_account: 'vatAccountCode',
}

/** Cột mà một lượt sửa làm câu hỏi giá đổi đáp án. */
const QUOTE_INPUT_COLUMNS: readonly string[] = ['item', 'unit', 'quantity', 'vat_rate']
/** Cột mà người dùng gõ vào là "chốt giá tay" (6F-1). */
const PRICE_TYPED_COLUMNS: readonly string[] = ['unit_price_fc', 'discount_percent']

export function emptySalesLineRow(): SalesLineRow {
  return {
    id: crypto.randomUUID(),
    itemCode: '',
    unitCode: '',
    warehouseCode: '',
    description: '',
    quantity: '',
    unitPriceFc: '',
    discountPercent: '',
    discountAmountFc: '',
    amountFc: '',
    vatRate: '',
    vatAmountFc: '',
    accountCode: '',
    vatAccountCode: '',
    dims: {},
    extendedDimensions: null,
    orderId: null,
    variantId: null,
    cogsAccountId: null,
    inventoryAccountId: null,
    unitCostFc: null,
    priceListId: null,
    priceSource: null,
    priceTyped: false,
  }
}

/** Dòng trắng hoàn toàn — không gửi lên server, không chặn lưu. */
export function isSalesLineRowEmpty(row: SalesLineRow): boolean {
  return (
    Object.values(COLUMN_FIELD).every((field) => {
      const value = row[field]
      return typeof value !== 'string' || value.trim() === ''
    }) && Object.values(row.dims).every((value) => value.trim() === '')
  )
}

export interface SalesLineChangeResult {
  readonly rows: SalesLineRow[]
  /** Chỉ số dòng cần hỏi giá lại — mã hàng/ĐVT/SL/thuế đổi trên dòng chưa chốt giá tay. */
  readonly requote: readonly number[]
}

/** Ghi một lượt thay đổi từ `DataGrid` vào mảng dòng — tự nới dòng trắng khi dán vượt số dòng. */
export function applySalesLineChanges(
  rows: readonly SalesLineRow[],
  changes: readonly DataGridChange[],
  dimensionKeys: readonly string[],
): SalesLineChangeResult {
  const next = [...rows]
  const requote = new Set<number>()
  for (const change of changes) {
    while (next.length <= change.rowIndex) {
      next.push(emptySalesLineRow())
    }
    const current = next[change.rowIndex]
    if (current === undefined) {
      continue
    }
    const field = COLUMN_FIELD[change.columnKey]
    if (field !== undefined) {
      let updated: SalesLineRow = { ...current, [field]: change.value }
      const previousItem = current.itemCode.trim()
      if (change.columnKey === 'item' && previousItem !== '' && change.value.trim() !== previousItem) {
        // ĐỔI mã hàng là câu hỏi giá khác: xóa cụm giá cũ, mở lại cho bộ định
        // giá, và quy cách đã lưu (thuộc mã cũ) không còn nghĩa. Gõ mã hàng vào
        // dòng TRỐNG thì không xóa gì — đơn giá người dùng gõ trước mã hàng là
        // thứ 6F-1 bảo vệ (review 7H-2a M-3/M-5).
        updated = {
          ...updated,
          unitPriceFc: '',
          discountPercent: '',
          priceListId: null,
          priceSource: null,
          priceTyped: false,
          variantId: null,
        }
      } else if (PRICE_TYPED_COLUMNS.includes(change.columnKey)) {
        updated = { ...updated, priceTyped: true, priceSource: null, priceListId: null }
      }
      next[change.rowIndex] = updated
      if (QUOTE_INPUT_COLUMNS.includes(change.columnKey) && !updated.priceTyped) {
        requote.add(change.rowIndex)
      }
    } else if (dimensionKeys.includes(change.columnKey)) {
      next[change.rowIndex] = {
        ...current,
        dims: { ...current.dims, [change.columnKey]: change.value },
      }
    }
  }
  return { rows: next, requote: [...requote] }
}
