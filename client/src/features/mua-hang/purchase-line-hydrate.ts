/**
 * Dựng lại dòng lưới từ hóa đơn mua ĐÃ LƯU — chiều ngược của
 * `purchase-line-resolve.ts`: id → mã hiển thị khi mở form sửa/xem.
 */

import type { Schemas } from '@api-types'

import type { LookupOption } from '@/design-system/components'

import { DIMENSION_CATALOG_SLUG } from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import { PURCHASE_DIMENSION_COLUMNS, type OptionsBySlug } from './purchase-line-resolve'
import type { LandedCostRow, PurchaseLineRow } from './purchase-line-types'

export type PurchaseLineOut = Schemas['PurchaseInvoiceLineOut']
export type LandedCostOut = Schemas['LandedCostOut']

function codeOf(options: readonly LookupOption[] | undefined, id: number | null): string {
  return id === null ? '' : options?.find((option) => option.id === id)?.code ?? ''
}

function accountCode(accounts: AccountMaps, id: number | null): string {
  return id === null ? '' : accounts.byId.get(id)?.code ?? ''
}

export function buildRowFromPurchaseLine(
  line: PurchaseLineOut,
  accounts: AccountMaps,
  options: OptionsBySlug,
  unitOptions: readonly LookupOption[],
): PurchaseLineRow {
  const account = accounts.byId.get(line.account_id)
  const vatAccount = line.vat_account_id === null ? undefined : accounts.byId.get(line.vat_account_id)
  const tracking = new Set([
    ...(account?.detail_tracking ?? []),
    ...(vatAccount?.detail_tracking ?? []),
  ])
  const dims: Record<string, string> = {}
  for (const column of PURCHASE_DIMENSION_COLUMNS) {
    const dimension = column.values.find((value) => tracking.has(value))
    if (dimension === undefined) {
      continue
    }
    const idValue = (line as unknown as Record<string, number | null>)[`${dimension}_id`] ?? null
    const slug = DIMENSION_CATALOG_SLUG[dimension]
    const code = slug === undefined ? '' : codeOf(options[slug], idValue)
    if (code !== '') {
      dims[column.key] = code
    }
  }
  return {
    id: line.id,
    itemCode: codeOf(options['items'], line.item_id),
    unitCode: codeOf(unitOptions, line.unit_id),
    warehouseCode: codeOf(options['warehouses'], line.warehouse_id),
    description: line.description ?? '',
    quantity: line.quantity ?? '',
    unitPriceFc: line.unit_price_fc ?? '',
    amountFc: line.amount_fc,
    vatRate: line.vat_rate ?? '',
    vatAmountFc: line.vat_amount_fc,
    landedCostFc: line.landed_cost_fc,
    accountCode: account?.code ?? '',
    vatAccountCode: vatAccount?.code ?? '',
    dims,
    extendedDimensions: line.extended_dimensions ?? null,
    orderId: line.order_id,
  }
}

export function buildRowFromLandedCost(
  cost: LandedCostOut,
  accounts: AccountMaps,
  vendorOptions: readonly LookupOption[] | undefined,
): LandedCostRow {
  return {
    id: cost.id,
    description: cost.description,
    vendorCode: codeOf(vendorOptions, cost.vendor_id),
    creditAccountCode: accountCode(accounts, cost.credit_account_id),
    amountFc: cost.amount_fc,
    vatRate: cost.vat_rate ?? '',
    vatAmountFc: cost.vat_amount_fc,
    vatAccountCode: accountCode(accounts, cost.vat_account_id),
  }
}
