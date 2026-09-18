/**
 * Cột gộp "Trạng thái với CQT" + việc tiếp theo + cột "Khách nhận": mỗi
 * trạng thái máy 7D một ca, kèm nhánh cơ quan thuế từ chối sau khi cấp mã và
 * hai câu "bởi số nào" của tờ bị thay thế / điều chỉnh.
 */

import { describe, expect, it } from 'vitest'

import type { Translate } from '@/lib/i18n'
import { vi } from '@/locales/vi'

import { listItem } from './feature-test-utils'
import {
  canResolveError,
  einvoiceRowStatus,
  invoiceNumberText,
  isIssued,
} from './einvoice-row-status'
import type { EInvoiceListItem } from './use-einvoices'

const t: Translate = (key, params) => {
  let text: string = vi[key]
  for (const [name, value] of Object.entries(params ?? {})) {
    text = text.replace(`{${name}}`, value)
  }
  return text
}

function row(overrides: Record<string, unknown>): EInvoiceListItem {
  return listItem(overrides) as unknown as EInvoiceListItem
}

describe('einvoiceRowStatus', () => {
  it('tờ nháp: chờ phát hành, việc = phát hành, chưa tới lúc gửi', () => {
    const status = einvoiceRowStatus(t, 'vi', row({ status: 0, invoice_no: null }))
    expect(status).toMatchObject({ tone: 'todo', nextAction: 'issue', delivery: null })
    expect(invoiceNumberText(t, row({ invoice_no: null }))).toBe('chưa cấp số')
    expect(isIssued(row({ status: 0 }))).toBe(false)
  })

  it('đang phát hành: đợi CQT, không có nút', () => {
    const status = einvoiceRowStatus(t, 'vi', row({ status: 1 }))
    expect(status.nextAction).toBeNull()
    expect(status.actionLabel).toBe('Đợi CQT')
  })

  it('phát hành lỗi: đỏ kèm lý do, việc = sửa & gửi lại', () => {
    const status = einvoiceRowStatus(t, 'vi', row({ status: 2, tax_authority_message: 'Sai MST người mua' }))
    expect(status).toMatchObject({ tone: 'bad', nextAction: 'reissue' })
    expect(status.label).toBe('Bị từ chối · Sai MST người mua')
  })

  it('đã cấp mã: xám, việc = gửi khách, cột khách nhận "Chưa gửi"', () => {
    const status = einvoiceRowStatus(t, 'vi', row({ status: 3 }))
    expect(status).toMatchObject({ tone: 'ok', nextAction: 'send' })
    expect(status.delivery).toEqual({ label: 'Chưa gửi', tone: 'todo' })
    expect(invoiceNumberText(t, row({}))).toBe('C26TKN 00004129')
  })

  it('đã gửi: khách nhận kèm ngày, không còn việc', () => {
    const status = einvoiceRowStatus(t, 'vi', row({ status: 4, sent_at: '2026-08-12T09:00:00+07:00' }))
    expect(status.nextAction).toBeNull()
    expect(status.delivery?.tone).toBe('ok')
    expect(status.delivery?.label).toMatch(/^Đã gửi /)
  })

  it('đã thay thế / điều chỉnh: nói bởi số nào khi tờ mới đã cấp số', () => {
    expect(einvoiceRowStatus(t, 'vi', row({ status: 5, superseded_by_no: '00004131' })).label).toBe(
      'Đã thay thế bởi 00004131',
    )
    expect(einvoiceRowStatus(t, 'vi', row({ status: 5 })).label).toBe('Đã thay thế')
    expect(einvoiceRowStatus(t, 'vi', row({ status: 6, superseded_by_no: '00004132' })).label).toBe(
      'Đã điều chỉnh bởi 00004132',
    )
    expect(einvoiceRowStatus(t, 'vi', row({ status: 7 })).label).toBe('Đã hủy')
  })

  it('chỉ tờ đã cấp mã / đã gửi mới xử lý sai sót được', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 7].filter((status) => canResolveError({ status }))).toEqual([3, 4])
  })
})
