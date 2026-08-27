/**
 * Form chứng từ ngân hàng: chuyển tiền nội bộ (CTNB) không có nghiệp vụ định
 * khoản và bắt buộc TK ngân hàng đích; POST đúng thân theo loại.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  EMPTY_OVERVIEW,
  baseRoutes,
  dimensionCatalogRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
  type RouteReply,
} from './feature-test-utils'

const ACCOUNTS_ROUTE: RouteReply = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      {
        id: 21,
        code: '1121',
        name: 'Tiền gửi VND',
        balance_nature: 1,
        detail_tracking: null,
        is_summary: false,
        is_foreign_currency: false,
        level: 2,
        parent_id: null,
      },
    ],
  },
}

const BANK_ACCOUNTS_ROUTE: RouteReply = {
  status: 200,
  body: {
    items: [
      {
        id: 7,
        uid: '019-vcb',
        code: 'VCB-01',
        name: 'TK Vietcombank',
        name_en: null,
        parent_id: null,
        path: '7',
        level: 0,
        is_group: false,
        is_active: true,
        branch_id: null,
        row_version: 1,
      },
      {
        id: 8,
        uid: '019-acb',
        code: 'ACB-01',
        name: 'TK ACB',
        name_en: null,
        parent_id: null,
        path: '8',
        level: 0,
        is_group: false,
        is_active: true,
        branch_id: null,
        row_version: 1,
      },
    ],
    total: 2,
  },
}

function formRoutes(): FakeRoutes {
  return {
    ...baseRoutes(),
    '/accounts': ACCOUNTS_ROUTE,
    '/master/company_bank_accounts': BANK_ACCOUNTS_ROUTE,
    ...dimensionCatalogRoutes(),
    '/cashflow/overview': EMPTY_OVERVIEW,
    '/cashflow/transactions': { status: 200, body: { items: [], total: 0 } },
  }
}

describe('form chứng từ ngân hàng', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('chuyển nội bộ: ẩn ô nghiệp vụ, POST mang counter_bank_account_id và không operation_code', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/bank/vouchers': {
        status: 201,
        body: {
          id: 'bbbbbbbb-0000-0000-0000-000000000001',
          document_type: 'CTNB',
          voucher_no: 'CTNB26-00001',
          branch_id: 1,
          document_date: '2026-08-27',
          posting_date: '2026-08-27',
          period_id: 8,
          currency_code: 'VND',
          exchange_rate: '1',
          description: null,
          status: 1,
          cashflow_activity: null,
          entry_kind: 0,
          created_at: '2026-08-27T00:00:00Z',
          created_by: 1,
          posted_at: null,
          posted_by: null,
          row_version: 1,
          kind: 3,
          operation_code: null,
          bank_account_id: 7,
          counter_bank_account_id: 8,
          partner_id: null,
          partner_kind: null,
          beneficiary_name: null,
          beneficiary_account_no: null,
          beneficiary_bank_name: null,
          cheque_no: null,
          cheque_date: null,
          reference_no: null,
          lines: [],
          settlements: [],
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/ngan-hang/moi')

    await user.click(await screen.findByRole('radio', { name: 'Chuyển nội bộ' }))
    expect(screen.queryByLabelText('Nghiệp vụ')).not.toBeInTheDocument()

    const sourceInput = screen.getByLabelText('TK ngân hàng')
    await user.type(sourceInput, 'VCB')
    await user.keyboard('{Enter}')
    const counterInput = screen.getByLabelText('TK ngân hàng đích')
    await user.type(counterInput, 'ACB')
    await user.keyboard('{Enter}')

    // Ô sống duy nhất của lưới là Ô ĐANG CHỌN (0,0 = "TK Nợ") — gõ TK rồi Tab
    // tới cột "Số tiền" (bỏ qua hai cột tên chỉ-đọc).
    const debitInput = await screen.findByLabelText('TK Nợ, dòng 1')
    await user.type(debitInput, '1121')
    await user.keyboard('{Tab}') // TK Nợ → TK Có
    await user.keyboard('{Tab}') // TK Có → Diễn giải
    await user.keyboard('{Tab}') // Diễn giải → Số tiền
    const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
    await user.type(amountInput, '2000000')
    await user.keyboard('{Tab}')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/bank/vouchers'),
      )
      expect(call).toBeDefined()
    })
    const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/bank/vouchers'))
    const body = parseJsonBody(call?.[1] as RequestInit)
    expect(body).toMatchObject({ kind: 3, bank_account_id: 7, counter_bank_account_id: 8 })
    expect(body).not.toHaveProperty('operation_code')
    expect((body.settlements as unknown[]).length).toBe(0)
  })

  it('chuyển nội bộ thiếu TK đích: chặn ngay trên form, không POST', async () => {
    const fetchMock = mockServer(formRoutes())
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/ngan-hang/moi')

    await user.click(await screen.findByRole('radio', { name: 'Chuyển nội bộ' }))
    const sourceInput = screen.getByLabelText('TK ngân hàng')
    await user.type(sourceInput, 'VCB')
    await user.keyboard('{Enter}')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(
      await screen.findByText('Chuyển nội bộ phải chọn tài khoản ngân hàng đích.'),
    ).toBeInTheDocument()
    const posted = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/bank/vouchers') &&
        (entry[1] as RequestInit | undefined)?.method === 'POST',
    )
    expect(posted).toBeUndefined()
  })
})
