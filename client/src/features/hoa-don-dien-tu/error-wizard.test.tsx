/**
 * Wizard xử lý hóa đơn sai (U4): câu hỏi dựng từ bảng quyết định (bước 2 ẩn
 * với nhánh hủy); ba nhánh gửi đúng thân `resolve-error`; hủy lập + nộp hai
 * văn bản TRƯỚC khi resolve; điều chỉnh tiền đòi chứng từ chênh; tờ không còn
 * hiệu lực bị chặn.
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

import {
  ERROR_FLOWS,
  INVOICE_ID,
  VOUCHER_ID,
  listItem,
  listReply,
  renderFeatureAt,
} from './feature-test-utils'

const REPLACEMENT_ID = 'eeeeeeee-1111-0000-0000-000000000009'
const DELTA_ID = 'bbbbbbbb-1111-0000-0000-000000000005'

function resolved(remedy: number): RouteReply {
  return {
    status: 200,
    body: {
      remedy,
      flow_code: 'x',
      legal_basis: 'NĐ 123',
      notice: {
        id: 'n1',
        einvoice_id: INVOICE_ID,
        kind: 2,
        notice_no: '04SS-01',
        notice_date: '2026-09-18',
        reason_code: null,
        reason: null,
        status: 1,
        submitted_at: null,
      },
      replacement: remedy === 0 ? listItem({ id: REPLACEMENT_ID, status: 0, invoice_no: null }) : null,
      adjustment: remedy === 1 || remedy === 2 ? listItem({ id: REPLACEMENT_ID, status: 0, invoice_no: null }) : null,
    },
  }
}

function routes(row: Record<string, unknown>, extra: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/einvoices/error-flows': ERROR_FLOWS,
    [`/einvoices/${INVOICE_ID}`]: { status: 200, body: row },
    '/einvoices': listReply([row]),
    '/sales/invoices': { status: 200, body: { items: [], total: 0, totals: [], page: 1, page_size: 50, as_of: '2026-09-18' } },
    ...extra,
  }
}

async function chooseKind(user: ReturnType<typeof userEvent.setup>, label: RegExp): Promise<void> {
  await user.click(await screen.findByRole('radio', { name: label }))
}

describe('wizard xử lý hóa đơn sai', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('thay thế: bước 2 hỏi, nhánh "chưa kê khai" → resolve với 04/SS; kết quả mở nút sửa chứng từ bán', async () => {
    const fetchMock = mockServer(routes(listItem({}), { '/actions/resolve-error': resolved(0) }))
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu/${INVOICE_ID}/xu-ly`)
    expect(await screen.findByRole('heading', { name: /Xử lý hóa đơn C26TKN 00004129 — CT TNHH Hoàng Long/ })).toBeInTheDocument()
    await chooseKind(user, /Sai tên, địa chỉ người mua/)
    // Chưa trả lời câu hai → chưa có nhánh, không có nút thi hành.
    expect(screen.queryByRole('button', { name: /^Thi hành/ })).toBeNull()
    await user.click(screen.getByRole('radio', { name: 'Chưa kê khai' }))
    expect(await screen.findByText(/Hệ thống sẽ làm giúp bạn: Lập hóa đơn thay thế/)).toBeInTheDocument()

    const notice = screen.getByRole('group', { name: 'Thông báo sai sót 04/SS-HĐĐT' })
    await user.type(within(notice).getByLabelText('Số văn bản'), '04SS-01')
    await user.type(screen.getByLabelText('Diễn giải sai sót'), 'Sai địa chỉ')
    await user.click(screen.getByRole('button', { name: 'Thi hành: Lập hóa đơn thay thế' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/actions/resolve-error'))
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit | undefined)).toMatchObject({
        error_kind: 0,
        buyer_declared: false,
        notice_no: '04SS-01',
        reason: 'Sai địa chỉ',
        adjustment_voucher_id: null,
      })
    })
    expect(await screen.findByText('Đã xử lý: Lập hóa đơn thay thế.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Sửa chứng từ bán' }))
    expect(await screen.findByTestId('cash-payment-stub')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some((entry) => String(entry[0]).includes('/notices'))).toBe(false)
  })

  it('hủy: không hỏi câu hai; hai văn bản hủy nộp trước rồi mới resolve', async () => {
    const fetchMock = mockServer(
      routes(listItem({}), {
        '/notices': { status: 201, body: { id: 'n0', einvoice_id: INVOICE_ID, kind: 0, notice_no: 'x', notice_date: '2026-09-18', reason_code: null, reason: null, status: 1, submitted_at: null } },
        '/actions/resolve-error': resolved(3),
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu/${INVOICE_ID}/xu-ly`)
    await chooseKind(user, /Hủy toàn bộ/)
    expect(screen.queryByRole('radio', { name: 'Chưa kê khai' })).toBeNull()
    expect(await screen.findByText(/Hệ thống sẽ làm giúp bạn: Hủy hóa đơn/)).toBeInTheDocument()

    await user.type(within(screen.getByRole('group', { name: 'Thông báo hủy (gửi CQT)' })).getByLabelText('Số văn bản'), 'TBH-01')
    await user.type(within(screen.getByRole('group', { name: 'Biên bản hủy (với người mua)' })).getByLabelText('Số văn bản'), 'BBH-01')
    await user.type(within(screen.getByRole('group', { name: 'Thông báo sai sót 04/SS-HĐĐT' })).getByLabelText('Số văn bản'), '04SS-02')
    await user.click(screen.getByRole('button', { name: 'Thi hành: Hủy hóa đơn' }))

    await waitFor(() => {
      expect(fetchMock.mock.calls.some((entry) => String(entry[0]).endsWith('/actions/resolve-error'))).toBe(true)
    })
    const order = fetchMock.mock.calls
      .map((entry) => String(entry[0]))
      .filter((url) => url.endsWith('/notices') || url.endsWith('/actions/resolve-error'))
    expect(order.map((url) => url.slice(url.indexOf('/api/')))).toEqual([
      `/api/v1/einvoices/${INVOICE_ID}/notices`,
      `/api/v1/einvoices/${INVOICE_ID}/notices`,
      `/api/v1/einvoices/${INVOICE_ID}/actions/resolve-error`,
    ])
    const notices = fetchMock.mock.calls
      .filter((entry) => String(entry[0]).endsWith('/notices'))
      .map((entry) => parseJsonBody(entry[1] as RequestInit | undefined))
    expect(notices).toEqual([
      expect.objectContaining({ kind: 0, notice_no: 'TBH-01', submitted: true }),
      expect.objectContaining({ kind: 1, notice_no: 'BBH-01', submitted: true }),
    ])
    const resolve = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/actions/resolve-error'))
    expect(parseJsonBody(resolve?.[1] as RequestInit | undefined)).toMatchObject({ error_kind: 2, buyer_declared: null, notice_no: '04SS-02' })
  })

  it('điều chỉnh tiền: picker hỏi server chứng từ điều chỉnh đã ghi sổ CỦA chứng từ gốc; thiếu thì chặn; có thì gửi kèm', async () => {
    const fetchMock = mockServer(
      routes(listItem({}), {
        '/sales/invoices': (_init, url) => ({
          status: 200,
          body: {
            // Server lọc theo chứng từ gốc + đã ghi sổ — client không lọc lại.
            items:
              String(url).includes(`adjusts_voucher_id=${VOUCHER_ID}`) && String(url).includes('status=2')
                ? [{ id: DELTA_ID, voucher_no: 'BH26-00090', posting_date: '2026-09-15', total_fc: '16800000', adjusts_voucher_id: VOUCHER_ID, customer_id: 601 }]
                : [],
            total: 0,
            totals: [],
            page: 1,
            page_size: 50,
            as_of: '2026-09-18',
          },
        }),
        '/actions/resolve-error': resolved(2),
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu/${INVOICE_ID}/xu-ly`)
    await chooseKind(user, /Sai số tiền/)
    await user.click(screen.getByRole('radio', { name: 'Đã kê khai' }))
    expect(await screen.findByText(/Hệ thống sẽ làm giúp bạn: Lập hóa đơn điều chỉnh tăng\/giảm/)).toBeInTheDocument()

    const picker = screen.getByLabelText('Chứng từ điều chỉnh')
    await waitFor(() => {
      expect(within(picker).getAllByRole('option')).toHaveLength(2)
    })
    expect(within(picker).getByRole('option', { name: /BH26-00090/ })).toBeInTheDocument()
    await user.type(within(screen.getByRole('group', { name: 'Thông báo sai sót 04/SS-HĐĐT' })).getByLabelText('Số văn bản'), '04SS-03')
    await user.click(screen.getByRole('button', { name: /^Thi hành/ }))
    expect(await screen.findByText('Chọn chứng từ bán mang phần chênh.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some((entry) => String(entry[0]).endsWith('/actions/resolve-error'))).toBe(false)

    await user.selectOptions(picker, DELTA_ID)
    await user.click(screen.getByRole('button', { name: /^Thi hành/ }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/actions/resolve-error'))
      expect(parseJsonBody(call?.[1] as RequestInit | undefined)).toMatchObject({
        error_kind: 1,
        buyer_declared: true,
        adjustment_voucher_id: DELTA_ID,
      })
    })
    // Nhánh chênh lệch: không có nút "Sửa chứng từ bán" — tờ mới nằm trên chứng từ chênh.
    expect(await screen.findByText(/Tờ điều chỉnh đang "Chờ phát hành" trên chứng từ chênh lệch/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sửa chứng từ bán' })).toBeNull()
  })

  it('hủy dở dang: văn bản thứ hai lỗi → gửi lại, văn bản đầu 409 "đã có" được bỏ qua, resolve vẫn chạy', async () => {
    let recordAttempts = 0
    const fetchMock = mockServer(
      routes(listItem({}), {
        '/notices': (init) => {
          const body = parseJsonBody(init)
          if (body['kind'] === 0) {
            return { status: 409, body: { error_code: 'data.duplicate', title: 'x', status: 409 } }
          }
          recordAttempts += 1
          return recordAttempts === 1
            ? { status: 422, body: { error_code: 'validation.failed', title: 'x', status: 422 } }
            : { status: 201, body: { id: 'n1', einvoice_id: INVOICE_ID, kind: 1, notice_no: 'BBH-01', notice_date: '2026-09-18', reason_code: null, reason: null, status: 1, submitted_at: null } }
        },
        '/actions/resolve-error': resolved(3),
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu/${INVOICE_ID}/xu-ly`)
    await chooseKind(user, /Hủy toàn bộ/)
    await screen.findByText(/Hệ thống sẽ làm giúp bạn: Hủy hóa đơn/)
    await user.type(within(screen.getByRole('group', { name: 'Thông báo hủy (gửi CQT)' })).getByLabelText('Số văn bản'), 'TBH-01')
    await user.type(within(screen.getByRole('group', { name: 'Biên bản hủy (với người mua)' })).getByLabelText('Số văn bản'), 'BBH-01')
    await user.type(within(screen.getByRole('group', { name: 'Thông báo sai sót 04/SS-HĐĐT' })).getByLabelText('Số văn bản'), '04SS-02')
    // Lượt đầu: thông báo hủy "đã có" (409) bỏ qua, biên bản 422 → dừng, báo lỗi.
    await user.click(screen.getByRole('button', { name: 'Thi hành: Hủy hóa đơn' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some((entry) => String(entry[0]).endsWith('/actions/resolve-error'))).toBe(false)
    // Lượt hai: đi tới resolve.
    await user.click(screen.getByRole('button', { name: 'Thi hành: Hủy hóa đơn' }))
    expect(await screen.findByText('Đã xử lý: Hủy hóa đơn.')).toBeInTheDocument()
    expect(recordAttempts).toBe(2)
  })

  it('thiếu ngày biên bản hủy → chặn trước khi gửi bất kỳ văn bản nào', async () => {
    const fetchMock = mockServer(routes(listItem({})))
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu/${INVOICE_ID}/xu-ly`)
    await chooseKind(user, /Hủy toàn bộ/)
    await screen.findByText(/Hệ thống sẽ làm giúp bạn: Hủy hóa đơn/)
    await user.type(within(screen.getByRole('group', { name: 'Thông báo hủy (gửi CQT)' })).getByLabelText('Số văn bản'), 'TBH-01')
    const record = screen.getByRole('group', { name: 'Biên bản hủy (với người mua)' })
    await user.type(within(record).getByLabelText('Số văn bản'), 'BBH-01')
    await user.clear(within(record).getByLabelText('Ngày văn bản'))
    await user.type(within(screen.getByRole('group', { name: 'Thông báo sai sót 04/SS-HĐĐT' })).getByLabelText('Số văn bản'), '04SS-02')
    await user.click(screen.getByRole('button', { name: 'Thi hành: Hủy hóa đơn' }))
    expect(await screen.findByText('Nhập số của cả thông báo hủy và biên bản hủy.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some((entry) => String(entry[0]).endsWith('/notices'))).toBe(false)
  })

  it('tờ đã bị thay thế: băng cảnh báo, nút thi hành khóa', async () => {
    mockServer(routes(listItem({ status: 5, superseded_by_no: '00004131' })))
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu/${INVOICE_ID}/xu-ly`)
    expect(await screen.findByText(/không còn hiệu lực để xử lý sai sót/)).toBeInTheDocument()
    await chooseKind(user, /Hủy toàn bộ/)
    expect(await screen.findByRole('button', { name: 'Thi hành: Hủy hóa đơn' })).toBeDisabled()
  })
})
