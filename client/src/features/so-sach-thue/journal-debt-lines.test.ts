/**
 * Luật nhận ra dòng chạm công nợ trên lưới GLE (bản chiếu client của
 * `posting.debt_lines.classify`) và phép đổi thang số dòng.
 */

import { describe, expect, it } from 'vitest'

import type { LookupOption } from '@/design-system/components'

import type { Account, AccountMaps } from './journal-line-resolve'
import type { LineRow } from './journal-line-types'
import { emptyLineRow, isLineRowEmpty } from './journal-line-types'
import { debtLinesOf, lineNoOf, rowSettlementKey, settlesAdvance } from './journal-debt-lines'

const RECEIVABLE: Account = {
  id: 131,
  code: '131',
  name: 'Phải thu khách hàng',
  name_en: null,
  balance_nature: 1,
  detail_tracking: ['customer'],
  is_summary: false,
  is_foreign_currency: false,
  level: 1,
  parent_id: null,
}
const PAYABLE: Account = { ...RECEIVABLE, id: 331, code: '331', detail_tracking: ['vendor'] }
const ADVANCE: Account = { ...RECEIVABLE, id: 141, code: '141', detail_tracking: ['employee'] }
// TK khai HAI loại đối tượng: cột "Mã đối tượng" theo giá trị ĐỨNG TRƯỚC (cùng
// phép chọn với `resolveLines`) — nhân viên trước thì dòng không phải dòng công nợ.
const MIXED: Account = { ...RECEIVABLE, id: 138, code: '138', detail_tracking: ['employee', 'customer'] }
const CASH: Account = { ...RECEIVABLE, id: 111, code: '111', detail_tracking: null }

const ACCOUNTS: AccountMaps = {
  byCode: new Map([
    ['131', RECEIVABLE],
    ['331', PAYABLE],
    ['141', ADVANCE],
    ['138', MIXED],
    ['111', CASH],
  ]),
}
const PARTNERS: readonly LookupOption[] = [
  { id: 5, code: 'KH01', label: 'Khách A' },
  { id: 9, code: 'NCC01', label: 'Nhà cung cấp B' },
]

function row(overrides: Partial<LineRow>): LineRow {
  return { ...emptyLineRow(), ...overrides }
}

describe('debtLinesOf', () => {
  it('nhận ra bốn ca của luật hai trục và đọc đúng bên/đối tác', () => {
    const rows = [
      row({ id: 'a', accountCode: '131', credit: '100', dims: { partner: 'kh01' } }),
      row({ id: 'b', accountCode: '131', debit: '50', dims: { partner: 'KH01' } }),
      row({ id: 'c', accountCode: '331', debit: '70', dims: { partner: 'NCC01' } }),
      row({ id: 'd', accountCode: '331', credit: '20', dims: { partner: 'NCC01' } }),
    ]
    const found = debtLinesOf(rows, ACCOUNTS, PARTNERS)
    expect(found.map((line) => [line.rowId, line.partnerKind, line.partnerId, line.onDebit, line.amount])).toEqual([
      ['a', 0, 5, false, '100'],
      ['b', 0, 5, true, '50'],
      ['c', 1, 9, true, '70'],
      ['d', 1, 9, false, '20'],
    ])
    // Bên NGƯỢC tất toán khoản nợ, bên THUẬN bù khoản ứng trước.
    expect(found.map(settlesAdvance)).toEqual([false, true, false, true])
    expect(found[0]?.rowNumber).toBe(1)
    expect(found[0]?.partnerLabel).toBe('KH01 — Khách A')
  })

  it('bỏ qua: nhân viên, TK không theo dõi đối tác, mã chưa tra được, không bên hoặc hai bên', () => {
    const rows = [
      row({ accountCode: '141', debit: '10', dims: { partner: 'KH01' } }),
      row({ accountCode: '111', debit: '10', dims: { partner: 'KH01' } }),
      row({ accountCode: '131', debit: '10', dims: { partner: 'KH99' } }),
      row({ accountCode: '131', debit: '10' }),
      row({ accountCode: '131', dims: { partner: 'KH01' } }),
      row({ accountCode: '131', debit: '10', credit: '10', dims: { partner: 'KH01' } }),
      row({ accountCode: '999', debit: '10', dims: { partner: 'KH01' } }),
      row({ accountCode: '138', debit: '10', dims: { partner: 'KH01' } }),
    ]
    expect(debtLinesOf(rows, ACCOUNTS, PARTNERS)).toEqual([])
    expect(debtLinesOf(rows, ACCOUNTS, undefined)).toEqual([])
  })
})

describe('lineNoOf', () => {
  it('đánh số theo dòng KHÔNG TRẮNG, đúng thang enumerate của server', () => {
    const rows = [
      row({ id: 'blank1' }),
      row({ id: 'a', accountCode: '111', debit: '1' }),
      row({ id: 'blank2' }),
      row({ id: 'b', accountCode: '131', credit: '1', dims: { partner: 'KH01' } }),
    ]
    expect(lineNoOf(rows, 'a', isLineRowEmpty)).toBe(1)
    expect(lineNoOf(rows, 'b', isLineRowEmpty)).toBe(2)
    expect(lineNoOf(rows, 'blank2', isLineRowEmpty)).toBeNull()
    expect(lineNoOf(rows, 'missing', isLineRowEmpty)).toBeNull()
  })
})

describe('rowSettlementKey', () => {
  it('ghép id dòng với khóa đích của bảng con', () => {
    expect(rowSettlementKey('r1', 3, 'abc')).toBe('r1|3:abc')
  })
})
