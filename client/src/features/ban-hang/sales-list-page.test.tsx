/**
 * Lưới chứng từ bán hàng (U1): tab từ BFF việc còn thiếu đổi bộ lọc lưới đúng
 * định nghĩa nhóm (`einvoice=missing`); ba việc tiếp theo; dòng tổng nhiều
 * tiền tệ; hóa đơn thiếu tô đỏ.
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

import { listItem, renderFeatureAt } from './feature-test-utils'

const PENDING: RouteReply = {
  status: 200,
  body: {
    side: 'sales',
    as_of: '2026-09-18',
    groups: [
      { code: 'chua-ghi-so', count: 1, next_action: 'post', sample: [] },
      { code: 'chua-co-hoa-don', count: 3, next_action: 'issue-einvoice', sample: [] },
      { code: 'qua-han', count: 2, next_action: 'collect', sample: [] },
    ],
  },
}

const DRAFT_ID = 'bbbbbbbb-1111-0000-0000-000000000011'
const BARE_ID = 'bbbbbbbb-1111-0000-0000-000000000012'

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
  return { ...baseRoutes(), '/sales/pending-issues': PENDING, '/sales/invoices': list }
}

describe('lưới chứng từ bán hàng', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('vẽ tab từ BFF kèm số việc, cột khách / hóa đơn / còn phải thu, dòng thiếu hóa đơn tô đỏ', async () => {
    mockServer(
      routes(
        listReply(
          [
            listItem({}),
            listItem({
              id: BARE_ID,
              voucher_no: 'BH26-00002',
              has_live_einvoice: false,
              einvoice_serial: null,
              einvoice_no: null,
              total_fc: '128400000',
              remaining_fc: '128400000',
            }),
          ],
          [{ currency_code: 'VND', count: 164, total_fc: '9480240000', remaining_fc: '2140880000' }],
        ),
      ),
    )

    renderFeatureAt('/ban-hang/chung-tu')

    expect(await screen.findByRole('tab', { name: /Chưa ghi sổ/ })).toHaveTextContent('1')
    expect(screen.getByRole('tab', { name: /Chưa có hóa đơn/ })).toHaveTextContent('3')
    expect(screen.getByRole('tab', { name: /Quá hạn thu/ })).toHaveTextContent('2')

    const table = await screen.findByRole('table', { name: 'Chứng từ bán hàng' })
    expect(within(table).getAllByText('CT CP Xây dựng Nam Long')).toHaveLength(2)
    expect(within(table).getByText('C26TGE 00000012')).toBeInTheDocument()
    const missing = within(table).getByText('chưa có')
    expect(missing.className).toContain('text-status-bad')
    expect(within(table).getByRole('button', { name: 'Phát hành hóa đơn' })).toBeInTheDocument()

    const totalRow = within(table).getByRole('row', { name: /Tổng 164 chứng từ/ })
    expect(totalRow).toHaveTextContent('9.480.240.000')
    expect(totalRow).toHaveTextContent('2.140.880.000')
  })

  it('nhiều tiền tệ: mỗi tiền tệ một dòng tổng; tờ trả lại không có số còn nợ (dấu gạch)', async () => {
    mockServer(
      routes(
        listReply(
          [
            listItem({}),
            listItem({ id: BARE_ID, voucher_no: 'BH26-00002', kind: 2, remaining_fc: null, has_live_einvoice: false, einvoice_no: null, einvoice_serial: null }),
          ],
          [
            { currency_code: 'USD', count: 1, total_fc: '110', remaining_fc: '0' },
            { currency_code: 'VND', count: 3, total_fc: '2000000', remaining_fc: '500000' },
          ],
        ),
      ),
    )

    renderFeatureAt('/ban-hang/chung-tu')
    const table = await screen.findByRole('table', { name: 'Chứng từ bán hàng' })
    expect(await within(table).findByRole('row', { name: /Tổng 1 chứng từ USD/ })).toHaveTextContent('110')
    expect(within(table).getByRole('row', { name: /Tổng 3 chứng từ VND/ })).toHaveTextContent('2.000.000')
    expect(within(table).getByRole('row', { name: /BH26-00001/ })).toHaveTextContent('Đã ghi sổ')
    // Tờ trả lại: ô "Hóa đơn" và "Còn phải thu" đều là dấu gạch; trạng thái Xong.
    const returned = within(table).getByRole('row', { name: /BH26-00002/ })
    expect(returned).toHaveTextContent('Xong')
    expect(returned).toHaveTextContent('—')
  })

  it('tab "Chưa có hóa đơn" lọc `status=2&einvoice=missing`; tab "Quá hạn" lọc `overdue=true`', async () => {
    const fetchMock = mockServer(routes(listReply([])))
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu')
    await user.click(await screen.findByRole('tab', { name: /Chưa có hóa đơn/ }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]))
      expect(
        urls.some(
          (url) => url.includes('/sales/invoices?') && url.includes('status=2') && url.includes('einvoice=missing'),
        ),
      ).toBe(true)
    })

    await user.click(screen.getByRole('tab', { name: /Quá hạn thu/ }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]))
      expect(urls.some((url) => url.includes('/sales/invoices?') && url.includes('overdue=true'))).toBe(true)
    })
  })

  it('"Ghi sổ" tại chỗ gọi endpoint ghi sổ dùng chung kèm khóa idempotency', async () => {
    const fetchMock = mockServer({
      ...routes(listReply([listItem({ id: DRAFT_ID, status: 1, remaining_fc: null, has_live_einvoice: false, einvoice_no: null, einvoice_serial: null })])),
      [`/vouchers/${DRAFT_ID}/actions/post`]: { status: 200, body: {} },
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu')
    await user.click(await screen.findByRole('button', { name: 'Ghi sổ' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => String(entry[0]).includes(`/vouchers/${DRAFT_ID}/actions/post`))
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(init.method).toBe('POST')
      expect(new Headers(init.headers).get('X-Idempotency-Key')).toBeTruthy()
    })
  })

  it('"Lập phiếu thu" mở form phiếu thu của nhóm 03 với khách điền sẵn', async () => {
    mockServer(routes(listReply([listItem({ customer_id: 601, days_overdue: 12 })])))
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu')
    expect(await screen.findByText('Quá hạn 12 ngày')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Lập phiếu thu' }))

    expect(await screen.findByTestId('cash-payment-stub')).toHaveTextContent('?kind=0&partner_id=601')
  })

  it('"Phát hành hóa đơn" dẫn sang nhóm Hóa đơn điện tử kèm source_voucher_id', async () => {
    mockServer(routes(listReply([listItem({ id: BARE_ID, has_live_einvoice: false, einvoice_no: null, einvoice_serial: null })])))
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu')
    await user.click(await screen.findByRole('button', { name: 'Phát hành hóa đơn' }))

    expect(await screen.findByTestId('cash-payment-stub')).toHaveTextContent(`?source_voucher_id=${BARE_ID}`)
  })

  it('nút Tạo chỉ có năm loại (không có điều chỉnh) và mang loại đã chọn sang form', async () => {
    mockServer({
      ...routes(listReply([])),
      '/accounts': { status: 200, body: { package_id: 1, items: [] } },
      '/auto-posting/operations': { status: 200, body: { package_id: 1, items: [] } },
      '/master/partners': { status: 200, body: { items: [], total: 0 } },
      '/master/employees': { status: 200, body: { items: [], total: 0 } },
      '/master/payment_terms': { status: 200, body: { items: [], total: 0 } },
      '/master/price_lists': { status: 200, body: { items: [], total: 0 } },
      '/master/units_of_measure': { status: 200, body: { items: [], total: 0 } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu')
    const select = await screen.findByLabelText('Loại chứng từ')
    expect(within(select).getAllByRole('option')).toHaveLength(5)
    expect(within(select).queryByRole('option', { name: /Điều chỉnh/ })).not.toBeInTheDocument()
    await user.selectOptions(select, '2')
    await user.click(screen.getByRole('button', { name: 'Tạo chứng từ bán hàng' }))

    expect(
      await screen.findByRole('heading', { name: 'Tạo chứng từ bán hàng — Hàng bán bị trả lại' }),
    ).toBeInTheDocument()
  })
})
