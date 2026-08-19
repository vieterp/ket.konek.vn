/**
 * Dòng của lưới nhập chứng từ — hình chiếu client, tách khỏi `JournalLineIn`
 * phía server: cột "TK" và các cột chiều trên lưới là MÃ người dùng gõ, chưa
 * phải id (id chỉ có sau khi tra danh mục lúc lưu — `journal-line-resolve.ts`).
 */

import type { DataGridChange } from '@/design-system/components'

export interface LineRow {
  /** Định danh ổn định của DÒNG (không phải id bản ghi) — bắt buộc cho `DataGrid.rowKey`. */
  readonly id: string
  readonly accountCode: string
  readonly description: string
  readonly debit: string
  readonly credit: string
  /** Mã chiều đã gõ, khóa theo `DimensionColumn.key`. */
  readonly dims: Readonly<Record<string, string>>
}

export function emptyLineRow(): LineRow {
  return {
    id: crypto.randomUUID(),
    accountCode: '',
    description: '',
    debit: '',
    credit: '',
    dims: {},
  }
}

/** Dòng trắng hoàn toàn — không gửi lên server, không chặn lưu. */
export function isLineRowEmpty(row: LineRow): boolean {
  return (
    row.accountCode.trim() === '' &&
    row.description.trim() === '' &&
    row.debit.trim() === '' &&
    row.credit.trim() === '' &&
    Object.values(row.dims).every((value) => value.trim() === '')
  )
}

/**
 * Ghi một lượt thay đổi từ `DataGrid` vào mảng dòng.
 *
 * `change.rowIndex` được phép vượt số dòng hiện có (dán nhiều dòng từ Excel —
 * hợp đồng của `DataGrid`), nên hàm này tự nới thêm dòng trắng cho tới khi đủ
 * chỗ, đúng khuôn `data-grid-bench-page.tsx`.
 */
export function applyLineChanges(
  rows: readonly LineRow[],
  changes: readonly DataGridChange[],
  dimensionKeys: readonly string[],
): LineRow[] {
  const next = [...rows]
  for (const change of changes) {
    while (next.length <= change.rowIndex) {
      next.push(emptyLineRow())
    }
    const current = next[change.rowIndex]
    if (current === undefined) {
      continue
    }
    if (change.columnKey === 'account') {
      next[change.rowIndex] = { ...current, accountCode: change.value }
    } else if (change.columnKey === 'description') {
      next[change.rowIndex] = { ...current, description: change.value }
    } else if (change.columnKey === 'debit') {
      next[change.rowIndex] = { ...current, debit: change.value }
    } else if (change.columnKey === 'credit') {
      next[change.rowIndex] = { ...current, credit: change.value }
    } else if (dimensionKeys.includes(change.columnKey)) {
      next[change.rowIndex] = {
        ...current,
        dims: { ...current.dims, [change.columnKey]: change.value },
      }
    }
  }
  return next
}
