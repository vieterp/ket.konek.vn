/**
 * Dựng lại dòng lưới từ hóa đơn bán ĐÃ LƯU — chiều ngược của
 * `sales-line-resolve.ts`: id → mã hiển thị khi mở form sửa/xem. Dòng đã lưu
 * mang `priceTyped = true`: đơn giá trên chứng từ là con số đã chốt, mở form
 * không được hỏi lại bộ định giá rồi ghi đè.
 */

import type { Schemas } from '@api-types'

import type { LookupOption } from '@/design-system/components'

import { DIMENSION_CATALOG_SLUG } from '@/features/so-sach-thue/dimension-config'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import { SALES_DIMENSION_COLUMNS, type OptionsBySlug } from './sales-line-resolve'
import type { SalesLineRow } from './sales-line-types'

export type SalesLineOut = Schemas['SalesInvoiceLineOut']

function codeOf(options: readonly LookupOption[] | undefined, id: number | null): string {
  return id === null ? '' : options?.find((option) => option.id === id)?.code ?? ''
}

export function buildRowFromSalesLine(
  line: SalesLineOut,
  accounts: AccountMaps,
  options: OptionsBySlug,
  unitOptions: readonly LookupOption[],
): SalesLineRow {
  const account = accounts.byId.get(line.account_id)
  const vatAccount = line.vat_account_id === null ? undefined : accounts.byId.get(line.vat_account_id)
  const tracking = new Set([
    ...(account?.detail_tracking ?? []),
    ...(vatAccount?.detail_tracking ?? []),
  ])
  const dims: Record<string, string> = {}
  for (const column of SALES_DIMENSION_COLUMNS) {
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
    discountPercent: line.discount_percent ?? '',
    discountAmountFc: line.discount_amount_fc,
    amountFc: line.amount_fc,
    vatRate: line.vat_rate ?? '',
    vatAmountFc: line.vat_amount_fc,
    accountCode: account?.code ?? '',
    vatAccountCode: vatAccount?.code ?? '',
    dims,
    extendedDimensions: line.extended_dimensions ?? null,
    orderId: line.order_id,
    variantId: line.variant_id,
    cogsAccountId: line.cogs_account_id,
    inventoryAccountId: line.inventory_account_id,
    unitCostFc: line.unit_cost_fc,
    priceListId: line.price_list_id,
    priceSource: line.price_source,
    priceTyped: true,
  }
}
