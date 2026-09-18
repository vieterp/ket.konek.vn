/**
 * Luật 6F-1 trên lưới bán: cột nào làm câu hỏi giá đổi đáp án, cột nào là
 * "chốt giá tay", và đổi mã hàng thì cụm giá cũ phải rơi.
 */

import { describe, expect, it } from 'vitest'

import { applySalesLineChanges, emptySalesLineRow } from './sales-line-types'
import { applyQuoteToRow, quoteFingerprint, quoteRequestFor } from './use-price-quote'

const items = [{ id: 7001, code: 'VT0231', label: 'Thép hộp' }]
const context = { onDate: '2026-09-18', customerId: 601, priceListId: null }

describe('applySalesLineChanges', () => {
  it('gõ mã hàng / số lượng trên dòng chưa chốt tay → dòng vào danh sách hỏi giá', () => {
    const start = [emptySalesLineRow()]
    const result = applySalesLineChanges(
      start,
      [
        { rowIndex: 0, columnKey: 'item', value: 'VT0231' },
        { rowIndex: 0, columnKey: 'quantity', value: '10' },
      ],
      [],
    )
    expect(result.requote).toEqual([0])
    expect(result.rows[0]).toMatchObject({ itemCode: 'VT0231', quantity: '10', priceTyped: false })
  })

  it('gõ đơn giá là chốt tay: cờ bật, nguồn về null, không hỏi giá lại khi đổi số lượng', () => {
    const [seeded] = applySalesLineChanges([emptySalesLineRow()], [{ rowIndex: 0, columnKey: 'item', value: 'VT0231' }], []).rows
    const quoted = applyQuoteToRow(seeded!, {
      unit_price: '100000',
      quoted_price: '100000',
      is_tax_inclusive: false,
      source: 'item_default',
      price_list_id: null,
      level: 1,
      discount_percent: '0',
    })
    expect(quoted).toMatchObject({ unitPriceFc: '100000', priceSource: 'item_default' })

    const typed = applySalesLineChanges([quoted], [{ rowIndex: 0, columnKey: 'unit_price_fc', value: '95000' }], [])
    expect(typed.requote).toEqual([])
    expect(typed.rows[0]).toMatchObject({ unitPriceFc: '95000', priceTyped: true, priceSource: null })

    const moreQuantity = applySalesLineChanges(typed.rows, [{ rowIndex: 0, columnKey: 'quantity', value: '20' }], [])
    expect(moreQuantity.requote).toEqual([])
    expect(quoteRequestFor(moreQuantity.rows[0]!, context, items, [])).toBeNull()
  })

  it('ĐỔI mã hàng xóa cụm giá cũ + quy cách đã lưu và mở lại cho bộ định giá', () => {
    const typed = applySalesLineChanges(
      [{ ...emptySalesLineRow(), itemCode: 'VT0231', unitPriceFc: '95000', priceTyped: true, variantId: 501 }],
      [{ rowIndex: 0, columnKey: 'item', value: 'VT0999' }],
      [],
    )
    expect(typed.requote).toEqual([0])
    expect(typed.rows[0]).toMatchObject({ itemCode: 'VT0999', unitPriceFc: '', priceTyped: false, variantId: null })
  })

  it('gõ mã hàng vào dòng trống KHÔNG xóa đơn giá gõ trước đó (6F-1)', () => {
    const typed = applySalesLineChanges(
      [{ ...emptySalesLineRow(), unitPriceFc: '95000', priceTyped: true }],
      [{ rowIndex: 0, columnKey: 'item', value: 'VT0231' }],
      [],
    )
    expect(typed.requote).toEqual([])
    expect(typed.rows[0]).toMatchObject({ itemCode: 'VT0231', unitPriceFc: '95000', priceTyped: true })
  })

  it('dấu vân câu hỏi giá đổi theo bốn cột đầu vào, không theo đơn giá', () => {
    const row = { ...emptySalesLineRow(), itemCode: 'VT0231', quantity: '10' }
    expect(quoteFingerprint(row)).toBe(quoteFingerprint({ ...row, unitPriceFc: '5', itemCode: 'vt0231 ' }))
    expect(quoteFingerprint(row)).not.toBe(quoteFingerprint({ ...row, quantity: '11' }))
    expect(quoteFingerprint(row)).not.toBe(quoteFingerprint({ ...row, vatRate: '8' }))
  })
})

describe('quoteRequestFor / applyQuoteToRow', () => {
  it('chưa có số lượng thì hỏi cho 1 đơn vị; thuế suất theo dòng; mã lạ → null', () => {
    const row = { ...emptySalesLineRow(), itemCode: 'vt0231', vatRate: '8' }
    expect(quoteRequestFor(row, context, items, [])).toMatchObject({
      item_id: 7001,
      unit_id: null,
      quantity: '1',
      direction: 1,
      on_date: '2026-09-18',
      partner_id: 601,
      tax_rate: '8',
    })
    expect(quoteRequestFor({ ...row, itemCode: 'KHONG-CO' }, context, items, [])).toBeNull()
  })

  it('source = none → đơn giá để trống, nguồn ghi "none"; thành tiền không bị đụng', () => {
    const row = { ...emptySalesLineRow(), itemCode: 'VT0231', amountFc: '123' }
    const applied = applyQuoteToRow(row, {
      unit_price: '0',
      quoted_price: '0',
      is_tax_inclusive: false,
      source: 'none',
      price_list_id: null,
      level: null,
      discount_percent: '0',
    })
    expect(applied).toMatchObject({ unitPriceFc: '', discountPercent: '', priceSource: 'none', amountFc: '123' })
  })
})
