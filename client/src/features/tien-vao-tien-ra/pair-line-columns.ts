/**
 * Cột của lưới cặp Nợ/Có — TK Nợ, TK Có (mỗi bên kèm cột tên chỉ-đọc), diễn
 * giải, MỘT cột số tiền nguyên tệ, và các cột chiều dùng chung config với lưới
 * GLE (`so-sach-thue/dimension-config.ts` — đọc, không sửa file đó).
 */

import type { DataGridColumn } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import type { DimensionColumn } from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import type { PairLineRow } from './pair-line-types'

export function buildPairLineColumns(
  t: Translate,
  accounts: AccountMaps,
  visibleDimensionColumns: readonly DimensionColumn[],
): DataGridColumn<PairLineRow>[] {
  const nameOf = (code: string): string =>
    accounts.byCode.get(code.trim().toLowerCase())?.name ?? ''
  return [
    {
      key: 'debit_account',
      header: t('cashflow.line.header.debitAccount'),
      width: 100,
      value: (row) => row.debitCode,
    },
    {
      key: 'debitAccountName',
      header: t('cashflow.line.header.debitAccountName'),
      readOnly: true,
      value: (row) => nameOf(row.debitCode),
    },
    {
      key: 'credit_account',
      header: t('cashflow.line.header.creditAccount'),
      width: 100,
      value: (row) => row.creditCode,
    },
    {
      key: 'creditAccountName',
      header: t('cashflow.line.header.creditAccountName'),
      readOnly: true,
      value: (row) => nameOf(row.creditCode),
    },
    {
      key: 'description',
      header: t('cashflow.line.header.description'),
      value: (row) => row.description,
    },
    {
      key: 'amount_fc',
      header: t('cashflow.line.header.amount'),
      width: 130,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.amountFc,
    },
    ...visibleDimensionColumns.map(
      (column): DataGridColumn<PairLineRow> => ({
        key: column.key,
        header: t(column.headerKey),
        width: 130,
        value: (row) => row.dims[column.key] ?? '',
      }),
    ),
  ]
}
