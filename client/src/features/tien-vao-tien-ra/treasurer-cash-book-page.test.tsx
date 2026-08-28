/**
 * Sổ quỹ thủ quỹ: lưới phân trang `limit`/`offset` (nợ 6C → API 6E-1) — trang
 * hai gửi đúng offset và tổng số dòng hiện ở chân trang.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
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
    ],
  },
}

function row(id: number, day: string, receipt: string, payment: string): Record<string, unknown> {
  return {
    id,
    branch_id: 1,
    cash_account_id: 11,
    book_date: day,
    voucher_id: `aaaaaaaa-0000-0000-0000-${String(id).padStart(12, '0')}`,
    receipt_amount: receipt,
    payment_amount: payment,
    posted_by: 5,
    posted_at: '2026-08-20T04:00:00Z',
  }
}

describe('sổ quỹ thủ quỹ', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('hiện dòng + tổng; trang sau gửi offset=50', async () => {
    const requested: string[] = []
    const routes: FakeRoutes = {
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      '/treasurer/cash-book': (_init, url) => {
        requested.push(String(url))
        return {
          status: 200,
          body: { items: [row(1, '2026-08-19', '400000', '0'), row(2, '2026-08-20', '0', '150000')], total: 120 },
        }
      },
    }
    mockServer(routes)
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/thu-quy/so-quy')

    expect(await screen.findByText('Tổng 120 dòng')).toBeInTheDocument()
    // `dateStyle: 'short'` của vi: '19/8/26'.
    expect(screen.getByText('19/8/26')).toBeInTheDocument()
    // Trang 1 phải là offset=0 — khẳng định `some(offset=50)` đơn thuần thỏa
    // được ngay ở trang 1 dưới đột biến lệch-trang (review 6F-2 M-5).
    expect(requested).toHaveLength(1)
    expect(requested[0]).toContain('offset=0')

    await user.click(screen.getByRole('button', { name: 'Trang sau' }))
    await waitFor(() => {
      expect(requested).toHaveLength(2)
      expect(requested[1]).toContain('offset=50')
    })
  })
})
