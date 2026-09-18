/**
 * Rà lưới của form hóa đơn bán trên client TRƯỚC khi gửi — mã → id.
 *
 * Cùng triết lý `mua-hang/purchase-line-resolve.ts`: chỉ chặn thứ đọc được
 * tại chỗ (mã không tra được, TK tổng hợp, thiếu thành tiền). Luật nghiệp vụ
 * (TK phải thu phải theo dõi khách, quy cách thuộc đúng mã hàng, thuế đòi TK
 * thuế) để server phán kèm số dòng.
 */

import type { Schemas } from '@api-types'

import type { LookupOption } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import {
  DIMENSION_CATALOG_SLUG,
  DIMENSION_COLUMNS,
  DIMENSION_LINE_FIELD,
  type DimensionColumn,
} from '@/features/so-sach-thue/dimension-config'
import type { MissingDimensionCode } from '@/features/so-sach-thue/journal-line-resolve'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import { isSalesLineRowEmpty, type SalesLineRow } from './sales-line-types'

export type SalesLineIn = Schemas['SalesInvoiceLineIn']

/**
 * Chiều hiện thành cột riêng trên dòng bán: mặt hàng và kho là hai cột CÓ SẴN
 * của dòng, đối tác nằm trên header (khách hàng), TK ngân hàng không có nghĩa
 * với hóa đơn bán — nên chỉ năm chiều này mới thành cột (cùng lưới mua).
 */
const SALES_DIMENSION_KEYS: readonly string[] = [
  'cost_object',
  'project',
  'order',
  'contract',
  'expense_item',
]

export const SALES_DIMENSION_COLUMNS: readonly DimensionColumn[] = DIMENSION_COLUMNS.filter(
  (column) => SALES_DIMENSION_KEYS.includes(column.key),
)

export type OptionsBySlug = Readonly<Record<string, readonly LookupOption[] | undefined>>

export interface ResolveSalesLinesResult {
  readonly lines: readonly SalesLineIn[]
  readonly errors: readonly string[]
  /** Mã danh mục chưa có trong bản đồ đang có — tra bù rồi rà lại (nợ M-B 6F-1). */
  readonly missing: readonly MissingDimensionCode[]
}

function findOption(
  options: readonly LookupOption[] | undefined,
  code: string,
): LookupOption | undefined {
  const wanted = code.trim().toLowerCase()
  return options?.find((option) => option.code.toLowerCase() === wanted)
}

function optional(value: string): string | null {
  return value.trim() === '' ? null : value.trim()
}

interface AccountResolution {
  readonly id: number | null
  readonly failed: boolean
  readonly detailTracking: readonly string[]
}

