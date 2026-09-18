/**
 * Lưới chứng từ mua hàng (U1): tab từ BFF việc còn thiếu đổi bộ lọc lưới đúng
 * định nghĩa nhóm; ba việc tiếp theo; dòng tổng; hóa đơn NCC thiếu tô đỏ.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
  seedSession,
  type FakeRoutes,
  type RouteReply,
} from '@/features/tien-vao-tien-ra/feature-test-utils'

import {
  listItem,
  renderFeatureAt,
} from './feature-test-utils'

const PENDING: RouteReply = {
  status: 200,
  body: {
    side: 'purchase',
    as_of: '2026-09-18',
    groups: [
      { code: 'chua-ghi-so', count: 1, next_action: 'post', sample: [] },
      { code: 'chua-co-hoa-don', count: 2, next_action: 'add-invoice', sample: [] },
      { code: 'qua-han', count: 1, next_action: 'pay', sample: [] },
    ],
  },
}

const DRAFT_ID = 'bbbbbbbb-0000-0000-0000-000000000011'

function listReply(items: Record<string, unknown>[], totals?: Record<string, unknown>[]): RouteReply {
  return {
    status: 200,
    body: {
      items,
      total: totals === undefined ? items.length : totals.reduce((sum, row) => sum + Number(row.count), 0),
      totals: totals ?? [{ currency_code: 'VND', count: items.length, total_fc: '0', remaining_fc: '0' }],
      page: 1,
      page_size: 50,
      as_of: '2026-09-18',
    },
  }
}

function routes(list: RouteReply): FakeRoutes {
  return { ...baseRoutes(), '/purchase/pending-issues': PENDING, '/purchase/invoices': list }
}

describe('lưới chứng từ mua hàng', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('vẽ tab từ BFF kèm số việc, cột NCC / hóa đơn NCC / còn nợ, và dòng tổng của cả tập lọc', async () => {
    mockServer(
      routes(
        listReply(
          [
            listItem({}),
            listItem({
              id: 'bbbbbbbb-0000-0000-0000-000000000002',
              voucher_no: 'MH26-00002',
              vendor_invoice_status: 1,
              vendor_invoice_serial: null,
              vendor_invoice_no: null,
              total_fc: '128400000',
              remaining_fc: '128400000',
            }),
          ],
          [{ currency_code: 'VND', count: 164, total_fc: '9480240000', remaining_fc: '2140880000' }],
        ),
      ),
    )

    renderFeatureAt('/mua-hang/chung-tu')

    expect(await screen.findByRole('tab', { name: /Chưa ghi sổ/ })).toHaveTextContent('1')
    expect(screen.getByRole('tab', { name: /Thiếu hóa đơn của NCC/ })).toHaveTextContent('2')
    expect(screen.getByRole('tab', { name: /Quá hạn trả/ })).toHaveTextContent('1')

    const table = await screen.findByRole('table', { name: 'Chứng từ mua hàng' })
    expect(within(table).getAllByText('CT TNHH Thép Việt Nhật')).toHaveLength(2)
    expect(within(table).getByText('1 C26TVN 0009120')).toBeInTheDocument()
    const missing = within(table).getByText('chưa có')
    expect(missing.className).toContain('text-status-bad')
    expect(within(table).getByText('Thiếu hóa đơn')).toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Bổ sung hóa đơn' })).toBeInTheDocument()

    // Dòng tổng là con số server tính trên TOÀN tập lọc — 164 chứng từ dù trang chỉ có 2.
    const totalRow = within(table).getByRole('row', { name: /Tổng 164 chứng từ/ })
    expect(totalRow).toHaveTextContent('9.480.240.000')
    expect(totalRow).toHaveTextContent('2.140.880.000')
    expect(screen.getByText('164 chứng từ')).toBeInTheDocument()
  })

  it('nhiều tiền tệ: mỗi tiền tệ một dòng tổng, nhãn nêu tiền tệ, mẫu số phân trang là tổng chung', async () => {
    mockServer(
      routes(
        listReply(
          [listItem({}), listItem({ id: 'bbbbbbbb-0000-0000-0000-000000000003', currency_code: 'USD', total_fc: '110' })],
          [
            { currency_code: 'USD', count: 1, total_fc: '110', remaining_fc: '0' },
            { currency_code: 'VND', count: 3, total_fc: '2000000', remaining_fc: '500000' },
          ],
        ),
      ),
    )

    renderFeatureAt('/mua-hang/chung-tu')
    const table = await screen.findByRole('table', { name: 'Chứng từ mua hàng' })
    expect(await within(table).findByRole('row', { name: /Tổng 1 chứng từ USD/ })).toHaveTextContent('110')
    expect(within(table).getByRole('row', { name: /Tổng 3 chứng từ VND/ })).toHaveTextContent('2.000.000')
    expect(screen.getByText('4 chứng từ')).toBeInTheDocument()
    expect(screen.getByText('1–4 trong 4')).toBeInTheDocument()
  })

  it('tab "Thiếu hóa đơn" lọc `status=2&vendor_invoice_status=1`; tab "Quá hạn" lọc `overdue=true`', async () => {
    const fetchMock = mockServer(routes(listReply([])))
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu')
    await user.click(await screen.findByRole('tab', { name: /Thiếu hóa đơn của NCC/ }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]))
      expect(
        urls.some(
          (url) =>
            url.includes('/purchase/invoices?') &&
            url.includes('status=2') &&
            url.includes('vendor_invoice_status=1'),
        ),
      ).toBe(true)
    })

    await user.click(screen.getByRole('tab', { name: /Quá hạn trả/ }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]))
      expect(urls.some((url) => url.includes('/purchase/invoices?') && url.includes('overdue=true'))).toBe(
        true,
      )
    })
  })

  it('"Ghi sổ" tại chỗ gọi endpoint ghi sổ dùng chung kèm khóa idempotency', async () => {
    const fetchMock = mockServer({
      ...routes(listReply([listItem({ id: DRAFT_ID, status: 1, remaining_fc: null })])),
      [`/vouchers/${DRAFT_ID}/actions/post`]: { status: 200, body: {} },
    })
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu')
    await user.click(await screen.findByRole('button', { name: 'Ghi sổ' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).includes(`/vouchers/${DRAFT_ID}/actions/post`),
      )
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(init.method).toBe('POST')
      expect(new Headers(init.headers).get('X-Idempotency-Key')).toBeTruthy()
    })
  })

  it('"Lập phiếu chi" mở form phiếu chi của nhóm 03 với NCC điền sẵn', async () => {
    mockServer(routes(listReply([listItem({ vendor_id: 501, days_overdue: 12 })])))
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu')
    expect(await screen.findByText('Quá hạn 12 ngày')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Lập phiếu chi' }))

    expect(await screen.findByTestId('cash-payment-stub')).toHaveTextContent(
      '?kind=1&partner_id=501',
    )
  })

  it('nút Tạo mang loại chứng từ đã chọn sang form', async () => {
    mockServer({
      ...routes(listReply([])),
      '/accounts': { status: 200, body: { package_id: 1, items: [] } },
      '/auto-posting/operations': { status: 200, body: { package_id: 1, items: [] } },
      '/master/partners': { status: 200, body: { items: [], total: 0 } },
      '/master/employees': { status: 200, body: { items: [], total: 0 } },
      '/master/payment_terms': { status: 200, body: { items: [], total: 0 } },
      '/master/units_of_measure': { status: 200, body: { items: [], total: 0 } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu')
    await user.selectOptions(await screen.findByLabelText('Loại chứng từ'), '4')
    await user.click(screen.getByRole('button', { name: 'Tạo chứng từ mua hàng' }))

    expect(
      await screen.findByRole('heading', { name: 'Tạo chứng từ mua hàng — Trả lại hàng mua' }),
    ).toBeInTheDocument()
  })
})
