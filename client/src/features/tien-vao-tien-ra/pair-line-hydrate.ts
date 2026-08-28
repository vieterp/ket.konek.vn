/**
 * Dựng lại dòng lưới cặp Nợ/Có từ chứng từ ĐÃ LƯU — chiều ngược lại với
 * `pair-line-resolve.ts`: id → mã hiển thị, dùng khi mở form sửa. Cùng khuôn
 * `so-sach-thue/journal-line-hydrate.ts` nhưng dòng có HAI TK và một số tiền.
 */

import type { Schemas } from '@api-types'

import { DIMENSION_COLUMNS } from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'
import type { DimensionLookups } from '@/features/so-sach-thue/use-dimension-lookups'

import type { PairLineRow } from './pair-line-types'

/** Hai module cùng hình dạng dòng trả về — dùng chung một hàm dựng. */
export type PairLineOut = Schemas['CashVoucherLineOut']

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

function idFor(line: PairLineOut, dimension: string): number | null {
  if (dimension === 'customer' || dimension === 'vendor' || dimension === 'employee') {
    const wantedKind = dimension === 'customer' ? 0 : dimension === 'vendor' ? 1 : 2
    return line.partner_kind === wantedKind ? line.partner_id : null
  }
  return (line as unknown as Record<string, number | null>)[`${dimension}_id`] ?? null
}

export function buildRowFromPairLine(
  line: PairLineOut,
  accounts: AccountMaps,
  dimensionOptions: DimensionLookups['options'],
): PairLineRow {
  const debit = line.debit_account_id === null ? undefined : accounts.byId.get(line.debit_account_id)
  const credit =
    line.credit_account_id === null ? undefined : accounts.byId.get(line.credit_account_id)
  const tracking = new Set([
    ...(debit?.detail_tracking ?? []),
    ...(credit?.detail_tracking ?? []),
  ])
  const dims: Record<string, string> = {}
  for (const column of DIMENSION_COLUMNS) {
    const dimension = column.values.find((value) => tracking.has(value))
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
    debitCode: debit?.code ?? '',
    creditCode: credit?.code ?? '',
    description: line.description ?? '',
    amountFc: line.amount_fc,
    dims,
    extendedDimensions: line.extended_dimensions ?? null,
  }
}
