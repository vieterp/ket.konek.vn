/**
 * Dòng của lưới phiếu thu/chi và chứng từ ngân hàng — cặp Nợ/Có + MỘT số tiền
 * nguyên tệ (cách kế toán viên đọc một phiếu, khác lưới GLE mỗi dòng một TK).
 *
 * Giá trị trên lưới là MÃ người dùng gõ, chưa phải id — id chỉ có sau khi tra
 * danh mục lúc lưu (`pair-line-resolve.ts`), cùng triết lý
 * `so-sach-thue/journal-line-types.ts`.
 */

import type { DataGridChange } from '@/design-system/components'

export interface PairLineRow {
  /** Định danh ổn định của DÒNG (không phải id bản ghi) — bắt buộc cho `DataGrid.rowKey`. */
  readonly id: string
  readonly debitCode: string
  readonly creditCode: string
  readonly description: string
  readonly amountFc: string
  /** Mã chiều đã gõ, khóa theo `DimensionColumn.key` (dùng chung config với GLE). */
  readonly dims: Readonly<Record<string, string>>
  /**
   * Chiều MỞ RỘNG của dòng đã lưu (`{dimension_id: value_id}`) — form chưa có
   * ô nhập, nhưng PUT thay trọn bộ nên SỬA phải vọng lại y nguyên, không được
   * wipe lặng lẽ thứ một API client khác đã ghi (review 6F-1 M-A).
   */
  readonly extendedDimensions: Readonly<Record<string, number>> | null
}

export function emptyPairLineRow(): PairLineRow {
  return {
    id: crypto.randomUUID(),
    debitCode: '',
    creditCode: '',
    description: '',
    amountFc: '',
    dims: {},
    extendedDimensions: null,
  }
}

/** Dòng trắng hoàn toàn — không gửi lên server, không chặn lưu. */
export function isPairLineRowEmpty(row: PairLineRow): boolean {
  return (
    row.debitCode.trim() === '' &&
    row.creditCode.trim() === '' &&
    row.description.trim() === '' &&
    row.amountFc.trim() === '' &&
    Object.values(row.dims).every((value) => value.trim() === '')
  )
}

/**
 * Ghi một lượt thay đổi từ `DataGrid` vào mảng dòng — tự nới dòng trắng khi
 * `rowIndex` vượt số dòng hiện có (dán nhiều dòng từ Excel, hợp đồng DataGrid).
 */
export function applyPairLineChanges(
  rows: readonly PairLineRow[],
  changes: readonly DataGridChange[],
  dimensionKeys: readonly string[],
): PairLineRow[] {
  const next = [...rows]
  for (const change of changes) {
    while (next.length <= change.rowIndex) {
      next.push(emptyPairLineRow())
    }
    const current = next[change.rowIndex]
    if (current === undefined) {
      continue
    }
    if (change.columnKey === 'debit_account') {
      next[change.rowIndex] = { ...current, debitCode: change.value }
    } else if (change.columnKey === 'credit_account') {
      next[change.rowIndex] = { ...current, creditCode: change.value }
    } else if (change.columnKey === 'description') {
      next[change.rowIndex] = { ...current, description: change.value }
    } else if (change.columnKey === 'amount_fc') {
      next[change.rowIndex] = { ...current, amountFc: change.value }
    } else if (dimensionKeys.includes(change.columnKey)) {
      next[change.rowIndex] = {
        ...current,
        dims: { ...current.dims, [change.columnKey]: change.value },
      }
    }
  }
  return next
}
