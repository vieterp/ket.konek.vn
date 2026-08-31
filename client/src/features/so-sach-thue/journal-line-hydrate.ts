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
import { DIMENSION_CATALOG_SLUG, DIMENSION_COLUMNS } from './dimension-config'
import type { LineRow } from './journal-line-types'
import type { JournalVoucherOut } from './use-journal-voucher'

type JournalLineOut = JournalVoucherOut['lines'][number]

/**
 * Slug danh mục của một giá trị `detail_tracking` — ĐỌC `DIMENSION_CATALOG_SLUG`,
 * không suy lại bằng `${dimension}s`.
 *
 * Bản suy-lại cũ đúng với chín chiều đầu rồi sai ngay ở chiều thứ mười:
 * `bank_account` → `bank_accounts`, trong khi danh mục tên là
 * `company_bank_accounts`. Mã đã tra được sẽ hiện thành ô TRỐNG trên form SỬA,
 * và PUT thay-trọn-bộ biến ô trống ấy thành mất dữ liệu (cùng họ với 6F-1 C-1).
 */
function slugFor(dimension: string): string {
  return DIMENSION_CATALOG_SLUG[dimension] ?? `${dimension}s`
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
