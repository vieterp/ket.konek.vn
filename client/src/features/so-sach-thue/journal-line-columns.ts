/**
 * Cột của lưới chi tiết chứng từ — tách khỏi `journal-voucher-form.tsx` vì đây
 * là phần duy nhất phải biết cả hình dạng `DataGridColumn` lẫn hai nguồn dữ
 * liệu ngoài (bản đồ TK, danh sách cột chiều đang hiện).
 */

import type { DataGridColumn } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import type { DimensionColumn } from './dimension-config'
import type { LineRow } from './journal-line-types'
import type { AccountMaps } from './use-account-lookup'

export function buildLineColumns(
  t: Translate,
  accounts: AccountMaps,
  visibleDimensionColumns: readonly DimensionColumn[],
): DataGridColumn<LineRow>[] {
  return [
    { key: 'account', header: t('gl.line.header.account'), width: 100, value: (row) => row.accountCode },
    {
      key: 'accountName',
      header: t('gl.line.header.accountName'),
      readOnly: true,
      value: (row) => accounts.byCode.get(row.accountCode.trim().toLowerCase())?.name ?? '',
    },
    { key: 'description', header: t('gl.line.header.description'), value: (row) => row.description },
    {
      key: 'debit',
      header: t('gl.line.header.debit'),
      width: 130,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.debit,
    },
    {
      key: 'credit',
      header: t('gl.line.header.credit'),
      width: 130,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.credit,
    },
    ...visibleDimensionColumns.map(
      (column): DataGridColumn<LineRow> => ({
        key: column.key,
        header: t(column.headerKey),
        width: 130,
        value: (row) => row.dims[column.key] ?? '',
      }),
    ),
  ]
}
