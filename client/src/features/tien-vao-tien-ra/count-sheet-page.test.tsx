/**
 * Màn Kiểm kê quỹ: danh sách biên bản (kèm nút xử lý chênh lệch chỉ khi còn
 * chênh và chưa có phiếu), drawer lập biên bản mới POST đúng thân.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

import {
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
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
    ],
  },
}

const SHEET = {
  id: 'cccccccc-0000-0000-0000-000000000001',
  branch_id: 1,
  cash_account_id: 11,
  count_date: '2026-08-25',
  book_balance: '5000000',
  counted_total: '5500000',
  difference: '500000',
  note: null,
  adjustment_voucher_id: null,
  created_by: 1,
  created_at: '2026-08-25T00:00:00Z',
  lines: [],
}

describe('màn kiểm kê quỹ', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('liệt kê biên bản; biên bản còn chênh chưa xử lý mới có nút tạo phiếu', async () => {
    mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      '/cash-book/count-sheets': {
        status: 200,
        body: {
          items: [
            SHEET,
            { ...SHEET, id: 'cccccccc-0000-0000-0000-000000000002', difference: '0' },
            // Chênh 0 nhưng serialize scale khác ('0.000' — money.scale là
            // cấu hình cấp người dùng, 6E-2): so CHUỖI '0'/'0.00' sẽ tưởng
            // còn chênh và hiện nút (review 6F-2 M-6, mutation MC9 hoàn tác L-1).
            { ...SHEET, id: 'cccccccc-0000-0000-0000-000000000003', difference: '0.000' },
          ],
          total: 3,
          page: 1,
          page_size: 50,
        },
      },
    })

    renderFeatureAt('/tien-vao-tien-ra/kiem-ke-quy')

    expect(await screen.findAllByText('1111')).toHaveLength(3)
    // Chỉ biên bản chênh 500.000 có nút; chênh 0 — kể cả dạng '0.00' — thì không.
    expect(screen.getAllByRole('button', { name: 'Tạo phiếu xử lý chênh lệch' })).toHaveLength(1)
  })

  it('phiếu xử lý chênh gặp cảnh báo: "Vẫn ghi sổ" gửi lại kèm acknowledge_warnings=true', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      [`/cash-book/count-sheets/${SHEET.id}/actions/create-adjustment`]: (_init, url) =>
        String(url).includes('acknowledge_warnings=true')
          ? { status: 200, body: { ...SHEET, adjustment_voucher_id: 'aaaaaaaa-0000-0000-0000-0000000000ff' } }
          : {
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
                    message: 'Chi quá tồn quỹ.',
                    details: { warning: 1 },
                  },
                ],
              },
            },
      '/cash-book/count-sheets': {
        status: 200,
        body: { items: [SHEET], total: 1, page: 1, page_size: 50 },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/kiem-ke-quy')

    await user.click(
      await screen.findByRole('button', { name: 'Tạo phiếu xử lý chênh lệch' }),
    )

    expect(
      await screen.findByText('Chứng từ chưa ghi sổ — có cảnh báo cần xác nhận:'),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Vẫn ghi sổ' }))

    await waitFor(() => {
      const retry = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).includes('acknowledge_warnings=true'),
      )
      expect(retry).toBeDefined()
    })
  })

  it('drawer lập biên bản: POST đúng thân kèm khóa chống trùng', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      '/cash-book/count-sheets': (init) =>
        init?.method === 'POST'
          ? { status: 201, body: SHEET }
          : { status: 200, body: { items: [], total: 0, page: 1, page_size: 50 } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/kiem-ke-quy')

    await user.click(await screen.findByRole('button', { name: 'Lập biên bản kiểm kê' }))

    const accountInput = await screen.findByLabelText('TK quỹ')
    await user.type(accountInput, '1111')
    await user.keyboard('{Enter}')
    await user.type(screen.getByLabelText('Số kiểm đếm'), '5500000')

    await user.click(screen.getByRole('button', { name: 'Lập biên bản' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) =>
          String(entry[0]).endsWith('/cash-book/count-sheets') &&
          (entry[1] as RequestInit | undefined)?.method === 'POST',
      )
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(init.headers).toHaveProperty(IDEMPOTENCY_HEADER)
      expect(parseJsonBody(init)).toMatchObject({
        branch_id: 1,
        cash_account_id: 11,
        counted_total: '5500000',
      })
    })
  })
})
