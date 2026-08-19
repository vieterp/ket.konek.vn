/**
 * Dựng lại dòng lưới từ một chứng từ ĐÃ LƯU (`JournalLineOut`) — chiều ngược
 * lại với `journal-line-resolve.ts`: id → mã hiển thị, dùng khi mở form sửa.
 *
 * `JournalLineOut` chỉ mang id (không mang mã), nên cần cả bản đồ TK
 * (`id → AccountResponse`, biết TK này khai chiều gì) lẫn danh mục chiều
 * (`id → mã`) mới dựng lại được ô người dùng từng gõ.
 */

import type { AccountMaps } from './use-account-lookup'
import type { DimensionLookups } from './use-dimension-lookups'
import { DIMENSION_COLUMNS } from './dimension-config'
import type { LineRow } from './journal-line-types'
import type { JournalVoucherOut } from './use-journal-voucher'

type JournalLineOut = JournalVoucherOut['lines'][number]

/** Slug danh mục ứng với một giá trị `detail_tracking` — mặt ngược của `DIMENSION_CATALOG_SLUG`. */
function slugFor(dimension: string): string {
  if (dimension === 'customer' || dimension === 'vendor') {
    return 'partners'
  }
  if (dimension === 'employee') {
    return 'employees'
  }
  return `${dimension}s`
}

function idFor(line: JournalLineOut, dimension: string): number | null {
  if (dimension === 'customer' || dimension === 'vendor' || dimension === 'employee') {
    const wantedKind = dimension === 'customer' ? 0 : dimension === 'vendor' ? 1 : 2
    return line.partner_kind === wantedKind ? line.partner_id : null
  }
  return (line as unknown as Record<string, number | null>)[`${dimension}_id`] ?? null
}

export function buildRowFromLine(
  line: JournalLineOut,
  accounts: AccountMaps,
  dimensionOptions: DimensionLookups['options'],
): LineRow {
  const account = accounts.byId.get(line.account_id)
  const dims: Record<string, string> = {}
  for (const column of DIMENSION_COLUMNS) {
    const dimension = account?.detail_tracking?.find((value) => column.values.includes(value))
    if (dimension === undefined) {
      continue
    }
    const idValue = idFor(line, dimension)
    if (idValue === null) {
      continue
    }
    const code = dimensionOptions[slugFor(dimension)]?.find((option) => option.id === idValue)?.code
    if (code !== undefined) {
      dims[column.key] = code
    }
  }
  return {
    id: line.id,
    accountCode: account?.code ?? '',
    description: line.description ?? '',
    debit: line.debit_fc,
    credit: line.credit_fc,
    dims,
  }
}
