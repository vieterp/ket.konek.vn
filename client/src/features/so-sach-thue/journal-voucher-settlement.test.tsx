/**
 * Khối đối trừ công nợ trên form chứng từ nghiệp vụ khác (7H-2b): hiện đúng
 * cho dòng chạm công nợ, hỏi endpoint theo BÊN của dòng, gửi `settlements`
 * với `line_no` thang server, vọng lại khi sửa, và bỏ khóa mồ côi.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  EMPTY_LIST,
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
} from './feature-test-utils'

const ACCOUNTS_ROUTE = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      {
        id: 1,
        code: '111',
        name: 'Tiền mặt',
        balance_nature: 1,
        detail_tracking: null,
        is_summary: false,
        is_foreign_currency: false,
        level: 1,
        parent_id: null,
      },
      {
        id: 131,
        code: '131',
        name: 'Phải thu khách hàng',
        balance_nature: 1,
        detail_tracking: ['customer'],
        is_summary: false,
        is_foreign_currency: false,
        level: 1,
        parent_id: null,
      },
    ],
  },
}

const PARTNERS_ROUTE = {
  status: 200,
  body: {
    items: [
      {
        id: 5,
        uid: '019-kh01',
        code: 'KH01',
        name: 'Khách A',
        name_en: null,
        parent_id: null,
        path: '5',
        level: 0,
        is_group: false,
        is_active: true,
        branch_id: null,
        row_version: 1,
      },
    ],
    total: 1,
  },
}

const OPEN_INVOICE = {
  target_kind: 3,
  target_id: 'cccccccc-0000-0000-0000-000000000003',
  partner_kind: 0,
  partner_id: 5,
  branch_id: 1,
  account_id: 131,
  invoice_no: 'GLE00007',
  invoice_date: '2026-03-01',
  due_date: null,
  currency_code: 'VND',
  exchange_rate: '1',
  amount_fc: '900000',
  remaining_fc: '900000',
  remaining: '900000',
  description: null,
}

function catalogRoutes(): Record<string, typeof EMPTY_LIST> {
  return {
    '/master/partners': PARTNERS_ROUTE,
    '/master/employees': EMPTY_LIST,
    '/master/cost_objects': EMPTY_LIST,
    '/master/projects': EMPTY_LIST,
    '/master/contracts': EMPTY_LIST,
    '/master/expense_items': EMPTY_LIST,
    '/master/items': EMPTY_LIST,
    '/master/warehouses': EMPTY_LIST,
    '/master/company_bank_accounts': EMPTY_LIST,
  }
}

function voucherBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'aaaaaaaa-0000-0000-0000-000000000021',
    document_type: 'gl_journal',
    voucher_no: 'GLE00021',
    branch_id: 1,
    document_date: '2026-03-31',
    posting_date: '2026-03-31',
    period_id: 3,
    currency_code: 'VND',
    exchange_rate: '1',
    description: 'Thu nợ bằng bút toán',
    status: 1,
    row_version: 1,
    created_at: '2026-03-31T00:00:00Z',
    created_by: 1,
    posted_at: null,
    posted_by: null,
    cashflow_activity: null,
    entry_kind: 0,
    lines: [],
    settlements: [],
    ...overrides,
  }
}

function lineOut(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 'bbbbbbbb-0000-0000-0000-000000000001',
    line_no: 1,
    account_id: 131,
    corresponding_account_id: null,
    currency_code: 'VND',
    exchange_rate: '1',
    debit_fc: '0',
    credit_fc: '500000',
    partner_id: 5,
    partner_kind: 0,
    cost_object_id: null,
    project_id: null,
    order_id: null,
    contract_id: null,
    expense_item_id: null,
    item_id: null,
    warehouse_id: null,
    bank_account_id: null,
    extended_dimensions: null,
    description: null,
    ...overrides,
  }
}

/**
 * Gõ một dòng chạm công nợ vào dòng 1 của lưới. Ô sống duy nhất là Ô ĐANG CHỌN
 * (0,0 = "TK"), đi sang phải bằng Tab (cột "Tên TK" chỉ đọc bị bỏ qua); cột
 * "Mã đối tượng" chỉ hiện sau khi TK 131 được chốt, đúng lúc Tab tới nó.
 */
