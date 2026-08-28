/**
 * Rà lưới ngay trên client TRƯỚC khi gửi lên server.
 *
 * Chỉ chặn hai thứ đọc được ngay tại chỗ: TK không tra được (hoặc là TK tổng
 * hợp — BR-SYS-03) và mã chiều gõ vào nhưng không khớp bản ghi nào trong danh
 * mục tương ứng. KHÔNG kiểm cân Nợ/Có, KHÔNG kiểm chiều bắt buộc còn thiếu —
 * hai luật đó thuộc validator ghi sổ phía server (`posting.invalid` mang
 * `violations`), lặp lại ở đây là hai nơi có thể lệch luật với nhau.
 */

import type { Schemas } from '@api-types'

import type { LookupOption } from '@/design-system/components'
import type { Translate } from '@/lib/i18n'

import {
  DIMENSION_CATALOG_SLUG,
  DIMENSION_COLUMNS,
  DIMENSION_LINE_FIELD,
  PARTNER_KIND_BY_DIMENSION,
} from './dimension-config'
import type { LineRow } from './journal-line-types'
import { isLineRowEmpty } from './journal-line-types'

export type Account = Schemas['AccountResponse']
export type JournalLineIn = Schemas['JournalLineIn']

export interface AccountMaps {
  readonly byCode: ReadonlyMap<string, Account>
}

export interface MissingDimensionCode {
  readonly slug: string
  readonly code: string
}

export interface ResolveLinesResult {
  readonly lines: readonly JournalLineIn[]
  readonly errors: readonly string[]
  /**
   * Mã chiều gõ vào nhưng không có trong bản đồ ĐANG CÓ — danh mục lớn hơn
   * trang seed 200 dòng (nợ M-B 6F-1). Form tra server các mã này rồi rà lại;
   * mã sai thật thì lượt rà thứ hai vẫn báo đúng `errors` cũ.
   */
  readonly missing: readonly MissingDimensionCode[]
}

export function resolveLines(
  rows: readonly LineRow[],
  accounts: AccountMaps,
  dimensionOptions: Readonly<Record<string, readonly LookupOption[] | undefined>>,
  t: Translate,
): ResolveLinesResult {
  const lines: JournalLineIn[] = []
  const errors: string[] = []
  const missing: MissingDimensionCode[] = []

  rows.forEach((row, index) => {
    if (isLineRowEmpty(row)) {
      return
    }
    const rowNumber = String(index + 1)
    const code = row.accountCode.trim()
    const account = accounts.byCode.get(code.toLowerCase())
    if (account === undefined) {
      errors.push(t('gl.line.error.accountUnresolved', { row: rowNumber, code }))
      return
    }
    if (account.is_summary) {
      errors.push(t('gl.line.error.accountSummary', { row: rowNumber, code }))
      return
    }

    // Thân dòng dựng thành `Record` vì các trường chiều (partner_id, project_id…)
    // gắn có điều kiện bên dưới; ép kiểu về `JournalLineIn` ở cuối khi đã chắc
    // đủ trường bắt buộc (account_id, debit_fc, credit_fc, extended).
    const line: Record<string, unknown> = {
      account_id: account.id,
      debit_fc: row.debit.trim() === '' ? '0' : row.debit.trim(),
      credit_fc: row.credit.trim() === '' ? '0' : row.credit.trim(),
      description: row.description.trim() === '' ? null : row.description.trim(),
      extended: [],
    }

    let rowFailed = false
    for (const column of DIMENSION_COLUMNS) {
      const dimension = account.detail_tracking?.find((value) => column.values.includes(value))
      if (dimension === undefined) {
        // TK dòng này không đòi chiều nhóm cột này — bỏ qua, kể cả nếu người
        // dùng lỡ gõ gì đó vào ô (không gửi lên, không chặn).
        continue
      }
      const typed = (row.dims[column.key] ?? '').trim()
      if (typed === '') {
        // Bỏ trống dù bắt buộc: để validator ghi sổ phía server chặn kèm số
        // dòng — client không đoán trước "bắt buộc" nghĩa là gì ở mọi TK.
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
          t('gl.line.error.dimensionUnresolved', {
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
      lines.push(line as JournalLineIn)
    }
  })

  return { lines, errors, missing }
}
