/**
 * Tab hóa đơn đầu vào: nạp tệp XML qua multipart (409 là băng thông tin,
 * không đỏ); lưới tab con "chưa lập chứng từ" gửi `pending_only=true`; hộp
 * "Lập chứng từ mua" gửi đúng phần kế toán rồi mở form mua; drawer dòng + tải
 * XML.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
  parseJsonBody,
  seedSession,
  type FakeRoutes,
  type RouteReply,
} from '@/features/tien-vao-tien-ra/feature-test-utils'

import { renderFeatureAt } from './feature-test-utils'

const INBOUND_ID = 'aaaaaaaa-2222-0000-0000-000000000001'
const NEW_VOUCHER_ID = 'bbbbbbbb-2222-0000-0000-000000000002'

function inboundRow(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: INBOUND_ID,
    branch_id: 1,
    seller_tax_code: '0301234567',
    seller_name: 'CT TNHH Thép Việt',
    seller_address: null,
    buyer_tax_code: '0309999999',
    buyer_name: 'Konek',
    buyer_address: null,
    invoice_form: '1',
    invoice_serial: 'C26TTV',
    invoice_no: '00000321',
    invoice_date: '2026-09-10',
    tax_authority_code: 'M1-26-TV',
    currency_code: 'VND',
    exchange_rate: '1',
    total_before_tax: '10000000',
    total_vat: '800000',
    total_amount: '10800000',
    nature: 0,
    related_form: null,
    related_serial: null,
    related_no: null,
    related_date: null,
    voucher_id: null,
    vendor_id: 701,
    file_name: 'hd-321.xml',
    byte_size: 4096,
    created_at: '2026-09-10T10:00:00+07:00',
    lines: [
      { line_no: 1, description: 'Thép hộp 40x80', unit: 'cây', quantity: '100', unit_price: '100000', amount: '10000000', vat_rate: '8', vat_rate_text: '8%', vat_amount: '800000' },
    ],
    ...overrides,
  }
}

const ACCOUNTS: RouteReply = {
  status: 200,
  body: {
    items: [
      { id: 331, code: '331', name: 'Phải trả người bán', is_summary: false, detail_tracking: ['vendor'], is_active: true },
      { id: 642, code: '642', name: 'Chi phí QLDN', is_summary: false, detail_tracking: [], is_active: true },
      { id: 1331, code: '1331', name: 'Thuế GTGT được khấu trừ', is_summary: false, detail_tracking: [], is_active: true },
    ],
    total: 3,
  },
}

const OPERATIONS: RouteReply = {
  status: 200,
  body: { items: [{ operation_code: 'mua-dich-vu', operation_name: 'Mua dịch vụ', debit_account_code: '642', credit_account_code: '331' }] },
}

const PARTNERS: RouteReply = {
  status: 200,
  body: { items: [{ id: 701, uid: 'p701', code: 'NCC01', name: 'CT TNHH Thép Việt', name_en: null, parent_id: null, path: '', level: 0, is_group: false, is_active: true, branch_id: null, row_version: 1 }], total: 1 },
}

function routes(items: Record<string, unknown>[], extra: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/einvoices/outbox': { status: 200, body: { items: [], total: 0, due_now: 0 } },
    '/accounts': ACCOUNTS,
    '/auto-posting/operations': OPERATIONS,
    '/master/partners': PARTNERS,
    '/master/payment_terms': { status: 200, body: { items: [], total: 0 } },
    [`/einvoices/inbound/${INBOUND_ID}`]: { status: 200, body: inboundRow({}) },
    ...extra,
    '/einvoices/inbound': { status: 200, body: { items, pending: items.filter((row) => row['voucher_id'] === null).length } },
  }
}

describe('tab hóa đơn đầu vào', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('nạp tệp gửi multipart kèm khóa idempotency; 409 là băng thông tin', async () => {
    let attempt = 0
    const fetchMock = mockServer(
      routes([], {
        '/einvoices/inbound/import': () => {
          attempt += 1
          return attempt === 1
            ? { status: 201, body: inboundRow({}) }
            : { status: 409, body: { error_code: 'einvoice.inbound_duplicate', title: 'x', status: 409 } }
        },
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu/dau-vao')
    const input = await screen.findByLabelText('Tệp XML')
    const file = new File(['<HDon/>'], 'hd-321.xml', { type: 'application/xml' })
    await user.upload(input, file)
    await user.click(screen.getByRole('button', { name: 'Nạp' }))
    expect(await screen.findByText('Đã nạp tờ C26TTV 00000321.')).toBeInTheDocument()
    const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/einvoices/inbound/import'))
    const init = call?.[1] as RequestInit | undefined
    expect(init?.body).toBeInstanceOf(FormData)
    expect(new Headers(init?.headers).get('X-Idempotency-Key')).toBeTruthy()

    await user.upload(input, new File(['<HDon/>'], 'hd-321-lai.xml', { type: 'application/xml' }))
    await user.click(screen.getByRole('button', { name: 'Nạp' }))
    expect(await screen.findByText('Tờ hóa đơn này đã có trong sổ.')).toBeInTheDocument()
  })

  it('lưới: tab "chưa lập" gửi pending_only=true; bấm số mở drawer dòng', async () => {
    const fetchMock = mockServer(routes([inboundRow({})]))
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu/dau-vao')
    const table = await screen.findByRole('table', { name: 'Hóa đơn đầu vào' })
    const row = await within(table).findByRole('row', { name: /C26TTV 00000321/ })
    expect(row).toHaveTextContent('CT TNHH Thép Việt · 0301234567')
    expect(row).toHaveTextContent('Chưa lập')
    expect(
      fetchMock.mock.calls.some((entry) => String(entry[0]).includes('/einvoices/inbound?pending_only=true')),
    ).toBe(true)
    expect(screen.getByRole('tab', { name: /Chưa lập chứng từ/ })).toHaveTextContent('1')

    await user.click(within(row).getByRole('button', { name: 'C26TTV 00000321' }))
    const dialog = await screen.findByRole('dialog', { name: 'C26TTV 00000321' })
    expect(await within(dialog).findByText('Thép hộp 40x80')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Tải XML gốc' })).toBeInTheDocument()
  })

  it('lập chứng từ mua: đối tác điền sẵn theo MST, gửi đúng phần kế toán rồi mở form mua', async () => {
    const fetchMock = mockServer(
      routes([inboundRow({})], {
        '/actions/create-purchase': { status: 201, body: inboundRow({ voucher_id: NEW_VOUCHER_ID }) },
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu/dau-vao')
    const table = await screen.findByRole('table', { name: 'Hóa đơn đầu vào' })
    await user.click(await within(table).findByRole('button', { name: 'Lập chứng từ mua' }))
    const dialog = await screen.findByRole('dialog', { name: 'Lập chứng từ mua từ C26TTV 00000321' })
    // Đối tác khớp MST hiện ở trạng thái đã chọn (không phải ô gõ).
    expect(await within(dialog).findByText('NCC01 — CT TNHH Thép Việt')).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Ngày hạch toán')).toHaveValue('2026-09-10')
    await user.selectOptions(within(dialog).getByLabelText('Nghiệp vụ *'), 'mua-dich-vu')

    await user.type(within(dialog).getByLabelText('TK phải trả'), '331')
    await user.click(await within(dialog).findByRole('option', { name: /331 · Phải trả người bán/ }))
    await user.type(within(dialog).getByLabelText('TK Nợ dòng hàng'), '642')
    await user.click(await within(dialog).findByRole('option', { name: /642 · Chi phí QLDN/ }))
    await user.type(within(dialog).getByLabelText('TK thuế GTGT được khấu trừ'), '1331')
    await user.click(await within(dialog).findByRole('option', { name: /1331 · Thuế GTGT/ }))
    await user.click(within(dialog).getByRole('button', { name: 'Lập chứng từ' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/actions/create-purchase'))
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit | undefined)).toEqual({
        kind: 1,
        operation_code: 'mua-dich-vu',
        payable_account_id: 331,
        account_id: 642,
        vat_account_id: 1331,
        vendor_id: 701,
        posting_date: '2026-09-10',
        payment_term_id: null,
        description: null,
      })
    })
    expect(await screen.findByTestId('cash-payment-stub')).toBeInTheDocument()
  })
})
