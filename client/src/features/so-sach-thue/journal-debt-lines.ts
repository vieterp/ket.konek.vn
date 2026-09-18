/**
 * Nhận ra dòng CHẠM CÔNG NỢ trên lưới chứng từ nghiệp vụ khác — đầu vào của
 * khối đối trừ (7H-2b), bản chiếu client của `posting.debt_lines.classify`.
 *
 * Cùng luật với server, đọc từ cùng dữ liệu: TK có `detail_tracking` khai
 * `customer`/`vendor` (đọc cấu hình, không đọc số hiệu 131/331), mã đối tượng
 * gõ ở cột `partner` tra được trong danh mục đối tác, và đúng một bên có số
 * dương. Nhân viên đứng ngoài — tạm ứng chưa có đường đối trừ (docstring
 * `debt_lines.py`). Chiều đối trừ (khoản nợ hay khoản ứng trước) KHÔNG suy ở
 * đây: server suy từ `on_debit` + TK qua chính `classify`, client chỉ hỏi.
 *
 * Hàm thuần, không hook: form gọi mỗi lượt render với dữ liệu lưới hiện có —
 * dòng nào đổi TK/đối tác/bên thì ra khỏi danh sách và khóa đối trừ của nó bị
 * bỏ lúc dựng payload (không gửi mồ côi).
 */

import type { LookupOption } from '@/design-system/components'
import { settlementKey } from '@/features/tien-vao-tien-ra/settlement-section'

import { DIMENSION_COLUMNS, PARTNER_KIND_BY_DIMENSION, trackedDimensionOf } from './dimension-config'
import type { AccountMaps } from './journal-line-resolve'
import type { LineRow } from './journal-line-types'

export interface DebtLineRow {
  /** `LineRow.id` — khóa state của khối đối trừ theo dòng. */
  readonly rowId: string
  /** Số dòng người dùng nhìn thấy trên lưới (1-based, tính cả dòng trắng). */
  readonly rowNumber: number
  readonly accountId: number
  readonly accountCode: string
  /** 0 khách hàng · 1 nhà cung cấp — khớp `PartnerKind` server. */
  readonly partnerKind: 0 | 1
  readonly partnerId: number
  readonly partnerLabel: string
  readonly onDebit: boolean
  /** Số nguyên tệ của bên có số, đúng chuỗi người dùng gõ. */
  readonly amount: string
}

/** Cột nào đổi thì khóa đối trừ của dòng ấy hết nghĩa (đích thuộc về TK + đối tác + bên). */
export const SETTLEMENT_CONTEXT_COLUMNS: ReadonlySet<string> = new Set(['account', 'partner', 'debit', 'credit'])

/** Khóa state của khối đối trừ: `${rowId}|${target_kind}:${target_id}`. */
export const ROW_KEY_SEPARATOR = '|'

export function rowSettlementKey(rowId: string, targetKind: number, targetId: string): string {
  return `${rowId}${ROW_KEY_SEPARATOR}${settlementKey(targetKind, targetId)}`
}

/** Bên thuận tính chất công nợ ⇒ dòng làm nợ lớn lên ⇒ chỉ tất toán được khoản ứng trước. */
export function settlesAdvance(line: DebtLineRow): boolean {
  const naturalSideIsDebit = line.partnerKind === 0
  return line.onDebit === naturalSideIsDebit
}

const PARTNER_COLUMN = DIMENSION_COLUMNS.find((column) => column.key === 'partner')

/**
 * Loại đối tác (0 khách · 1 NCC) mà cột "Mã đối tượng" của dòng này mang —
 * `undefined` khi TK không theo dõi đối tác hoặc theo dõi nhân viên. Cùng phép
 * chọn với `resolveLines` (qua `trackedDimensionOf`).
 */
export function debtPartnerKindOf(detailTracking: readonly string[] | null | undefined): 0 | 1 | undefined {
  if (PARTNER_COLUMN === undefined) {
    return undefined
  }
  const dimension = trackedDimensionOf(detailTracking, PARTNER_COLUMN)
  const kind = dimension === undefined ? undefined : PARTNER_KIND_BY_DIMENSION[dimension]
  return kind === 0 || kind === 1 ? kind : undefined
}

function positiveAmount(value: string): boolean {
  const parsed = Number.parseFloat(value.replace(/,/g, ''))
  return !Number.isNaN(parsed) && parsed > 0
}

export function debtLinesOf(
  rows: readonly LineRow[],
  accounts: AccountMaps,
  partnerOptions: readonly LookupOption[] | undefined,
): DebtLineRow[] {
  const found: DebtLineRow[] = []
  rows.forEach((row, index) => {
    const account = accounts.byCode.get(row.accountCode.trim().toLowerCase())
    if (account === undefined) {
      return
    }
    const partnerKind = debtPartnerKindOf(account.detail_tracking)
    if (partnerKind === undefined) {
      return
    }
    const typed = (row.dims.partner ?? '').trim().toLowerCase()
    if (typed === '') {
      return
    }
    const partner = partnerOptions?.find((option) => option.code.toLowerCase() === typed)
    if (partner === undefined) {
      return
    }
    const debit = positiveAmount(row.debit)
    const credit = positiveAmount(row.credit)
    if (debit === credit) {
      // Không bên nào, hoặc cả hai bên — server từ chối dòng ấy ở cửa, khối
      // đối trừ không có gì để hỏi.
      return
    }
    found.push({
      rowId: row.id,
      rowNumber: index + 1,
      accountId: account.id,
      accountCode: account.code,
      partnerKind,
      partnerId: partner.id,
      partnerLabel: `${partner.code} — ${partner.label}`,
      onDebit: debit,
      amount: debit ? row.debit.trim() : row.credit.trim(),
    })
  })
  return found
}

/**
 * `line_no` server đánh theo `enumerate` các dòng KHÔNG TRẮNG gửi lên, còn số
 * dòng trên lưới tính cả dòng trắng — hai thang khác nhau, đổi ở đúng một chỗ.
 */
export function lineNoOf(rows: readonly LineRow[], rowId: string, isEmpty: (row: LineRow) => boolean): number | null {
  let lineNo = 0
  for (const row of rows) {
    if (isEmpty(row)) {
      continue
    }
    lineNo += 1
    if (row.id === rowId) {
      return lineNo
    }
  }
  return null
}
