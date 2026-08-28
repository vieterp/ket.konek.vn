/**
 * Rà lưới cặp Nợ/Có ngay trên client TRƯỚC khi gửi lên server.
 *
 * Cùng triết lý `so-sach-thue/journal-line-resolve.ts`: chỉ chặn thứ đọc được
 * ngay tại chỗ — mã TK/mã chiều không tra được, TK tổng hợp, thiếu số tiền.
 * KHÔNG lặp lại luật ghi sổ phía server (thiếu một bên được phép ở bản nháp —
 * chính `posting_mapper` phía server từ chối lúc ghi sổ, kèm số dòng).
 */

import type { Schemas } from '@api-types'

import type { LookupOption } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import {
  DIMENSION_CATALOG_SLUG,
  DIMENSION_COLUMNS,
  DIMENSION_LINE_FIELD,
  PARTNER_KIND_BY_DIMENSION,
} from '@/features/so-sach-thue/dimension-config'
import type { MissingDimensionCode } from '@/features/so-sach-thue/journal-line-resolve'
import type { AccountMaps } from '@/features/so-sach-thue/use-account-lookup'

import type { PairLineRow } from './pair-line-types'
import { isPairLineRowEmpty } from './pair-line-types'

/** Hai module cùng một hình dạng dòng — dùng chung một hàm rà. */
export type PairLineIn = Schemas['CashVoucherLineIn']

export interface ResolvePairLinesResult {
  readonly lines: readonly PairLineIn[]
  readonly errors: readonly string[]
  /**
   * Mã chiều gõ vào nhưng không có trong bản đồ ĐANG CÓ — danh mục lớn hơn
   * trang seed 200 dòng (nợ M-B 6F-1). Form tra server các mã này rồi rà lại;
   * mã sai thật thì lượt rà thứ hai vẫn báo đúng `errors` cũ.
   */
  readonly missing: readonly MissingDimensionCode[]
}

interface SideResolution {
  readonly id: number | null
  readonly failed: boolean
  readonly detailTracking: readonly string[]
}

export function resolvePairLines(
  rows: readonly PairLineRow[],
  accounts: AccountMaps,
  dimensionOptions: Readonly<Record<string, readonly LookupOption[] | undefined>>,
  t: Translate,
): ResolvePairLinesResult {
  const lines: PairLineIn[] = []
  const errors: string[] = []
  const missing: MissingDimensionCode[] = []

  function resolveSide(code: string, rowNumber: string): SideResolution {
    const trimmed = code.trim()
    if (trimmed === '') {
      return { id: null, failed: false, detailTracking: [] }
    }
    const account = accounts.byCode.get(trimmed.toLowerCase())
    if (account === undefined) {
      errors.push(t('cashflow.line.error.accountUnresolved', { row: rowNumber, code: trimmed }))
      return { id: null, failed: true, detailTracking: [] }
    }
    if (account.is_summary) {
      errors.push(t('cashflow.line.error.accountSummary', { row: rowNumber, code: trimmed }))
      return { id: null, failed: true, detailTracking: [] }
    }
    return { id: account.id, failed: false, detailTracking: account.detail_tracking ?? [] }
  }

  rows.forEach((row, index) => {
    if (isPairLineRowEmpty(row)) {
      return
    }
    const rowNumber = String(index + 1)
    const debit = resolveSide(row.debitCode, rowNumber)
    const credit = resolveSide(row.creditCode, rowNumber)
    let rowFailed = debit.failed || credit.failed

    if (row.amountFc.trim() === '') {
      errors.push(t('cashflow.line.error.amountRequired', { row: rowNumber }))
      rowFailed = true
    }

    const line: Record<string, unknown> = {
      debit_account_id: debit.id,
      credit_account_id: credit.id,
      amount_fc: row.amountFc.trim(),
      description: row.description.trim() === '' ? null : row.description.trim(),
      // Vọng lại chiều mở rộng đã lưu — form chưa có ô nhập nhưng PUT thay
      // trọn bộ (review 6F-1 M-A). Server nhận `{dimension_id, value_id}`.
      extended: Object.entries(row.extendedDimensions ?? {}).map(([dimensionId, valueId]) => ({
        dimension_id: Number.parseInt(dimensionId, 10),
        value_id: valueId,
      })),
    }

    // Chiều của dòng = HỢP của hai bên: chiều nào một trong hai TK khai thì cột
    // đó có nghĩa với dòng này.
    const tracking = new Set([...debit.detailTracking, ...credit.detailTracking])
    for (const column of DIMENSION_COLUMNS) {
      const dimension = column.values.find((value) => tracking.has(value))
      if (dimension === undefined) {
        continue
      }
      const typed = (row.dims[column.key] ?? '').trim()
      if (typed === '') {
        // Bỏ trống dù bắt buộc: để validator ghi sổ phía server chặn kèm số
        // dòng — client không đoán "bắt buộc" nghĩa là gì ở mọi TK.
        continue
      }
      const slug = DIMENSION_CATALOG_SLUG[dimension]
      const options = slug === undefined ? undefined : dimensionOptions[slug]
      const match = options?.find((option) => option.code.toLowerCase() === typed.toLowerCase())
      if (match === undefined) {
        // `order` không có slug (danh mục thuộc phase 7) — không có gì để tra
        // bù, lỗi đứng nguyên.
        if (slug !== undefined) {
          missing.push({ slug, code: typed })
        }
        errors.push(
          t('cashflow.line.error.dimensionUnresolved', {
            row: rowNumber,
            label: t(column.headerKey),
            code: typed,
          }),
        )
        rowFailed = true
        continue
      }
      const partnerKind = PARTNER_KIND_BY_DIMENSION[dimension]
      if (partnerKind !== undefined) {
        line.partner_id = match.id
        line.partner_kind = partnerKind
        continue
      }
      const field = DIMENSION_LINE_FIELD[dimension]
      if (field !== undefined) {
        line[field] = match.id
      }
    }

    if (!rowFailed) {
      lines.push(line as unknown as PairLineIn)
    }
  })

  return { lines, errors, missing }
}
