/**
 * Màn "Tiền vào tiền ra": hàng thẻ từ BFF overview, lưới giao dịch đổi theo
 * thẻ đang chọn (đúng tham số truy vấn cho từng nguồn), và dòng ghi chú số dư
 * 112 chưa gắn tài khoản ngân hàng (M-3 review 6D).
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
  renderFeatureAt,
  seedSession,
  type RouteReply,
} from './feature-test-utils'

const OVERVIEW: RouteReply = {
  status: 200,
  body: {
    as_of: '2026-08-27',
    ledger: 0,
    cash_accounts: [
      {
        source: 'cash',
        account_code: '1111',
        account_name: 'Tiền mặt VND',
        currency_code: 'VND',
        balance: '5000000',
      },
    ],
    bank_accounts: [
      {
        source: 'bank',
        bank_account_id: 7,
        bank_account_code: 'VCB-01',
        bank_account_name: 'TK Vietcombank',
        bank_name: 'Vietcombank',
        currency_code: 'VND',
        balance_fc: '20000000',
        balance: '20000000',
      },
    ],
    unassigned_deposit: '300000',
  },
}

const TRANSACTIONS: RouteReply = {
  status: 200,
  body: {
    items: [
      {
        voucher_id: 'aaaaaaaa-0000-0000-0000-000000000001',
        voucher_no: 'PT26-00001',
        document_type: 'PT',
        source: 'cash',
        posting_date: '2026-08-20',
        document_date: '2026-08-20',
        description: 'Thu tiền bán hàng',
        partner_name: 'Công ty A',
        amount: '1000000',
        status: 2,
      },
    ],
    total: 1,
  },
}

describe('màn hình Tiền vào tiền ra', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('vẽ hàng thẻ, lưới của thẻ mặc định (quỹ) và ghi chú 112 chưa gắn', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/cashflow/overview': OVERVIEW,
      '/cashflow/transactions': TRANSACTIONS,
    })

    renderFeatureAt('/tien-vao-tien-ra/giao-dich')

    expect(await screen.findByText('Tiền mặt VND')).toBeInTheDocument()
    expect(screen.getByText('TK Vietcombank')).toBeInTheDocument()
    // Ghi chú M-3: phần 112 không quy được về thẻ nào phải hiện ra, không bị giấu.
    expect(screen.getByText(/chưa gắn tài khoản ngân hàng/)).toBeInTheDocument()

    // Lưới của thẻ mặc định = thẻ quỹ đầu tiên, lọc theo SỐ HIỆU TK.
    expect(await screen.findByText('PT26-00001')).toBeInTheDocument()
    const call = fetchMock.mock.calls.find((entry) =>
      String(entry[0]).includes('/cashflow/transactions'),
    )
    expect(String(call?.[0])).toContain('source=cash')
    expect(String(call?.[0])).toContain('cash_account_code=1111')
  })

  it('bấm thẻ ngân hàng: lưới tra lại theo bank_account_id', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/cashflow/overview': OVERVIEW,
      '/cashflow/transactions': TRANSACTIONS,
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich')

    await user.click(await screen.findByRole('option', { name: /TK Vietcombank/ }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).includes('source=bank'),
      )
      expect(String(call?.[0])).toContain('bank_account_id=7')
    })
  })

  it('không có thẻ nào trong phạm vi: hiện hướng dẫn thay vì trang trắng', async () => {
    mockServer({
      ...baseRoutes(),
      '/cashflow/overview': {
        status: 200,
        body: {
          as_of: '2026-08-27',
          ledger: 0,
          cash_accounts: [],
          bank_accounts: [],
          unassigned_deposit: '0',
        },
      },
    })

    renderFeatureAt('/tien-vao-tien-ra/giao-dich')

    expect(
      await screen.findByText(/Chưa có tài khoản quỹ hay ngân hàng nào trong phạm vi/),
    ).toBeInTheDocument()
  })
})
