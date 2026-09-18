/**
 * Cột trạng thái + việc tiếp theo của lưới mua hàng: mỗi nhánh một ca, và
 * phép so "còn nợ dương" trên chuỗi thập phân (không qua float).
 */

import { describe, expect, it } from 'vitest'

import { vi } from '@/locales/vi'
import type { Translate } from '@/lib/i18n'

import { isPositiveAmount, purchaseRowStatus, vendorInvoiceText } from './purchase-row-status'
import type { PurchaseInvoiceListItem } from './use-purchase-invoices'

const t: Translate = (key, params) => {
  let text: string = vi[key]
  for (const [name, value] of Object.entries(params ?? {})) {
    text = text.replace(`{${name}}`, value)
  }
  return text
}

function row(overrides: Partial<PurchaseInvoiceListItem>): PurchaseInvoiceListItem {
  return {
    id: 'x',
    voucher_no: 'MH26-00001',
    branch_id: 1,
    document_date: '2026-09-10',
    posting_date: '2026-09-10',
    status: 2,
    currency_code: 'VND',
    kind: 0,
    vendor_id: 1,
    vendor_code: 'NCC',
    vendor_name: 'NCC',
    vendor_invoice_status: 0,
    vendor_invoice_form: null,
    vendor_invoice_serial: null,
    vendor_invoice_no: null,
    total_fc: '1100000',
    remaining_fc: '1100000',
    due_date: null,
    days_overdue: null,
    ...overrides,
  }
}

describe('isPositiveAmount', () => {
  it('đọc chuỗi thập phân không qua float', () => {
    expect(isPositiveAmount('0.00')).toBe(false)
    expect(isPositiveAmount('0')).toBe(false)
    expect(isPositiveAmount(null)).toBe(false)
    expect(isPositiveAmount('0.01')).toBe(true)
    expect(isPositiveAmount('123456789012345678901234567890')).toBe(true)
    expect(isPositiveAmount('-5')).toBe(false)
  })
})

describe('purchaseRowStatus', () => {
  it('chưa ghi sổ là việc phải làm (todo) — Ghi sổ', () => {
    const status = purchaseRowStatus(t, row({ status: 1, remaining_fc: null }))
    expect(status).toMatchObject({ tone: 'todo', nextAction: 'post', actionLabel: 'Ghi sổ' })
    expect(status.label).toBe('Đã lưu, chưa ghi sổ')
  })

  it('quá hạn thắng thiếu hóa đơn: tiền đến hạn là việc khẩn hơn', () => {
    const status = purchaseRowStatus(t, row({ vendor_invoice_status: 1, days_overdue: 10 }))
    expect(status).toMatchObject({ tone: 'bad', nextAction: 'pay', actionLabel: 'Lập phiếu chi' })
    expect(status.label).toBe('Quá hạn 10 ngày')
  })

  it('thiếu hóa đơn NCC là hỏng (bad) — Bổ sung hóa đơn', () => {
    const status = purchaseRowStatus(t, row({ vendor_invoice_status: 1 }))
    expect(status).toMatchObject({ tone: 'bad', nextAction: 'add-invoice' })
    expect(status.label).toBe('Thiếu hóa đơn')
  })

  it('đã ghi sổ còn nợ chưa tới hạn là xám — nút Trả tiền nhẹ', () => {
    const status = purchaseRowStatus(t, row({}))
    expect(status).toMatchObject({ tone: 'ok', nextAction: 'pay', actionLabel: 'Trả tiền' })
    expect(status.label).toBe('Đã ghi sổ')
  })

  it('hết nợ hoặc trả lại hàng (không có khoản nợ) là Xong, không việc', () => {
    expect(purchaseRowStatus(t, row({ remaining_fc: '0.00' }))).toMatchObject({
      tone: 'ok',
      nextAction: null,
      label: 'Xong',
    })
    expect(purchaseRowStatus(t, row({ kind: 4, remaining_fc: null }))).toMatchObject({
      nextAction: null,
      label: 'Xong',
    })
  })

  it('quá hạn không còn nợ thì không phải việc — `days_overdue` chỉ có nghĩa khi còn tiền', () => {
    expect(purchaseRowStatus(t, row({ remaining_fc: '0', days_overdue: 3 }))).toMatchObject({
      tone: 'ok',
      label: 'Xong',
    })
  })
})

describe('vendorInvoiceText', () => {
  it('ghép ba mảnh; chưa có thì tô đỏ; không có (mua của cá nhân) thì không', () => {
    expect(
      vendorInvoiceText(t, row({ vendor_invoice_form: '1', vendor_invoice_serial: 'C26TVN', vendor_invoice_no: '0009120' })),
    ).toEqual({ text: '1 C26TVN 0009120', missing: false })
    expect(vendorInvoiceText(t, row({ vendor_invoice_status: 1 }))).toEqual({ text: 'chưa có', missing: true })
    expect(vendorInvoiceText(t, row({ vendor_invoice_status: 2 }))).toEqual({ text: 'không có', missing: false })
  })
})
