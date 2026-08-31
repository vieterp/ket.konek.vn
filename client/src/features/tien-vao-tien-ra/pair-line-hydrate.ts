/**
 * Dựng lại dòng lưới cặp Nợ/Có từ chứng từ ĐÃ LƯU — chiều ngược lại với
 * `pair-line-resolve.ts`: id → mã hiển thị, dùng khi mở form sửa. Cùng khuôn
 * `so-sach-thue/journal-line-hydrate.ts` nhưng dòng có HAI TK và một số tiền.
 */

import type { Schemas } from '@api-types'

import {
  DIMENSION_CATALOG_SLUG,
  DIMENSION_COLUMNS,
} from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'
import type { DimensionLookups } from '@/features/so-sach-thue/use-dimension-lookups'

import type { PairLineRow } from './pair-line-types'

/**
 * Dòng trả về của hai module — dùng chung một hàm dựng.
 *
 * Hợp chứ không phải một: từ lát 6G-1 chỉ dòng phiếu quỹ mang cột chiều
 * `bank_account_id`; chứng từ tiền gửi suy chủ sở hữu 112x từ THÂN chứng từ nên
 * không có (và không được có) ô nhập tay tương ứng.
 */
export type PairLineOut = Schemas['CashVoucherLineOut'] | Schemas['BankVoucherLineOut']

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
