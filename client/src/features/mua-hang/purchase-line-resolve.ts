/**
 * Rà hai lưới của form hóa đơn mua trên client TRƯỚC khi gửi — mã → id.
 *
 * Cùng triết lý `tien-vao-tien-ra/pair-line-resolve.ts`: chỉ chặn thứ đọc được
 * tại chỗ (mã không tra được, TK tổng hợp, thiếu thành tiền). Luật nghiệp vụ
 * (thuế đòi hóa đơn NCC — BR-PUR-02, TK phải trả phải theo dõi NCC, tổng phân
 * bổ thủ công phải khớp) để server phán kèm số dòng.
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

import {
  isLandedCostRowEmpty,
  isPurchaseLineRowEmpty,
  type LandedCostRow,
  type PurchaseLineRow,
} from './purchase-line-types'

export type PurchaseLineIn = Schemas['PurchaseInvoiceLineIn']
export type LandedCostIn = Schemas['LandedCostIn']

/**
 * Chiều hiện thành cột riêng trên dòng mua: mặt hàng và kho là hai cột CÓ SẴN
 * của dòng (`item_id`/`warehouse_id`), đối tác nằm trên header (NCC), TK ngân
 * hàng không có nghĩa với hóa đơn mua — nên chỉ năm chiều này mới thành cột.
 */
const PURCHASE_DIMENSION_KEYS: readonly string[] = [
  'cost_object',
  'project',
  'order',
  'contract',
  'expense_item',
]

export const PURCHASE_DIMENSION_COLUMNS: readonly DimensionColumn[] = DIMENSION_COLUMNS.filter(
  (column) => PURCHASE_DIMENSION_KEYS.includes(column.key),
)

export type OptionsBySlug = Readonly<Record<string, readonly LookupOption[] | undefined>>

export interface ResolvePurchaseLinesResult {
  readonly lines: readonly PurchaseLineIn[]
  readonly errors: readonly string[]
  /** Mã danh mục chưa có trong bản đồ đang có — tra bù rồi rà lại (nợ M-B 6F-1). */
  readonly missing: readonly MissingDimensionCode[]
}

