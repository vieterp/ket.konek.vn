/**
 * Cột của hai lưới trên form hóa đơn mua: lưới hàng ("Mua cái gì") và lưới chi
 * phí mua hàng. Cột tên mặt hàng / tên TK là cột chỉ-đọc tra từ mã bên cạnh —
 * cùng quy ước lưới cặp Nợ/Có. Cột chiều dùng chung `dimension-config` với GLE.
 */

import type { DataGridColumn, LookupOption } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import type { DimensionColumn } from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import type { LandedCostRow, PurchaseLineRow } from './purchase-line-types'

export interface PurchaseLineColumnOptions {
  readonly t: Translate
  readonly accounts: AccountMaps
  readonly items: readonly LookupOption[]
  readonly visibleDimensionColumns: readonly DimensionColumn[]
  /** Phân bổ thủ công → cột chi phí mua trên dòng mở khóa; còn lại server tính (chỉ-đọc). */
  readonly manualLandedCost: boolean
}

function labelOf(options: readonly LookupOption[], code: string): string {
  const wanted = code.trim().toLowerCase()
  return wanted === '' ? '' : options.find((option) => option.code.toLowerCase() === wanted)?.label ?? ''
}

export function buildPurchaseLineColumns({
  t,
  accounts,
  items,
  visibleDimensionColumns,
  manualLandedCost,
}: PurchaseLineColumnOptions): DataGridColumn<PurchaseLineRow>[] {
  const accountName = (code: string): string =>
    accounts.byCode.get(code.trim().toLowerCase())?.name ?? ''
  return [
    { key: 'item', header: t('purchase.line.header.item'), width: 110, value: (row) => row.itemCode },
    {
      key: 'itemName',
      header: t('purchase.line.header.itemName'),
      readOnly: true,
      width: 180,
      value: (row) => labelOf(items, row.itemCode),
    },
    { key: 'unit', header: t('purchase.line.header.unit'), width: 80, value: (row) => row.unitCode },
    {
      key: 'warehouse',
      header: t('purchase.line.header.warehouse'),
      width: 100,
      value: (row) => row.warehouseCode,
    },
    {
      key: 'description',
      header: t('purchase.line.header.description'),
      width: 180,
      value: (row) => row.description,
    },
    {
      key: 'quantity',
      header: t('purchase.line.header.quantity'),
      width: 90,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.quantity,
    },
    {
      key: 'unit_price_fc',
      header: t('purchase.line.header.unitPrice'),
      width: 110,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.unitPriceFc,
    },
    {
      key: 'amount_fc',
      header: t('purchase.line.header.amount'),
      width: 130,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.amountFc,
    },
    {
      key: 'vat_rate',
      header: t('purchase.line.header.vatRate'),
      width: 70,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.vatRate,
    },
    {
      key: 'vat_amount_fc',
      header: t('purchase.line.header.vatAmount'),
      width: 120,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.vatAmountFc,
    },
    {
      key: 'landed_cost_fc',
      header: t('purchase.line.header.landedCost'),
      width: 120,
      align: 'right',
      inputMode: 'decimal',
      readOnly: !manualLandedCost,
      value: (row) => row.landedCostFc,
    },
    { key: 'account', header: t('purchase.line.header.account'), width: 90, value: (row) => row.accountCode },
    {
      key: 'accountName',
      header: t('purchase.line.header.accountName'),
      readOnly: true,
      width: 160,
      value: (row) => accountName(row.accountCode),
    },
    {
      key: 'vat_account',
      header: t('purchase.line.header.vatAccount'),
      width: 90,
      value: (row) => row.vatAccountCode,
    },
    ...visibleDimensionColumns.map(
      (column): DataGridColumn<PurchaseLineRow> => ({
        key: column.key,
        header: t(column.headerKey),
        width: 130,
        value: (row) => row.dims[column.key] ?? '',
      }),
    ),
  ]
}

export function buildLandedCostColumns(
  t: Translate,
  accounts: AccountMaps,
  vendors: readonly LookupOption[],
): DataGridColumn<LandedCostRow>[] {
  return [
    {
      key: 'description',
      header: t('purchase.cost.header.description'),
      width: 200,
      value: (row) => row.description,
    },
    { key: 'vendor', header: t('purchase.cost.header.vendor'), width: 110, value: (row) => row.vendorCode },
    {
      key: 'vendorName',
      header: t('purchase.cost.header.vendorName'),
      readOnly: true,
      width: 160,
      value: (row) => labelOf(vendors, row.vendorCode),
    },
    {
      key: 'credit_account',
      header: t('purchase.cost.header.creditAccount'),
      width: 90,
      value: (row) => row.creditAccountCode,
    },
    {
      key: 'creditAccountName',
      header: t('purchase.cost.header.creditAccountName'),
      readOnly: true,
      width: 160,
      value: (row) => accounts.byCode.get(row.creditAccountCode.trim().toLowerCase())?.name ?? '',
    },
    {
      key: 'amount_fc',
      header: t('purchase.cost.header.amount'),
      width: 130,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.amountFc,
    },
    {
      key: 'vat_rate',
      header: t('purchase.cost.header.vatRate'),
      width: 70,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.vatRate,
    },
    {
      key: 'vat_amount_fc',
      header: t('purchase.cost.header.vatAmount'),
      width: 120,
      align: 'right',
      inputMode: 'decimal',
      value: (row) => row.vatAmountFc,
    },
    {
      key: 'vat_account',
      header: t('purchase.cost.header.vatAccount'),
      width: 90,
      value: (row) => row.vatAccountCode,
    },
  ]
}
