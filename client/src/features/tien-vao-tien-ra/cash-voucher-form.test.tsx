/**
 * Form phiếu thu: chọn nghiệp vụ điền sẵn cặp Nợ/Có (FR-SYS-025), POST kèm
 * khóa chống trùng, và vòng "Vẫn ghi sổ?" cho cảnh báo FR-SYS-062 (mọi vi phạm
 * mang `details.warning` → gửi lại với `acknowledge_warnings=true`).
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

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
        id: 11,
        code: '1111',
        name: 'Tiền mặt VND',
        balance_nature: 1,
        detail_tracking: null,
        is_summary: false,
        is_foreign_currency: false,
        level: 2,
        parent_id: null,
      },
      {
        id: 71,
        code: '711',
        name: 'Thu nhập khác',
        balance_nature: 2,
        detail_tracking: null,
        is_summary: false,
        is_foreign_currency: false,
        level: 1,
        parent_id: null,
      },
    ],
  },
}

const OPERATIONS_ROUTE: RouteReply = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      {
        operation_code: 'thu-khac',
        operation_name: 'Thu khác',
        debit_account_code: '1111',
        credit_account_code: '711',
        requires_partner: false,
        partner_kind: null,
        display_order: 1,
      },
    ],
  },
}

const CREATED_VOUCHER = {
  id: 'aaaaaaaa-0000-0000-0000-000000000001',
  document_type: 'PT',
  voucher_no: 'PT26-00001',
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
  kind: 0,
  operation_code: 'thu-khac',
  cash_account_id: 11,
  partner_id: null,
  partner_kind: null,
  payer_receiver_name: null,
  attachment_count: null,
  treasurer_status: 0,
  lines: [],
  settlements: [],
}

function formRoutes(): FakeRoutes {
  return {
    ...baseRoutes(),
    '/accounts': ACCOUNTS_ROUTE,
    '/auto-posting/operations': OPERATIONS_ROUTE,
    ...dimensionCatalogRoutes(),
    '/cashflow/overview': EMPTY_OVERVIEW,
    '/cashflow/transactions': { status: 200, body: { items: [], total: 0 } },
  }
}

/** Chọn nghiệp vụ + TK quỹ + nhập số tiền dòng 1 — đường nhập tối thiểu của một phiếu thu. */
async function fillMinimalReceipt(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const operationSelect = await screen.findByLabelText('Nghiệp vụ')
  await waitFor(() => {
    expect(screen.getByRole('option', { name: 'Thu khác' })).toBeInTheDocument()
  })
  await user.selectOptions(operationSelect, 'thu-khac')

  const cashInput = screen.getByLabelText('TK quỹ')
  await user.type(cashInput, '1111')
  await user.keyboard('{Enter}')

  // Ô sống duy nhất của lưới là Ô ĐANG CHỌN (0,0 = "TK Nợ") — đi tới cột
  // "Số tiền" bằng Tab (bỏ qua hai cột tên chỉ-đọc), cùng khuôn test GLE.
  const firstCell = await screen.findByLabelText('TK Nợ, dòng 1')
  await user.click(firstCell)
  await user.keyboard('{Tab}') // TK Nợ → TK Có
  await user.keyboard('{Tab}') // TK Có → Diễn giải
  await user.keyboard('{Tab}') // Diễn giải → Số tiền
  const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
  await user.type(amountInput, '1000000')
  await user.keyboard('{Tab}')
}

describe('form phiếu thu tiền mặt', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('chọn nghiệp vụ điền sẵn cặp Nợ/Có; POST đúng thân kèm khóa chống trùng', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/cash-book/vouchers': { status: 201, body: CREATED_VOUCHER },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    await fillMinimalReceipt(user)

    // Nghiệp vụ đã điền sẵn mã TK vào dòng đầu — tên TK hiện trên cột chỉ-đọc.
    expect(screen.getByText('Thu nhập khác')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/cash-book/vouchers'),
      )
      expect(call).toBeDefined()
    })
    const call = fetchMock.mock.calls.find((entry) =>
      String(entry[0]).endsWith('/cash-book/vouchers'),
    )
    const init = call?.[1] as RequestInit
    expect(init.headers).toHaveProperty(IDEMPOTENCY_HEADER)
    const body = parseJsonBody(init)
    expect(body).toMatchObject({ kind: 0, operation_code: 'thu-khac', cash_account_id: 11 })
    expect((body.lines as Record<string, unknown>[])[0]).toMatchObject({
      debit_account_id: 11,
      credit_account_id: 71,
      amount_fc: '1000000',
    })

    // Lưu xong quay về màn Tiền vào tiền ra.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Tiền vào tiền ra' })).toBeInTheDocument()
    })
  })

  it('422 toàn cảnh báo: hiện "Vẫn ghi sổ", bấm là gửi lại với acknowledge_warnings=true', async () => {
    const warningReply: RouteReply = {
      status: 422,
      body: {
        type: 'https://konek.vn/errors/posting.invalid',
        title: 'posting.invalid',
        status: 422,
        detail: 'Chứng từ không hợp lệ',
        error_code: 'posting.invalid',
        violations: [
          {
            code: 'warning.cash_balance',
            message: 'Chi quá tồn quỹ: số dư sẽ âm 500.000.',
            details: { warning: 1 },
          },
        ],
      },
    }
    const fetchMock = mockServer({
      ...formRoutes(),
      '/cash-book/vouchers': (_init, url) =>
        String(url).includes('acknowledge_warnings=true')
          ? { status: 201, body: CREATED_VOUCHER }
          : warningReply,
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    await fillMinimalReceipt(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    // Băng cảnh báo thay băng lỗi, kèm nội dung cảnh báo và nút xác nhận.
    expect(
      await screen.findByText('Chứng từ chưa ghi sổ — có cảnh báo cần xác nhận:'),
    ).toBeInTheDocument()
    expect(screen.getByText('Chi quá tồn quỹ: số dư sẽ âm 500.000.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Vẫn ghi sổ' }))

    await waitFor(() => {
      const retry = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).includes('acknowledge_warnings=true'),
      )
      expect(retry).toBeDefined()
    })
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Tiền vào tiền ra' })).toBeInTheDocument()
    })
  })

  it('vi phạm CHẶN lẫn trong cảnh báo: chỉ hiện lỗi, không có nút xác nhận', async () => {
    mockServer({
      ...formRoutes(),
      '/cash-book/vouchers': {
        status: 422,
        body: {
          type: 'https://konek.vn/errors/posting.invalid',
          title: 'posting.invalid',
          status: 422,
          detail: 'Chứng từ không hợp lệ',
          error_code: 'posting.invalid',
          violations: [
            { code: 'warning.cash_balance', message: 'Chi quá tồn quỹ.', details: { warning: 1 } },
            { code: 'posting.unbalanced', message: 'Chứng từ không cân.', line_no: 1 },
          ],
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    await fillMinimalReceipt(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(await screen.findByText('Dòng 1: Chứng từ không cân.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Vẫn ghi sổ' })).not.toBeInTheDocument()
  })
})
