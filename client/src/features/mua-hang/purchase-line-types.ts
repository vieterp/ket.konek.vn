/**
 * Dòng của lưới "Mua cái gì" trên form hóa đơn mua (design màn 01 `#mua-form`).
 *
 * Khác lưới cặp Nợ/Có của phiếu quỹ: mỗi dòng là MỘT mặt hàng/dịch vụ với
 * số lượng, đơn giá, thành tiền, thuế và TK hạch toán; TK đối ứng (phải trả
 * NCC) nằm trên header. Giá trị là MÃ/chuỗi người dùng gõ — id chỉ có sau khi
 * tra danh mục lúc lưu (`purchase-line-resolve.ts`). Thành tiền và tiền thuế
 * là ô GÕ, không phải ô tính: client không nhân chia tiền (H15), server nhận
 * đúng con số người kế toán quyết.
 */

import type { DataGridChange } from '@/design-system/components'

export interface PurchaseLineRow {
  /** Định danh ổn định của DÒNG cho `DataGrid.rowKey` — không phải id bản ghi. */
  readonly id: string
  readonly itemCode: string
  readonly unitCode: string
  readonly warehouseCode: string
  readonly description: string
  readonly quantity: string
  readonly unitPriceFc: string
  readonly amountFc: string
  readonly vatRate: string
  readonly vatAmountFc: string
  /** Chi phí mua phân bổ vào dòng — gõ tay khi phân bổ thủ công, còn lại server tính. */
  readonly landedCostFc: string
  readonly accountCode: string
  readonly vatAccountCode: string
  /** Mã chiều đã gõ, khóa theo `DimensionColumn.key`. */
  readonly dims: Readonly<Record<string, string>>
  /** Chiều mở rộng của dòng đã lưu — vọng lại y nguyên khi PUT (cùng lý do 6F-1 M-A). */
  readonly extendedDimensions: Readonly<Record<string, number>> | null
  /**
   * `order_id` đã lưu — chiều đơn hàng chưa có danh mục để tra (module Đơn hàng
   * chưa có), nên cột "Đơn hàng" không gõ được; giá trị cũ vẫn phải đi theo dòng
   * khi PUT thay trọn bộ (review 7H-1 M-5).
   */
  readonly orderId: number | null
}

/** Khóa cột lưới → trường trên `PurchaseLineRow`. Cột chiều đi riêng qua `dims`. */
const COLUMN_FIELD: Readonly<Record<string, keyof PurchaseLineRow>> = {
  item: 'itemCode',
  unit: 'unitCode',
  warehouse: 'warehouseCode',
  description: 'description',
  quantity: 'quantity',
  unit_price_fc: 'unitPriceFc',
  amount_fc: 'amountFc',
  vat_rate: 'vatRate',
  vat_amount_fc: 'vatAmountFc',
  landed_cost_fc: 'landedCostFc',
  account: 'accountCode',
  vat_account: 'vatAccountCode',
}

export function emptyPurchaseLineRow(): PurchaseLineRow {
  return {
    id: crypto.randomUUID(),
    itemCode: '',
    unitCode: '',
    warehouseCode: '',
    description: '',
    quantity: '',
    unitPriceFc: '',
    amountFc: '',
    vatRate: '',
    vatAmountFc: '',
    landedCostFc: '',
    accountCode: '',
    vatAccountCode: '',
    dims: {},
    extendedDimensions: null,
    orderId: null,
  }
}

/** Dòng trắng hoàn toàn — không gửi lên server, không chặn lưu. */
export function isPurchaseLineRowEmpty(row: PurchaseLineRow): boolean {
  return (
    Object.values(COLUMN_FIELD).every((field) => {
      const value = row[field]
      return typeof value !== 'string' || value.trim() === ''
    }) && Object.values(row.dims).every((value) => value.trim() === '')
  )
}

/** Ghi một lượt thay đổi từ `DataGrid` vào mảng dòng — tự nới dòng trắng khi dán vượt số dòng. */
export function applyPurchaseLineChanges(
  rows: readonly PurchaseLineRow[],
  changes: readonly DataGridChange[],
  dimensionKeys: readonly string[],
): PurchaseLineRow[] {
  const next = [...rows]
  for (const change of changes) {
    while (next.length <= change.rowIndex) {
      next.push(emptyPurchaseLineRow())
    }
    const current = next[change.rowIndex]
    if (current === undefined) {
      continue
    }
    const field = COLUMN_FIELD[change.columnKey]
    if (field !== undefined) {
      next[change.rowIndex] = { ...current, [field]: change.value }
    } else if (dimensionKeys.includes(change.columnKey)) {
      next[change.rowIndex] = {
        ...current,
        dims: { ...current.dims, [change.columnKey]: change.value },
      }
    }
  }
  return next
}

/** Dòng chi phí mua hàng (vận chuyển, bốc xếp…) — khối dưới lưới, phân bổ vào giá nhập. */
export interface LandedCostRow {
  readonly id: string
  readonly description: string
  /** Mã NCC của khoản chi phí; trống = chi phí của chính NCC trên hóa đơn không ghi nợ riêng. */
  readonly vendorCode: string
  readonly creditAccountCode: string
  readonly amountFc: string
  readonly vatRate: string
  readonly vatAmountFc: string
  readonly vatAccountCode: string
}

const COST_COLUMN_FIELD: Readonly<Record<string, keyof LandedCostRow>> = {
  description: 'description',
  vendor: 'vendorCode',
  credit_account: 'creditAccountCode',
  amount_fc: 'amountFc',
  vat_rate: 'vatRate',
  vat_amount_fc: 'vatAmountFc',
  vat_account: 'vatAccountCode',
}

export function emptyLandedCostRow(): LandedCostRow {
  return {
    id: crypto.randomUUID(),
    description: '',
    vendorCode: '',
    creditAccountCode: '',
    amountFc: '',
    vatRate: '',
    vatAmountFc: '',
    vatAccountCode: '',
  }
}

export function isLandedCostRowEmpty(row: LandedCostRow): boolean {
  return Object.values(COST_COLUMN_FIELD).every((field) => row[field].trim() === '')
}

export function applyLandedCostChanges(
  rows: readonly LandedCostRow[],
  changes: readonly DataGridChange[],
): LandedCostRow[] {
  const next = [...rows]
  for (const change of changes) {
    while (next.length <= change.rowIndex) {
      next.push(emptyLandedCostRow())
    }
    const current = next[change.rowIndex]
    const field = COST_COLUMN_FIELD[change.columnKey]
    if (current !== undefined && field !== undefined) {
      next[change.rowIndex] = { ...current, [field]: change.value }
    }
  }
  return next
}
