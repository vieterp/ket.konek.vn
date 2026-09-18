/**
 * Cột lưới "Bán cái gì" trên form hóa đơn bán. Cột tên mặt hàng / tên TK /
 * nguồn giá là cột chỉ-đọc tra từ ô bên cạnh — cùng quy ước lưới cặp Nợ/Có.
 * Cột chiều dùng chung `dimension-config` với GLE.
 */

import type { DataGridColumn, LookupOption } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import type { DimensionColumn } from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import { priceSourceLabel } from './use-price-quote'
import type { SalesLineRow } from './sales-line-types'

export interface SalesLineColumnOptions {
  readonly t: Translate
  readonly accounts: AccountMaps
  readonly items: readonly LookupOption[]
  readonly visibleDimensionColumns: readonly DimensionColumn[]
}

function labelOf(options: readonly LookupOption[], code: string): string {
  const wanted = code.trim().toLowerCase()
  return wanted === '' ? '' : options.find((option) => option.code.toLowerCase() === wanted)?.label ?? ''
}

export function buildSalesLineColumns({
  t,
  accounts,
  items,
  visibleDimensionColumns,
}: SalesLineColumnOptions): DataGridColumn<SalesLineRow>[] {
  const accountName = (code: string): string =>
    accounts.byCode.get(code.trim().toLowerCase())?.name ?? ''
  return [
    { key: 'item', header: t('sales.line.header.item'), width: 110, value: (row) => row.itemCode },
    {
      key: 'itemName',
      header: t('sales.line.header.itemName'),
      readOnly: true,
      width: 180,
      value: (row) => labelOf(items, row.itemCode),
    },
    { key: 'unit', header: t('sales.line.header.unit'), width: 80, value: (row) => row.unitCode },
    {
      key: 'warehouse',
      header: t('sales.line.header.warehouse'),
      width: 100,
      value: (row) => row.warehouseCode,
    },
    {
      key: 'description',
      header: t('sales.line.header.description'),
      width: 180,
      value: (row) => row.description,
    },
    {
      key: 'quantity',
      header: t('sales.line.header.quantity'),
      width: 90,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.quantity,
    },
    {
      key: 'unit_price_fc',
      header: t('sales.line.header.unitPrice'),
      width: 110,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.unitPriceFc,
    },
    {
      // "Số này ở đâu ra" — câu hỏi đầu tiên khi thấy một đơn giá tự điền.
      key: 'priceSource',
      header: t('sales.line.header.priceSource'),
      readOnly: true,
      width: 110,
      value: (row) => priceSourceLabel(t, row),
    },
    {
      key: 'discount_percent',
      header: t('sales.line.header.discountPercent'),
      width: 70,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.discountPercent,
    },
    {
      key: 'discount_amount_fc',
      header: t('sales.line.header.discountAmount'),
      width: 110,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.discountAmountFc,
    },
    {
      key: 'amount_fc',
      header: t('sales.line.header.amount'),
      width: 130,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.amountFc,
    },
    {
      key: 'vat_rate',
      header: t('sales.line.header.vatRate'),
      width: 70,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.vatRate,
    },
    {
      key: 'vat_amount_fc',
      header: t('sales.line.header.vatAmount'),
      width: 120,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.vatAmountFc,
    },
    { key: 'account', header: t('sales.line.header.account'), width: 90, value: (row) => row.accountCode },
    {
      key: 'accountName',
      header: t('sales.line.header.accountName'),
      readOnly: true,
      width: 160,
      value: (row) => accountName(row.accountCode),
    },
    {
      key: 'vat_account',
      header: t('sales.line.header.vatAccount'),
      width: 90,
      value: (row) => row.vatAccountCode,
    },
    ...visibleDimensionColumns.map(
      (column): DataGridColumn<SalesLineRow> => ({
        key: column.key,
        header: t(column.headerKey),
        width: 130,
        value: (row) => row.dims[column.key] ?? '',
      }),
    ),
  ]
}