export interface ResolveLandedCostsResult {
  readonly costs: readonly LandedCostIn[]
  readonly errors: readonly string[]
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

function makeAccountResolver(
  accounts: AccountMaps,
  errors: string[],
  t: Translate,
): (code: string, rowNumber: string) => AccountResolution {
  return (code, rowNumber) => {
    const trimmed = code.trim()
    if (trimmed === '') {
      return { id: null, failed: false, detailTracking: [] }
    }
    const account = accounts.byCode.get(trimmed.toLowerCase())
    if (account === undefined) {
      errors.push(t('purchase.line.error.accountUnresolved', { row: rowNumber, code: trimmed }))
      return { id: null, failed: true, detailTracking: [] }
    }
    if (account.is_summary) {
      errors.push(t('purchase.line.error.accountSummary', { row: rowNumber, code: trimmed }))
      return { id: null, failed: true, detailTracking: [] }
    }
    return { id: account.id, failed: false, detailTracking: account.detail_tracking ?? [] }
  }
}

export function resolvePurchaseLines(
  rows: readonly PurchaseLineRow[],
  accounts: AccountMaps,
  options: OptionsBySlug,
  unitOptions: readonly LookupOption[],
  manualLandedCost: boolean,
  t: Translate,
): ResolvePurchaseLinesResult {
  const lines: PurchaseLineIn[] = []
  const errors: string[] = []
  const missing: MissingDimensionCode[] = []
  const resolveAccount = makeAccountResolver(accounts, errors, t)

  /** Tra một mã danh mục theo slug; không có → ghi lỗi + xếp vào `missing` để tra bù. */
  function resolveCatalog(
    slug: string,
    code: string,
    rowNumber: string,
    labelKey: 'purchase.line.header.item' | 'purchase.line.header.warehouse',
  ): { readonly id: number | null; readonly failed: boolean } {
    if (code.trim() === '') {
      return { id: null, failed: false }
    }
    const match = findOption(options[slug], code)
    if (match === undefined) {
      missing.push({ slug, code: code.trim() })
      errors.push(
        t('purchase.line.error.catalogUnresolved', {
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
    if (isPurchaseLineRowEmpty(row)) {
      return
    }
    const rowNumber = String(index + 1)
    const account = resolveAccount(row.accountCode, rowNumber)
    const vatAccount = resolveAccount(row.vatAccountCode, rowNumber)
    const item = resolveCatalog('items', row.itemCode, rowNumber, 'purchase.line.header.item')
    const warehouse = resolveCatalog(
      'warehouses',
      row.warehouseCode,
      rowNumber,
      'purchase.line.header.warehouse',
    )
    let rowFailed = account.failed || vatAccount.failed || item.failed || warehouse.failed

    if (account.id === null) {
      errors.push(t('purchase.line.error.accountRequired', { row: rowNumber }))
      rowFailed = true
    }
    if (row.amountFc.trim() === '') {
      errors.push(t('purchase.line.error.amountRequired', { row: rowNumber }))
      rowFailed = true
    }
    let unitId: number | null = null
    if (row.unitCode.trim() !== '') {
      const unit = findOption(unitOptions, row.unitCode)
      if (unit === undefined) {
        missing.push({ slug: 'units_of_measure', code: row.unitCode.trim() })
        errors.push(
          t('purchase.line.error.catalogUnresolved', {
            row: rowNumber,
            label: t('purchase.line.header.unit'),
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
      quantity: optional(row.quantity),
      unit_price_fc: optional(row.unitPriceFc),
      amount_fc: row.amountFc.trim(),
      vat_rate: optional(row.vatRate),
      vat_amount_fc: row.vatAmountFc.trim() === '' ? '0' : row.vatAmountFc.trim(),
      // Không thủ công thì gửi 0: server tự phân bổ và ghi đè; thủ công thì
      // gửi đúng số người dùng gõ, trống = 0.
      landed_cost_fc:
        manualLandedCost && row.landedCostFc.trim() !== '' ? row.landedCostFc.trim() : '0',
      account_id: account.id,
      vat_account_id: vatAccount.id,
      // Chiều đơn hàng không tra được từ ô gõ (không có danh mục) — vọng lại
      // giá trị đã lưu; ô có gõ gì thì lượt rà bên dưới báo lỗi như mọi mã lạ.
      order_id: row.orderId,
      extended: Object.entries(row.extendedDimensions ?? {}).map(([dimensionId, valueId]) => ({
        dimension_id: Number.parseInt(dimensionId, 10),
        value_id: valueId,
      })),
    }

    const tracking = new Set([...account.detailTracking, ...vatAccount.detailTracking])
    for (const column of PURCHASE_DIMENSION_COLUMNS) {
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
          t('purchase.line.error.catalogUnresolved', {
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
      lines.push(line as unknown as PurchaseLineIn)
    }
  })

  return { lines, errors, missing }
}

export function resolveLandedCosts(
  rows: readonly LandedCostRow[],
  accounts: AccountMaps,
  vendorOptions: readonly LookupOption[] | undefined,
  t: Translate,
): ResolveLandedCostsResult {
  const costs: LandedCostIn[] = []
  const errors: string[] = []
  const missing: MissingDimensionCode[] = []
  const resolveAccount = makeAccountResolver(accounts, errors, t)

  rows.forEach((row, index) => {
    if (isLandedCostRowEmpty(row)) {
      return
    }
    const rowNumber = String(index + 1)
    const credit = resolveAccount(row.creditAccountCode, rowNumber)
    const vatAccount = resolveAccount(row.vatAccountCode, rowNumber)
    let rowFailed = credit.failed || vatAccount.failed
    if (row.description.trim() === '') {
      errors.push(t('purchase.cost.error.descriptionRequired', { row: rowNumber }))
      rowFailed = true
    }
    if (credit.id === null) {
      errors.push(t('purchase.cost.error.creditAccountRequired', { row: rowNumber }))
      rowFailed = true
    }
    if (row.amountFc.trim() === '') {
      errors.push(t('purchase.cost.error.amountRequired', { row: rowNumber }))
      rowFailed = true
    }
    let vendorId: number | null = null
    if (row.vendorCode.trim() !== '') {
      const vendor = findOption(vendorOptions, row.vendorCode)
      if (vendor === undefined) {
        missing.push({ slug: 'partners', code: row.vendorCode.trim() })
        errors.push(
          t('purchase.cost.error.vendorUnresolved', { row: rowNumber, code: row.vendorCode.trim() }),
        )
        rowFailed = true
      } else {
        vendorId = vendor.id
      }
    }
    if (!rowFailed) {
      costs.push({
        description: row.description.trim(),
        vendor_id: vendorId,
        credit_account_id: credit.id ?? 0,
        amount_fc: row.amountFc.trim(),
        vat_rate: optional(row.vatRate),
        vat_amount_fc: row.vatAmountFc.trim() === '' ? '0' : row.vatAmountFc.trim(),
        vat_account_id: vatAccount.id,
      })
    }
  })

  return { costs, errors, missing }
}