async function fillDebtLine(
  user: ReturnType<typeof userEvent.setup>,
  side: 'debit' | 'credit',
  amount: string,
  partnerCode: string,
): Promise<void> {
  const accountInput = await screen.findByLabelText('TK, dòng 1')
  await user.click(accountInput)
  await user.clear(accountInput)
  await user.type(accountInput, '131')
  await user.keyboard('{Tab}') // TK → Diễn giải
  await user.keyboard('{Tab}') // Diễn giải → Nợ
  if (side === 'debit') {
    await user.type(await screen.findByLabelText('Nợ, dòng 1'), amount)
  }
  await user.keyboard('{Tab}') // Nợ → Có
  if (side === 'credit') {
    await user.type(await screen.findByLabelText('Có, dòng 1'), amount)
  }
  await user.keyboard('{Tab}') // Có → Mã đối tượng
  await user.type(await screen.findByLabelText('Mã đối tượng, dòng 1'), partnerCode)
  await user.keyboard('{Tab}')
}

describe('khối đối trừ công nợ trên form GLE', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('dòng Có 131 khách hiện khối, hỏi endpoint với on_debit=false và POST mang settlements', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      ...catalogRoutes(),
      '/gl/journal-vouchers/open-invoices': { status: 200, body: { items: [OPEN_INVOICE] } },
      '/gl/journal-vouchers': { status: 201, body: voucherBody() },
    })
    const user = userEvent.setup()

    renderFeatureAt('/so-sach-thue/chung-tu/moi')
    await user.type(await screen.findByLabelText('Diễn giải'), 'Thu nợ bằng bút toán')
    expect(screen.queryByRole('region', { name: 'Đối trừ công nợ' })).not.toBeInTheDocument()
    await fillDebtLine(user, 'credit', '500000', 'KH01')

    const block = await screen.findByRole('region', { name: 'Đối trừ công nợ' })
    expect(within(block).getByText('Dòng 1')).toBeInTheDocument()
    expect(within(block).getByText('tất toán khoản nợ')).toBeInTheDocument()

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).includes('/gl/journal-vouchers/open-invoices'),
      )
      expect(call).toBeDefined()
      const url = String(call?.[0])
      expect(url).toContain('partner_kind=0')
      expect(url).toContain('partner_id=5')
      expect(url).toContain('account_id=131')
      expect(url).toContain('on_debit=false')
      expect(url).toContain('currency_code=VND')
      expect(url).toContain('branch_id=1')
    })

    await user.type(await screen.findByLabelText('Số đối trừ cho GLE00007'), '500000')
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) =>
          String(entry[0]).endsWith('/gl/journal-vouchers') &&
          (entry[1] as RequestInit | undefined)?.method === 'POST',
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body.settlements).toEqual([
        { line_no: 1, target_kind: 3, target_id: OPEN_INVOICE.target_id, amount_fc: '500000' },
      ])
    })
  })

  it('dòng Nợ 131 khách hỏi endpoint với on_debit=true và nói rõ đang bù khoản ứng trước', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      ...catalogRoutes(),
      '/gl/journal-vouchers/open-invoices': { status: 200, body: { items: [] } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/so-sach-thue/chung-tu/moi')
    await fillDebtLine(user, 'debit', '70000', 'KH01')

    const block = await screen.findByRole('region', { name: 'Đối trừ công nợ' })
    expect(within(block).getByText('bù khoản ứng trước')).toBeInTheDocument()
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).includes('/gl/journal-vouchers/open-invoices'),
      )
      expect(String(call?.[0])).toContain('on_debit=true')
    })
  })

  const SAVED_ID = 'aaaaaaaa-0000-0000-0000-000000000021'
  function savedVoucherRoutes() {
    const saved = voucherBody({
      lines: [
        lineOut({}),
        lineOut({
          id: 'bbbbbbbb-0000-0000-0000-000000000002',
          line_no: 2,
          account_id: 1,
          debit_fc: '500000',
          credit_fc: '0',
          partner_id: null,
          partner_kind: null,
        }),
      ],
      settlements: [
        {
          id: 'dddddddd-0000-0000-0000-000000000001',
          journal_line_id: 'bbbbbbbb-0000-0000-0000-000000000001',
          target_kind: 3,
          target_id: OPEN_INVOICE.target_id,
          amount_fc: '500000',
          amount: '500000',
          fx_diff: '0',
        },
      ],
    })
    return {
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      ...catalogRoutes(),
      '/gl/journal-vouchers/open-invoices': { status: 200, body: { items: [OPEN_INVOICE] } },
      [`/gl/journal-vouchers/${SAVED_ID}`]: { status: 200, body: saved },
    }
  }

  function putBody(fetchMock: ReturnType<typeof mockServer>): Record<string, unknown> | undefined {
    const call = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith(`/gl/journal-vouchers/${SAVED_ID}`) &&
        (entry[1] as RequestInit | undefined)?.method === 'PUT',
    )
    return call === undefined ? undefined : parseJsonBody(call[1] as RequestInit)
  }

  it('mở chứng từ có đối trừ: ô điền sẵn theo journal_line_id và PUT vọng lại đúng line_no', async () => {
    const fetchMock = mockServer(savedVoucherRoutes())
    const user = userEvent.setup()

    renderFeatureAt(`/so-sach-thue/chung-tu/${SAVED_ID}`)
    expect(await screen.findByLabelText('Số đối trừ cho GLE00007')).toHaveValue('500000')

    await user.click(screen.getByRole('button', { name: 'Cất' }))
    await waitFor(() => {
      expect(putBody(fetchMock)?.settlements).toEqual([
        { line_no: 1, target_kind: 3, target_id: OPEN_INVOICE.target_id, amount_fc: '500000' },
      ])
    })
  })

  it('đổi TK của dòng đang đối trừ sang TK thường: khối biến mất và PUT không gửi dòng đối trừ mồ côi', async () => {
    const fetchMock = mockServer(savedVoucherRoutes())
    const user = userEvent.setup()

    renderFeatureAt(`/so-sach-thue/chung-tu/${SAVED_ID}`)
    expect(await screen.findByLabelText('Số đối trừ cho GLE00007')).toHaveValue('500000')

    const accountInput = await screen.findByLabelText('TK, dòng 1')
    await user.click(accountInput)
    await user.clear(accountInput)
    await user.type(accountInput, '111')
    await user.keyboard('{Tab}')
    await waitFor(() => {
      expect(screen.queryByRole('region', { name: 'Đối trừ công nợ' })).not.toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: 'Cất' }))
    await waitFor(() => {
      expect(putBody(fetchMock)?.settlements).toEqual([])
    })
  })

  it('đổi đối tác của dòng đang đối trừ: khóa cũ bị bỏ, PUT không gửi số đã gõ cho ngữ cảnh cũ (review H-1)', async () => {
    const fetchMock = mockServer({
      ...savedVoucherRoutes(),
      '/master/partners': {
        status: 200,
        body: {
          items: [
            ...PARTNERS_ROUTE.body.items,
            { ...PARTNERS_ROUTE.body.items[0], id: 6, uid: '019-kh02', code: 'KH02', name: 'Khách B', path: '6' },
          ],
          total: 2,
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/so-sach-thue/chung-tu/${SAVED_ID}`)
    expect(await screen.findByLabelText('Số đối trừ cho GLE00007')).toHaveValue('500000')

    // Ô "Mã đối tượng" dòng 1: TK → Diễn giải → Nợ → Có → Mã đối tượng.
    const accountInput = await screen.findByLabelText('TK, dòng 1')
    await user.click(accountInput)
    await user.keyboard('{Tab}{Tab}{Tab}{Tab}')
    const partnerInput = await screen.findByLabelText('Mã đối tượng, dòng 1')
    await user.clear(partnerInput)
    await user.type(partnerInput, 'KH02')
    await user.keyboard('{Tab}')

    // Khối vẫn hiện (KH02 cũng là khách) nhưng ô đối trừ của đích cũ TRỐNG.
    await waitFor(() => {
      expect(screen.getByLabelText('Số đối trừ cho GLE00007')).toHaveValue('')
    })
    await user.click(screen.getByRole('button', { name: 'Cất' }))
    await waitFor(() => {
      expect(putBody(fetchMock)?.settlements).toEqual([])
    })
  })
})
