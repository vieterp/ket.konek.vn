/**
 * Lưới hóa đơn điện tử (U3): thẻ đếm + tab từ `counts_by_status`, tab đổi
 * `tab=` gửi server, ô tìm gửi `q=`, cột gộp CQT, nút đồng bộ bơm hàng đợi,
 * hộp phát hành (có tờ nháp → chỉ cấp số; chưa có → lập rồi cấp số; mở sẵn
 * từ `?source_voucher_id=`), drawer xem trước (iframe từ blob; 503 là băng
 * vàng; ghi nhận đã gửi), xóa tờ nháp.
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
} from '@/features/tien-vao-tien-ra/feature-test-utils'

import {
  ERROR_FLOWS,
  INVOICE_ID,
  OUTBOX_EMPTY,
  VOUCHER_ID,
  listItem,
  listReply,
  renderFeatureAt,
} from './feature-test-utils'

const DRAFT_ID = 'eeeeeeee-1111-0000-0000-000000000002'

const FORMS = {
  status: 200,
  body: {
    items: [
      { id: 7, code: 'C26TKN', name: 'Hóa đơn GTGT', form_no: '1', kind: 0, is_group: false, is_active: true, row_version: 1 },
      { id: 8, code: 'NHOM', name: 'Nhóm', form_no: null, kind: null, is_group: true, is_active: true, row_version: 1 },
      { id: 9, code: 'GIAY', name: 'Đặt in', form_no: '2', kind: 1, is_group: false, is_active: true, row_version: 1 },
    ],
    total: 3,
  },
}

/** Lớp gốc (không `search=`) chỉ có nhóm; ký hiệu thật nằm TRONG nhóm và chỉ lộ ra ở chế độ tra phẳng. */
const NESTED_FORMS: FakeRoutes[string] = (_init, url) =>
  String(url).includes('search=')
    ? {
        status: 200,
        body: {
          items: [
            { id: 7, code: 'C26TKN', name: 'Hóa đơn GTGT', form_no: '1', kind: 0, is_group: false, is_active: true, parent_id: 8, row_version: 1 },
          ],
          total: 1,
        },
      }
    : { status: 200, body: { items: [FORMS.body.items[1]], total: 1 } }

function routes(list: FakeRoutes[string], extra: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/einvoices/outbox': OUTBOX_EMPTY,
    '/einvoices/error-flows': ERROR_FLOWS,
    '/master/invoice_forms': FORMS,
    ...extra,
    '/einvoices': list,
  }
}

