/**
 * Cột trạng thái + việc tiếp theo + ô hóa đơn của lưới bán hàng: mỗi nhánh
 * một ca; "thiếu hóa đơn" đúng định nghĩa server (đã ghi sổ, loại cần hóa đơn,
 * không tờ còn sống); hai nguồn số hóa đơn (tờ HĐĐT trước, gõ tay sau).
 */

import { describe, expect, it } from 'vitest'

import { vi } from '@/locales/vi'
import type { Translate } from '@/lib/i18n'

import { invoiceText, isMissingEInvoice, salesRowStatus } from './sales-row-status'
import type { SalesInvoiceListItem } from './use-sales-invoices'

const t: Translate = (key, params) => {
  let text: string = vi[key]
  for (const [name, value] of Object.entries(params ?? {})) {
    text = text.replace(`{${name}}`, value)
  }
  return text
}

function row(overrides: Partial<SalesInvoiceListItem>): SalesInvoiceListItem {
  return {
    id: 'x',
    voucher_no: 'BH26-00001',
    branch_id: 1,
    document_date: '2026-09-10',
    posting_date: '2026-09-10',
    status: 2,
    currency_code: 'VND',
    kind: 0,
    customer_id: 1,
    customer_code: 'KH',
    customer_name: 'KH',
    invoice_form: null,
    invoice_serial: null,
    invoice_no: null,
    invoice_date: null,
    has_live_einvoice: true,
    einvoice_serial: 'C26TGE',
    einvoice_no: '00000012',
    total_fc: '1100000',
    remaining_fc: '1100000',
    due_date: '2026-10-25',
    days_overdue: null,
    ...overrides,
  }
}

describe('salesRowStatus', () => {
  it('chưa ghi sổ → todo + Ghi sổ', () => {
    expect(salesRowStatus(t, row({ status: 1, remaining_fc: null }))).toMatchObject({
      tone: 'todo',
      nextAction: 'post',
      actionLabel: 'Ghi sổ',
    })
  })

  it('quá hạn đứng trước thiếu hóa đơn — bad + Lập phiếu thu', () => {
    expect(
      salesRowStatus(t, row({ has_live_einvoice: false, einvoice_no: null, days_overdue: 9 })),
    ).toMatchObject({ tone: 'bad', label: 'Quá hạn 9 ngày', nextAction: 'collect', actionLabel: 'Lập phiếu thu' })
  })

  it('đã ghi sổ, loại cần hóa đơn, không tờ còn sống → bad + Phát hành hóa đơn', () => {
    expect(
      salesRowStatus(t, row({ has_live_einvoice: false, einvoice_serial: null, einvoice_no: null })),
    ).toMatchObject({ tone: 'bad', label: 'Chưa có hóa đơn', nextAction: 'issue-einvoice' })
  })

  it('tờ trả lại (kind 2) không cần hóa đơn: không tờ vẫn không phải việc', () => {
    const returned = row({ kind: 2, has_live_einvoice: false, einvoice_no: null, remaining_fc: null })
    expect(isMissingEInvoice(returned)).toBe(false)
    expect(salesRowStatus(t, returned)).toMatchObject({ tone: 'ok', label: 'Xong', nextAction: null })
  })

  it('đã ghi sổ còn nợ chưa tới hạn → ok + Thu tiền; hết nợ → Xong', () => {
    expect(salesRowStatus(t, row({}))).toMatchObject({ tone: 'ok', label: 'Đã ghi sổ', actionLabel: 'Thu tiền' })
    expect(salesRowStatus(t, row({ remaining_fc: '0' }))).toMatchObject({ tone: 'ok', label: 'Xong', nextAction: null })
  })

  it('đã hủy → bad, không việc', () => {
    expect(salesRowStatus(t, row({ status: 4 }))).toMatchObject({ tone: 'bad', label: 'Đã hủy', nextAction: null })
  })
})

describe('invoiceText', () => {
  it('tờ HĐĐT còn sống: ký hiệu + số; số chưa về → "đang cấp số"', () => {
    expect(invoiceText(t, row({}))).toEqual({ text: 'C26TGE 00000012', missing: false })
    expect(invoiceText(t, row({ einvoice_no: null }))).toEqual({ text: 'đang cấp số', missing: false })
  })

  it('không tờ: bốn mảnh gõ tay; không có gì và cần hóa đơn → "chưa có" tô đỏ', () => {
    expect(
      invoiceText(
        t,
        row({ has_live_einvoice: false, einvoice_serial: null, einvoice_no: null, invoice_form: '1', invoice_serial: 'AB/26E', invoice_no: '0000777' }),
      ),
    ).toEqual({ text: '1 AB/26E 0000777', missing: false })
    expect(invoiceText(t, row({ has_live_einvoice: false, einvoice_serial: null, einvoice_no: null }))).toEqual({
      text: 'chưa có',
      missing: true,
    })
  })

  it('nháp chưa ghi sổ không tờ → "—", không phải việc', () => {
    expect(invoiceText(t, row({ status: 1, has_live_einvoice: false, einvoice_serial: null, einvoice_no: null }))).toEqual({
      text: '—',
      missing: false,
    })
  })
})