export function resolveSalesLines(
  rows: readonly SalesLineRow[],
  accounts: AccountMaps,
  options: OptionsBySlug,
  unitOptions: readonly LookupOption[],
  t: Translate,
): ResolveSalesLinesResult {
  const lines: SalesLineIn[] = []
  const errors: string[] = []
  const missing: MissingDimensionCode[] = []

  function resolveAccount(code: string, rowNumber: string): AccountResolution {
    const trimmed = code.trim()
    if (trimmed === '') {
      return { id: null, failed: false, detailTracking: [] }
    }
    const account = accounts.byCode.get(trimmed.toLowerCase())
    if (account === undefined) {
      errors.push(t('sales.line.error.accountUnresolved', { row: rowNumber, code: trimmed }))
      return { id: null, failed: true, detailTracking: [] }
    }
    if (account.is_summary) {
      errors.push(t('sales.line.error.accountSummary', { row: rowNumber, code: trimmed }))
      return { id: null, failed: true, detailTracking: [] }
    }
    return { id: account.id, failed: false, detailTracking: account.detail_tracking ?? [] }
  }

  /** Tra một mã danh mục theo slug; không có → ghi lỗi + xếp vào `missing` để tra bù. */
  function resolveCatalog(
    slug: string,
    code: string,
    rowNumber: string,
    labelKey: 'sales.line.header.item' | 'sales.line.header.warehouse',
  ): { readonly id: number | null; readonly failed: boolean } {
    if (code.trim() === '') {
      return { id: null, failed: false }
    }
    const match = findOption(options[slug], code)
    if (match === undefined) {
      missing.push({ slug, code: code.trim() })
      errors.push(
        t('sales.line.error.catalogUnresolved', {
          row: rowNumber,
          label: t(labelKey),
          code: code.trim(),
        }),
      )
      return { id: null, failed: true }
    }
    return { id: match.id, failed: false }
  }

  rows.forEach((row, index) => {
    if (isSalesLineRowEmpty(row)) {
      return
    }
    const rowNumber = String(index + 1)
    const account = resolveAccount(row.accountCode, rowNumber)
    const vatAccount = resolveAccount(row.vatAccountCode, rowNumber)
    const item = resolveCatalog('items', row.itemCode, rowNumber, 'sales.line.header.item')
    const warehouse = resolveCatalog(
      'warehouses',
      row.warehouseCode,
      rowNumber,
      'sales.line.header.warehouse',
    )
    let rowFailed = account.failed || vatAccount.failed || item.failed || warehouse.failed

    if (account.id === null) {
      errors.push(t('sales.line.error.accountRequired', { row: rowNumber }))
      rowFailed = true
    }
    if (row.amountFc.trim() === '') {
      errors.push(t('sales.line.error.amountRequired', { row: rowNumber }))
      rowFailed = true
    }
    let unitId: number | null = null
    if (row.unitCode.trim() !== '') {
      const unit = findOption(unitOptions, row.unitCode)
      if (unit === undefined) {
        missing.push({ slug: 'units_of_measure', code: row.unitCode.trim() })
        errors.push(
          t('sales.line.error.catalogUnresolved', {
            row: rowNumber,
            label: t('sales.line.header.unit'),
            code: row.unitCode.trim(),
          }),
        )
        rowFailed = true
      } else {
        unitId = unit.id
      }
    }

    const line: Record<string, unknown> = {
      description: optional(row.description),
      item_id: item.id,
      unit_id: unitId,
      warehouse_id: warehouse.id,
      // Quy cách chỉ có nghĩa khi còn cùng mã hàng — đổi mã thì quy cách cũ vô nghĩa.
      variant_id: item.id === null ? null : row.variantId,
      quantity: optional(row.quantity),
      unit_price_fc: optional(row.unitPriceFc),
      discount_percent: optional(row.discountPercent),
      discount_amount_fc: row.discountAmountFc.trim() === '' ? '0' : row.discountAmountFc.trim(),
      amount_fc: row.amountFc.trim(),
      vat_rate: optional(row.vatRate),
      vat_amount_fc: row.vatAmountFc.trim() === '' ? '0' : row.vatAmountFc.trim(),
      account_id: account.id,
      vat_account_id: vatAccount.id,
      cogs_account_id: row.cogsAccountId,
      inventory_account_id: row.inventoryAccountId,
      unit_cost_fc: row.unitCostFc,
      // Cụm nguồn giá chép nguyên: bộ định giá điền, người dùng gõ đè thì
      // `applySalesLineChanges` đã xóa về `null` (= người lập gõ); dòng đã lưu
      // vọng lại đúng nguồn cũ khi PUT.
      price_list_id: row.priceListId,
      price_source: row.priceSource,
      // Chiều đơn hàng không tra được từ ô gõ (không có danh mục) — vọng lại
      // giá trị đã lưu; ô có gõ gì thì lượt rà bên dưới báo lỗi như mọi mã lạ.
      order_id: row.orderId,
      extended: Object.entries(row.extendedDimensions ?? {}).map(([dimensionId, valueId]) => ({
        dimension_id: Number.parseInt(dimensionId, 10),
        value_id: valueId,
      })),
    }

    const tracking = new Set([...account.detailTracking, ...vatAccount.detailTracking])
    for (const column of SALES_DIMENSION_COLUMNS) {
      const dimension = column.values.find((value) => tracking.has(value))
      if (dimension === undefined) {
        continue
      }
      const typed = (row.dims[column.key] ?? '').trim()
      if (typed === '') {
        continue
      }
      const slug = DIMENSION_CATALOG_SLUG[dimension]
      const match = slug === undefined ? undefined : findOption(options[slug], typed)
      if (match === undefined) {
        if (slug !== undefined) {
          missing.push({ slug, code: typed })
        }
        errors.push(
          t('sales.line.error.catalogUnresolved', {
            row: rowNumber,
            label: t(column.headerKey),
            code: typed,
          }),
        )
        rowFailed = true
        continue
      }
      const field = DIMENSION_LINE_FIELD[dimension]
      if (field !== undefined) {
        line[field] = match.id
      }
    }

    if (!rowFailed) {
      lines.push(line as unknown as SalesLineIn)
    }
  })

  return { lines, errors, missing }
}
