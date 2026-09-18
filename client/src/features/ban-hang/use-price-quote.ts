/**
 * Hỏi giá & chiết khấu cho dòng chứng từ bán (FR-SAL §4.2, FR-SYS-042/045) —
 * `POST /api/v1/pricing/quote-batch`.
 *
 * Mọi luật chọn giá chạy ở server; client chỉ chép kết quả lên dòng: đơn giá,
 * %CK, bảng giá, nguồn. Luôn đi đường lô kể cả một dòng — một endpoint, một
 * hình dạng ghép theo VỊ TRÍ (docstring `PriceQuoteBatchRequest`). Thành tiền
 * và tiền CK KHÔNG suy từ kết quả (H15; quyết định user 2026-09-04).
 *
 * `source = none` → đơn giá để TRỐNG chứ không điền `0` của server: một ô trống
 * nói "chưa khai giá, gõ tay" rõ hơn một con số sai đứng cạnh những số đúng.
 */

import type { Schemas } from '@api-types'

import type { LookupOption } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'
import { useSession } from '@/lib/session'

import type { SalesLineRow } from './sales-line-types'

export type PriceQuoteRequest = Schemas['PriceQuoteRequest']
export type PriceQuoteResponse = Schemas['PriceQuoteResponse']

/** Khớp `PriceDirection.SALE` phía server. */
const DIRECTION_SALE = 1
/** Trần `BATCH_QUOTE_MAX_LINES` phía server — lưới 200 dòng vừa khít. */
export const QUOTE_BATCH_MAX_LINES = 200

export interface QuoteContext {
  readonly onDate: string
  readonly customerId: number | null
  /** Bảng giá ÉP chọn ở Mở rộng — bỏ qua phép đi tìm (`kernel.pricing.quote_price`). */
  readonly priceListId: number | null
}

function findOption(options: readonly LookupOption[] | undefined, code: string): LookupOption | undefined {
  const wanted = code.trim().toLowerCase()
  return wanted === '' ? undefined : options?.find((option) => option.code.toLowerCase() === wanted)
}

/**
 * Câu hỏi giá của một dòng; `null` khi dòng chưa đủ dữ kiện (không mã hàng,
 * mã chưa tra được, hoặc đã chốt giá tay). Chưa có số lượng thì hỏi cho 1 đơn
 * vị — bậc chiết khấu theo số lượng sẽ được hỏi lại khi số lượng được gõ.
 */
export function quoteRequestFor(
  row: SalesLineRow,
  context: QuoteContext,
  items: readonly LookupOption[] | undefined,
  units: readonly LookupOption[],
): PriceQuoteRequest | null {
  if (row.priceTyped || context.onDate.trim() === '') {
    return null
  }
  const item = findOption(items, row.itemCode)
  if (item === undefined) {
    return null
  }
  const unit = findOption(units, row.unitCode)
  const quantity = row.quantity.trim()
  const taxRate = row.vatRate.trim()
  return {
    item_id: item.id,
    unit_id: unit?.id ?? null,
    quantity: /[1-9]/.test(quantity) && !quantity.startsWith('-') ? quantity : '1',
    direction: DIRECTION_SALE,
    on_date: context.onDate,
    partner_id: context.customerId,
    contract_id: null,
    price_list_id: context.priceListId,
    level: 1,
    tax_rate: taxRate === '' ? '0' : taxRate,
  }
}

/**
 * Dấu vân của câu hỏi giá một dòng — thứ kết quả về muộn phải so lại trước
 * khi chép (review 7H-2a H-1): gõ mã A, đổi sang B trong lúc chờ, kết quả A về
 * sau B thì không được đè giá của B. Cùng bốn cột `applySalesLineChanges` coi
 * là "đổi câu hỏi".
 */
export function quoteFingerprint(row: SalesLineRow): string {
  return [row.itemCode.trim().toLowerCase(), row.unitCode.trim().toLowerCase(), row.quantity.trim(), row.vatRate.trim()].join('|')
}

/** Chép kết quả định giá lên dòng — chỉ cụm giá, không đụng thành tiền. */
export function applyQuoteToRow(row: SalesLineRow, quote: PriceQuoteResponse): SalesLineRow {
  if (row.priceTyped) {
    return row
  }
  const none = quote.source === 'none'
  return {
    ...row,
    unitPriceFc: none ? '' : quote.unit_price,
    discountPercent: none ? '' : quote.discount_percent,
    priceListId: quote.price_list_id,
    priceSource: quote.source,
  }
}

/**
 * Nhãn cột "Nguồn giá": tầng đã trả lời; không có tầng nào mà dòng đã chốt tay
 * (người dùng gõ đè — `applySalesLineChanges` xóa nguồn) → "gõ tay". Dòng đã
 * lưu mang cả cờ chốt lẫn nguồn cũ, và nguồn cũ là câu trả lời đúng.
 */
export function priceSourceLabel(t: Translate, row: SalesLineRow): string {
  if (row.priceSource === null && row.priceTyped) {
    return t('sales.line.priceSource.typed')
  }
  switch (row.priceSource) {
    case 'price_list':
      return t('sales.line.priceSource.priceList')
    case 'item_level':
      return t('sales.line.priceSource.itemLevel')
    case 'item_default':
      return t('sales.line.priceSource.itemDefault')
    case 'none':
      return t('sales.line.priceSource.none')
    default:
      return ''
  }
}

export function usePriceQuote() {
  const { client, datasetCode } = useSession()
  return {
    quoteBatch: (lines: readonly PriceQuoteRequest[]): Promise<readonly PriceQuoteResponse[]> =>
      client
        .post<Schemas['PriceQuoteBatchResponse']>(
          '/api/v1/pricing/quote-batch',
          { lines: lines.slice(0, QUOTE_BATCH_MAX_LINES) },
          { datasetCode },
        )
        .then((response) => response.items),
  }
}