describe('lưới hóa đơn điện tử', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('vẽ thẻ đếm + tab từ counts_by_status, cột gộp CQT kèm lý do, cột khách nhận, việc tiếp theo', async () => {
    mockServer(
      routes(
        listReply(
          [
            listItem({}),
            listItem({ id: DRAFT_ID, status: 2, invoice_no: '00004130', tax_authority_message: 'Sai MST người mua', voucher_no: 'BH26-00002' }),
          ],
          { '0': 9, '1': 2, '2': 1, '3': 200, '4': 31, '5': 7 },
        ),
      ),
    )

    renderFeatureAt('/hoa-don-dien-tu')

    const table = await screen.findByRole('table', { name: 'Hóa đơn điện tử' })
    await within(table).findByRole('row', { name: /BH26-00001/ })
    expect(screen.getByRole('tab', { name: /^Chờ phát hành/ })).toHaveTextContent('9')
    expect(screen.getByRole('tab', { name: /Cần xử lý/ })).toHaveTextContent('1')
    expect(screen.getByRole('tab', { name: /Đã thay thế · điều chỉnh/ })).toHaveTextContent('7')
    expect(screen.getByRole('tab', { name: /Khách chưa nhận/ })).toHaveTextContent('200')
    // Thẻ "Đã cấp mã" cộng cả đã gửi (3 + 4).
    expect(screen.getByText('231')).toBeInTheDocument()

    const issued = within(table).getByRole('row', { name: /BH26-00001/ })
    expect(issued).toHaveTextContent('C26TKN 00004129')
    expect(issued).toHaveTextContent('Đã cấp mã')
    expect(issued).toHaveTextContent('Chưa gửi')
    expect(within(issued).getByRole('button', { name: 'Gửi khách' })).toBeInTheDocument()
    const rejected = within(table).getByRole('row', { name: /BH26-00002/ })
    expect(rejected).toHaveTextContent('Bị từ chối · Sai MST người mua')
    expect(within(rejected).getByRole('button', { name: 'Sửa & gửi lại' })).toBeInTheDocument()
  })

  it('tab gửi `tab=`, ô tìm gửi `q=`, nút đồng bộ gọi pump', async () => {
    const fetchMock = mockServer(
      routes(listReply([]), {
        '/einvoices/outbox/actions/pump': { status: 202, body: { job_id: 'j1' } },
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu')
    await user.click(await screen.findByRole('tab', { name: /Khách chưa nhận/ }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]))
      expect(urls.some((url) => url.includes('/einvoices?') && url.includes('tab=khach-chua-nhan'))).toBe(true)
    })

    await user.type(screen.getByLabelText('Tìm số hóa đơn, khách'), 'Hoàng Long')
    await user.click(screen.getByRole('button', { name: 'Tìm' }))
    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => decodeURIComponent(String(call[0])))
      expect(urls.some((url) => url.includes('/einvoices?') && url.includes('q=Hoàng+Long'))).toBe(true)
    })

    await user.click(screen.getByRole('button', { name: 'Đồng bộ với CQT' }))
    expect(await screen.findByText(/Đã xếp lượt gửi hàng đợi/)).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(
        (call) => String(call[0]).endsWith('/einvoices/outbox/actions/pump') && (call[1] as RequestInit | undefined)?.method === 'POST',
      ),
    ).toBe(true)
  })

  it('`?source_voucher_id=` mở hộp phát hành; chứng từ chưa có tờ → chọn ký hiệu, lập rồi cấp số', async () => {
    const fetchMock = mockServer(
      routes(
        (init, url) => {
          if (init?.method === 'POST') {
            return { status: 201, body: listItem({ id: DRAFT_ID, status: 0, invoice_no: null }) }
          }
          return String(url).includes('source_voucher_id=')
            ? listReply([])
            : listReply([listItem({})], { '3': 1 })
        },
        { '/actions/issue': { status: 200, body: listItem({ id: DRAFT_ID, status: 1 }) } },
      ),
    )
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu?source_voucher_id=${VOUCHER_ID}`)
    const dialog = await screen.findByRole('dialog', { name: 'Phát hành hóa đơn' })
    const lookup = await within(dialog).findByRole('combobox', { name: 'Ký hiệu hóa đơn' })
    await user.type(lookup, 'C')
    // Chỉ ký hiệu lá · điện tử · đang dùng: nhóm và ký hiệu đặt in không có mặt.
    const options = await within(dialog).findAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual(['C26TKN — 1 · Hóa đơn GTGT'])
    await user.click(options[0] as HTMLElement)
    await user.click(within(dialog).getByRole('button', { name: 'Phát hành' }))

    await waitFor(() => {
      const created = fetchMock.mock.calls.find(
        (call) => String(call[0]).endsWith('/api/v1/einvoices') && (call[1] as RequestInit | undefined)?.method === 'POST',
      )
      expect(created).toBeDefined()
      expect(parseJsonBody(created?.[1] as RequestInit | undefined)).toEqual({
        source_voucher_id: VOUCHER_ID,
        invoice_form_id: 7,
      })
    })
    await waitFor(() => {
      const issued = fetchMock.mock.calls.find((call) =>
        String(call[0]).endsWith(`/einvoices/${DRAFT_ID}/actions/issue`),
      )
      expect(issued).toBeDefined()
      expect(parseJsonBody(issued?.[1] as RequestInit | undefined)['invoice_date']).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    })
    expect(await screen.findByText('Đã cấp số và xếp vào hàng đợi truyền tải.')).toBeInTheDocument()
  })

  it('chứng từ đã có tờ nháp → hộp phát hành bỏ bước lập, chỉ cấp số', async () => {
    const draft = listItem({ id: DRAFT_ID, status: 0, invoice_no: null, invoice_date: null })
    const fetchMock = mockServer(
      routes(listReply([draft], { '0': 1 }), {
        '/actions/issue': { status: 200, body: { ...draft, status: 1 } },
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu')
    const table = await screen.findByRole('table', { name: 'Hóa đơn điện tử' })
    await user.click(await within(table).findByRole('button', { name: 'Phát hành' }))
    const dialog = await screen.findByRole('dialog', { name: 'Phát hành hóa đơn' })
    expect(await within(dialog).findByText(/Chứng từ đã có tờ C26TKN chờ phát hành/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('combobox', { name: 'Ký hiệu hóa đơn' })).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Phát hành' }))

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith(`/einvoices/${DRAFT_ID}/actions/issue`)),
      ).toBe(true)
    })
    expect(
      fetchMock.mock.calls.some(
        (call) => String(call[0]).endsWith('/api/v1/einvoices') && (call[1] as RequestInit | undefined)?.method === 'POST',
      ),
    ).toBe(false)
  })

  it('ký hiệu nằm trong nhóm vẫn tìm được — hộp tra phẳng toàn cây khi gõ', async () => {
    const fetchMock = mockServer(
      routes(
        (init, url) =>
          init?.method === 'POST'
            ? { status: 201, body: listItem({ id: DRAFT_ID, status: 0, invoice_no: null }) }
            : String(url).includes('source_voucher_id=')
              ? listReply([])
              : listReply([]),
        { '/master/invoice_forms': NESTED_FORMS, '/actions/issue': { status: 200, body: listItem({ id: DRAFT_ID, status: 1 }) } },
      ),
    )
    const user = userEvent.setup()

    renderFeatureAt(`/hoa-don-dien-tu?source_voucher_id=${VOUCHER_ID}`)
    const dialog = await screen.findByRole('dialog', { name: 'Phát hành hóa đơn' })
    await user.type(await within(dialog).findByRole('combobox', { name: 'Ký hiệu hóa đơn' }), 'C26')
    await user.click(await within(dialog).findByRole('option', { name: /C26TKN/ }))
    await user.click(within(dialog).getByRole('button', { name: 'Phát hành' }))
    await waitFor(() => {
      const created = fetchMock.mock.calls.find(
        (call) => String(call[0]).endsWith('/api/v1/einvoices') && (call[1] as RequestInit | undefined)?.method === 'POST',
      )
      expect(parseJsonBody(created?.[1] as RequestInit | undefined)['invoice_form_id']).toBe(7)
    })
    expect(fetchMock.mock.calls.some((call) => decodeURIComponent(String(call[0])).includes('/master/invoice_forms?search=C26'))).toBe(true)
  })

  it('chứng từ đã có tờ còn hiệu lực → hộp nói ra, không có nút phát hành', async () => {
    const live = listItem({ status: 1, invoice_no: '00004131', invoice_date: null })
    mockServer(routes(listReply([live], { '1': 1 })))

    renderFeatureAt(`/hoa-don-dien-tu?source_voucher_id=${VOUCHER_ID}`)
    const dialog = await screen.findByRole('dialog', { name: 'Phát hành hóa đơn' })
    expect(await within(dialog).findByText(/đã có tờ C26TKN 00004131 còn hiệu lực/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Phát hành' })).toBeNull()
    expect(within(dialog).queryByRole('combobox', { name: 'Ký hiệu hóa đơn' })).toBeNull()
  })

  it('bấm số hóa đơn mở drawer xem trước: iframe từ blob, nút tải, ghi nhận đã gửi', async () => {
    // jsdom không có hai hàm này; gắn lên chính `URL` để `new URL()` của api-client còn chạy.
    Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:preview-1'), revokeObjectURL: vi.fn() })
    const fetchMock = mockServer(
      routes(listReply([listItem({})], { '3': 1 }), {
        '/representation': { status: 200, body: 'PDF' },
        '/actions/mark-sent': {
          status: 200,
          body: listItem({ status: 4, sent_at: '2026-08-12T09:00:00+07:00', sent_to: 'kt@hoanglong.vn' }),
        },
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu')
    const table = await screen.findByRole('table', { name: 'Hóa đơn điện tử' })
    await user.click(await within(table).findByRole('button', { name: 'C26TKN 00004129' }))
    const dialog = await screen.findByRole('dialog', { name: 'Hóa đơn C26TKN 00004129' })
    const frame = await within(dialog).findByTitle('Bản thể hiện hóa đơn')
    expect(frame).toHaveAttribute('src', 'blob:preview-1')
    expect(within(dialog).getByText('Chưa ghi nhận gửi cho khách')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Tải XML' })).toBeInTheDocument()

    await user.type(within(dialog).getByLabelText('Gửi tới (email / kênh)'), 'kt@hoanglong.vn')
    await user.click(within(dialog).getByRole('button', { name: 'Đã gửi khách' }))
    await waitFor(() => {
      const sent = fetchMock.mock.calls.find((call) => String(call[0]).endsWith('/actions/mark-sent'))
      expect(sent).toBeDefined()
      expect(parseJsonBody(sent?.[1] as RequestInit | undefined)).toEqual({ sent_to: 'kt@hoanglong.vn', sent_at: null })
    })
    // Ghi nhận xong thì drawer đóng — hàng trong drawer là ảnh chụp cũ, bấm lần hai sẽ 409.
    await waitFor(() => {
      expect(screen.queryByRole('dialog', { name: 'Hóa đơn C26TKN 00004129' })).toBeNull()
    })
  })

  it('bản thể hiện 503 là băng vàng "thử lại sau", không phải lỗi đỏ', async () => {
    mockServer(
      routes(listReply([listItem({})], { '3': 1 }), {
        '/representation': {
          status: 503,
          body: { error_code: 'einvoice.representation_unreachable', title: 'x', status: 503 },
        },
      }),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu')
    const table = await screen.findByRole('table', { name: 'Hóa đơn điện tử' })
    await user.click(await within(table).findByRole('button', { name: 'C26TKN 00004129' }))
    expect(await screen.findByText('Nhà cung cấp chưa trả bản thể hiện — thử lại sau.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')?.textContent ?? '').not.toContain('Đã xảy ra lỗi')
  })

  it('tờ nháp xóa được tại chỗ; tờ đã cấp mã có lối "Xử lý sai sót"', async () => {
    const fetchMock = mockServer(
      routes(
        listReply([listItem({}), listItem({ id: DRAFT_ID, status: 0, invoice_no: null, voucher_no: 'BH26-00002' })]),
        { [`/einvoices/${DRAFT_ID}`]: { status: 204, body: null } },
      ),
    )
    const user = userEvent.setup()

    renderFeatureAt('/hoa-don-dien-tu')
    const table = await screen.findByRole('table', { name: 'Hóa đơn điện tử' })
    const issued = await within(table).findByRole('row', { name: /BH26-00001/ })
    expect(within(issued).getByRole('button', { name: 'Xử lý sai sót' })).toBeInTheDocument()
    const draft = within(table).getByRole('row', { name: /BH26-00002/ })
    await user.click(within(draft).getByRole('button', { name: 'Xóa nháp' }))
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) => String(call[0]).endsWith(`/einvoices/${DRAFT_ID}`) && (call[1] as RequestInit | undefined)?.method === 'DELETE',
        ),
      ).toBe(true)
    })
    expect(INVOICE_ID).toBeTruthy()
  })
})
