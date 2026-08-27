/**
 * Luật chọn thẻ ngữ cảnh (nợ L-4 review 6F-1, cổng thêm ở review 6F-2 M-6):
 * thẻ đã chọn biến khỏi overview mới thì rơi về thẻ đầu, overview đang tải thì
 * giữ nguyên lựa chọn.
 */

import { describe, expect, it } from 'vitest'

import { resolveSelectedCard } from './selected-card'
import type { CashflowOverview } from './use-cashflow'

const OVERVIEW = {
  as_of: '2026-08-27',
  ledger: 0,
  cash_accounts: [{ account_code: '1111', account_name: 'Tiền mặt', balance: '1000' }],
  bank_accounts: [
    {
      bank_account_id: 21,
      code: 'VCB-001',
      name: 'VCB',
      currency_code: null,
      balance: '2000',
      balance_fc: null,
    },
  ],
  unassigned_deposit: '0',
} as unknown as CashflowOverview

describe('resolveSelectedCard', () => {
  it('thẻ đã chọn còn trong overview thì giữ nguyên', () => {
    expect(
      resolveSelectedCard({ source: 'bank', bankAccountId: 21 }, OVERVIEW),
    ).toEqual({ source: 'bank', bankAccountId: 21 })
  })

  it('thẻ đã chọn biến mất (TK bị gộp/ngừng theo dõi) thì rơi về thẻ đầu — không giữ thẻ ma', () => {
    expect(
      resolveSelectedCard({ source: 'bank', bankAccountId: 99 }, OVERVIEW),
    ).toEqual({ source: 'cash', accountCode: '1111' })
    expect(resolveSelectedCard({ source: 'cash', accountCode: '1112' }, OVERVIEW)).toEqual({
      source: 'cash',
      accountCode: '1111',
    })
  })

  it('overview đang tải thì GIỮ lựa chọn — không nhảy thẻ giữa chừng', () => {
    expect(resolveSelectedCard({ source: 'bank', bankAccountId: 99 }, undefined)).toEqual({
      source: 'bank',
      bankAccountId: 99,
    })
  })

  it('chưa bấm gì: quỹ đứng trước; không có thẻ nào thì null', () => {
    expect(resolveSelectedCard(null, OVERVIEW)).toEqual({ source: 'cash', accountCode: '1111' })
    expect(
      resolveSelectedCard(null, {
        ...OVERVIEW,
        cash_accounts: [],
      }),
    ).toEqual({ source: 'bank', bankAccountId: 21 })
    expect(
      resolveSelectedCard(null, {
        ...OVERVIEW,
        cash_accounts: [],
        bank_accounts: [],
      }),
    ).toBeNull()
  })
})
