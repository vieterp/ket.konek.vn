/**
 * Form hóa đơn bán: nghiệp vụ điền sẵn TK phải thu + TK dòng; bộ định giá điền
 * đơn giá/%CK theo luật 6F-1 (không ghi đè giá đã gõ, hỏi lại khi đổi khách,
 * `source=none` để trống); POST đủ thân hóa đơn kèm khóa chống trùng; vòng
 * "Vẫn ghi sổ?" cho cảnh báo ngưỡng nợ khách; form sửa dựng lại lưới và PUT
 * vọng lại trọn bộ (giá vốn, quy cách, order_id, nguồn giá, row_version); tờ
 * trả lại đối trừ hóa đơn gốc qua `/sales/open-invoices`; điều chỉnh qua
 * deep-link; xóa vướng tờ HĐĐT nháp nói ra bước phải làm.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

import { catalogRow } from '@/features/mua-hang/feature-test-utils'
import {
  EMPTY_LIST,
  baseRoutes,
  dimensionCatalogRoutes,
  mockServer,
  parseJsonBody,
  seedSession,
  type FakeRoutes,
  type RouteReply,
} from '@/features/tien-vao-tien-ra/feature-test-utils'

import { renderFeatureAt } from './feature-test-utils'

function account(
  id: number,
  code: string,
  name: string,
  detailTracking: string[] | null = null,
  isSummary = false,
): Record<string, unknown> {
  return {
    id,
    code,
    name,
    balance_nature: 1,
    detail_tracking: detailTracking,
    is_summary: isSummary,
    is_foreign_currency: false,
    level: 2,
    parent_id: null,
  }
}

const ACCOUNTS_ROUTE: RouteReply = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      account(5111, '5111', 'Doanh thu bán hàng hóa'),
      account(521, '521', 'Giảm trừ doanh thu'),
      account(131, '131', 'Phải thu của khách hàng', ['customer']),
      account(33311, '33311', 'Thuế GTGT đầu ra'),
      account(13, '13', 'Phải thu (tổng hợp)', null, true),
    ],
  },
}

const OPERATIONS_ROUTE: RouteReply = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      {
        operation_code: 'ban-hang-hoa',
        operation_name: 'Bán hàng hóa trong nước',
        debit_account_code: '131',
        credit_account_code: '5111',
        requires_partner: true,
        partner_kind: 0,
        display_order: 1,
      },
      {
        operation_code: 'tra-lai-hang-ban',
        operation_name: 'Hàng bán bị trả lại',
        debit_account_code: '521',
        credit_account_code: '131',
        requires_partner: true,
        partner_kind: 0,
        display_order: 2,
      },
      {
        operation_code: 'dieu-chinh-giam-hoa-don',
        operation_name: 'Điều chỉnh giảm hóa đơn đã phát hành',
        debit_account_code: '521',
        credit_account_code: '131',
        requires_partner: true,
        partner_kind: 0,
        display_order: 3,
      },
    ],
  },
}

const CUSTOMER_ID = 601
const ITEM_ID = 7001
const UNIT_ID = 7101
const WAREHOUSE_ID = 7201
const PRICE_LIST_ID = 9101
const INVOICE_ID = 'cccccccc-1111-0000-0000-000000000001'
const ORIGINAL_ID = 'cccccccc-1111-0000-0000-000000000002'
const DEBT_ID = 'dddddddd-1111-0000-0000-000000000001'

const PARTNERS_ROUTE: RouteReply = {
  status: 200,
  body: { items: [catalogRow(CUSTOMER_ID, 'KH01', 'CT CP Xây dựng Nam Long')], total: 1 },
}

const OVERVIEW_ROUTE: RouteReply = {
  status: 200,
  body: {
    partner: {
      ...catalogRow(CUSTOMER_ID, 'KH01', 'CT CP Xây dựng Nam Long'),
      is_customer: true,
      is_vendor: false,
      is_organization: true,
      tax_code: '0301447712',
      payment_term_id: null,
    },
    debt: { receivable: null, payable: null, credit_limit: null, credit_available: null },
  },
}

/** Bộ định giá giả: mã hàng 7001 có giá mặc định 100.000, CK 5% từ bậc số lượng. */
function quoteReply(source: string): (init?: RequestInit) => RouteReply {
  return (init) => {
    const lines = (parseJsonBody(init).lines ?? []) as Record<string, unknown>[]
    return {
      status: 200,
      body: {
        items: lines.map(() => ({
          unit_price: source === 'none' ? '0' : '100000',
          quoted_price: source === 'none' ? '0' : '100000',
          is_tax_inclusive: false,
          source,
          price_list_id: source === 'price_list' ? PRICE_LIST_ID : null,
          level: 1,
          discount_percent: source === 'none' ? '0' : '5',
        })),
      },
    }
  }
}

const CREATED_INVOICE = {
  id: INVOICE_ID,
  document_type: 'SAL',
  voucher_no: 'BH26-00001',
  branch_id: 1,
  document_date: '2026-09-18',
  posting_date: '2026-09-18',
  period_id: 9,
  currency_code: 'VND',
  exchange_rate: '1',
  description: null,
  status: 1,
  cashflow_activity: null,
  entry_kind: 0,
  created_at: '2026-09-18T00:00:00Z',
  created_by: 1,
  posted_at: null,
  posted_by: null,
  row_version: 1,
  kind: 0,
  adjusts_voucher_id: null,
  operation_code: 'ban-hang-hoa',
  customer_id: CUSTOMER_ID,
  salesperson_id: 88,
  ship_to: '12 Nguyễn Huệ',
  recipient_name: 'Anh Long',
  invoice_form: null,
  invoice_serial: null,
  invoice_no: null,
  invoice_date: null,
  payment_term_id: 5,
  due_date: '2026-10-25',
  receivable_account_id: 131,
  price_list_id: PRICE_LIST_ID,
  is_stock_issue: true,
  cogs_posted: false,
  total_before_tax_fc: '950000',
  total_discount_fc: '50000',
  total_vat_fc: '95000',
  total_fc: '1045000',
  lines: [
    {
      id: 'eeeeeeee-1111-0000-0000-000000000001',
      line_no: 1,
      description: 'Thép hộp',
      item_id: ITEM_ID,
      unit_id: UNIT_ID,
      warehouse_id: WAREHOUSE_ID,
      variant_id: 501,
      quantity: '10',
      unit_price_fc: '100000',
      discount_percent: '5',
      discount_amount_fc: '50000',
      amount_fc: '950000',
      vat_rate: '10',
      vat_amount_fc: '95000',
      account_id: 5111,
      vat_account_id: 33311,
      cogs_account_id: 632,
      inventory_account_id: 156,
      unit_cost_fc: '80000',
      price_list_id: PRICE_LIST_ID,
      price_source: 'price_list',
      cost_object_id: null,
      project_id: null,
      order_id: 77,
      contract_id: null,
      expense_item_id: null,
      extended_dimensions: { '3': 31 },
    },
  ],
  settlements: [],
}

const EMPTY_INVOICE_LIST: RouteReply = {
  status: 200,
  body: { items: [], total: 0, totals: [], page: 1, page_size: 50, as_of: '2026-09-18' },
}

function formRoutes(): FakeRoutes {
  return {
    ...baseRoutes(),
    ...dimensionCatalogRoutes(),
    '/accounts': ACCOUNTS_ROUTE,
    '/auto-posting/operations': OPERATIONS_ROUTE,
    '/master/partners': PARTNERS_ROUTE,
    '/master/items': { status: 200, body: { items: [catalogRow(ITEM_ID, 'VT0231', 'Thép hộp 40x80')], total: 1 } },
    '/master/units_of_measure': { status: 200, body: { items: [catalogRow(UNIT_ID, 'Cay', 'Cây')], total: 1 } },
    '/master/warehouses': { status: 200, body: { items: [catalogRow(WAREHOUSE_ID, 'KHCM', 'Kho HCM')], total: 1 } },
    '/master/payment_terms': EMPTY_LIST,
    '/master/price_lists': { status: 200, body: { items: [catalogRow(PRICE_LIST_ID, 'BG-DL', 'Bảng giá đại lý')], total: 1 } },
    '/pricing/quote-batch': quoteReply('item_default'),
    [`/partners/${String(CUSTOMER_ID)}/overview`]: OVERVIEW_ROUTE,
    '/sales/pending-issues': { status: 200, body: { side: 'sales', as_of: '2026-09-18', groups: [] } },
  }
}

type User = ReturnType<typeof userEvent.setup>

async function pickCustomerAndOperation(user: User): Promise<void> {
  const customerInput = await screen.findByLabelText('Khách hàng *')
  await user.type(customerInput, 'KH01')
  await user.keyboard('{Enter}')
  const operationSelect = screen.getByLabelText('Nghiệp vụ *')
  await waitFor(() => {
    expect(screen.getByRole('option', { name: 'Bán hàng hóa trong nước' })).toBeInTheDocument()
  })
  await user.selectOptions(operationSelect, 'ban-hang-hoa')
}

/** Gõ mã hàng rồi Tab tới Số lượng — hai ô làm bộ định giá chạy. */
async function typeItemAndQuantity(user: User): Promise<void> {
  const itemCell = await screen.findByLabelText('Mã hàng, dòng 1')
  await user.click(itemCell)
  await user.type(itemCell, 'VT0231')
  await user.keyboard('{Tab}') // → ĐVT
  await user.keyboard('{Tab}') // → Kho
  await user.keyboard('{Tab}') // → Diễn giải
  await user.keyboard('{Tab}') // → Số lượng
  const quantityCell = await screen.findByLabelText('Số lượng, dòng 1')
  await user.type(quantityCell, '10')
  await user.keyboard('{Tab}') // → Đơn giá
}

/** Từ ô Đơn giá đang chọn: Tab qua CK % · Tiền CK tới Thành tiền, Tiền thuế. */
async function typeAmounts(user: User): Promise<void> {
  await user.keyboard('{Tab}') // → CK %
  await user.keyboard('{Tab}') // → Tiền CK
  await user.keyboard('{Tab}') // → Thành tiền
  const amountCell = await screen.findByLabelText('Thành tiền, dòng 1')
  await user.type(amountCell, '950000')
  await user.keyboard('{Tab}') // → Thuế %
  await user.keyboard('{Tab}') // → Tiền thuế
  const vatCell = await screen.findByLabelText('Tiền thuế, dòng 1')
  await user.type(vatCell, '95000')
  await user.keyboard('{Tab}')
}

function quoteCalls(fetchMock: ReturnType<typeof mockServer>): Record<string, unknown>[] {
  return fetchMock.mock.calls
    .filter((entry) => String(entry[0]).endsWith('/pricing/quote-batch'))
    .map((entry) => parseJsonBody(entry[1] as RequestInit))
}

describe('form hóa đơn bán hàng', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('nghiệp vụ điền TK phải thu + TK dòng; bộ định giá điền đơn giá/%CK kèm nguồn; Cất gửi đủ thân kèm khóa chống trùng', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/sales/invoices': (init) => (init?.method === 'POST' ? { status: 201, body: CREATED_INVOICE } : EMPTY_INVOICE_LIST),
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    expect(await screen.findByRole('heading', { name: 'Tạo chứng từ bán hàng — Bán hàng hóa' })).toBeInTheDocument()
    await pickCustomerAndOperation(user)
    expect(await screen.findByText('MST 0301447712')).toBeInTheDocument()
    await typeItemAndQuantity(user)

    // Bộ định giá đã hỏi với khách + ngày + thuế suất của dòng, rồi điền cụm giá.
    await waitFor(() => {
      expect(screen.getByLabelText('Đơn giá, dòng 1')).toHaveValue('100000')
    })
    const grid = screen.getByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })
    expect(within(grid).getByText('giá mặc định')).toBeInTheDocument()
    const lastQuote = quoteCalls(fetchMock).at(-1)
    expect((lastQuote?.lines as Record<string, unknown>[])[0]).toMatchObject({
      item_id: ITEM_ID,
      quantity: '10',
      direction: 1,
      partner_id: CUSTOMER_ID,
      price_list_id: null,
    })
    await typeAmounts(user)

    await user.click(screen.getByRole('button', { name: /Mở rộng/ }))
    expect(screen.getByText('131 — Phải thu của khách hàng')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/api/v1/sales/invoices') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(new Headers(init.headers).get(IDEMPOTENCY_HEADER)).toBeTruthy()
      const body = parseJsonBody(init)
      expect(body).toMatchObject({
        kind: 0,
        adjusts_voucher_id: null,
        operation_code: 'ban-hang-hoa',
        customer_id: CUSTOMER_ID,
        receivable_account_id: 131,
        branch_id: 1,
        price_list_id: null,
        is_stock_issue: false,
        settlements: [],
      })
      const lines = body.lines as Record<string, unknown>[]
      expect(lines).toHaveLength(1)
      expect(lines[0]).toMatchObject({
        item_id: ITEM_ID,
        quantity: '10',
        unit_price_fc: '100000',
        discount_percent: '5',
        discount_amount_fc: '0',
        amount_fc: '950000',
        vat_amount_fc: '95000',
        account_id: 5111,
        price_list_id: null,
        price_source: 'item_default',
        variant_id: null,
        cogs_account_id: null,
      })
    })
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Chứng từ bán hàng' })).toBeInTheDocument()
    })
  })

  it('giá gõ tay không bị ghi đè: đổi số lượng không hỏi lại; đổi khách chỉ hỏi lại dòng chưa gõ tay', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/partners': {
        status: 200,
        body: {
          items: [catalogRow(CUSTOMER_ID, 'KH01', 'CT CP Xây dựng Nam Long'), catalogRow(602, 'KH02', 'Khách hai')],
          total: 2,
        },
      },
      '/partners/602/overview': OVERVIEW_ROUTE,
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    await pickCustomerAndOperation(user)
    await typeItemAndQuantity(user)
    await waitFor(() => {
      expect(screen.getByLabelText('Đơn giá, dòng 1')).toHaveValue('100000')
    })
    const quotesBeforeTyping = quoteCalls(fetchMock).length

    // Gõ đè đơn giá → chốt tay; nguồn đổi thành "gõ tay".
    const priceCell = screen.getByLabelText('Đơn giá, dòng 1')
    await user.clear(priceCell)
    await user.type(priceCell, '95000')
    await user.keyboard('{Tab}')
    const grid = screen.getByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })
    expect(await within(grid).findByText('gõ tay')).toBeInTheDocument()

    // Đổi số lượng trên dòng đã chốt tay: không hỏi giá, giá giữ nguyên.
    await user.click(within(grid).getByText('10'))
    const quantityCell = await screen.findByLabelText('Số lượng, dòng 1')
    await user.clear(quantityCell)
    await user.type(quantityCell, '20')
    await user.keyboard('{Tab}')
    expect(screen.getByLabelText('Đơn giá, dòng 1')).toHaveValue('95000')
    expect(quoteCalls(fetchMock)).toHaveLength(quotesBeforeTyping)

    // Đổi khách: dòng đã chốt tay vẫn không được hỏi lại.
    await user.click(screen.getByRole('button', { name: 'Bỏ chọn' }))
    // Ô gõ vừa hiện lại đã mở danh sách gợi ý (cùng nhãn) — lấy phần tử đầu là ô gõ.
    const customerInput = (await screen.findAllByLabelText('Khách hàng *'))[0]
    expect(customerInput).toBeDefined()
    await user.type(customerInput!, 'KH02')
    await user.keyboard('{Enter}')
    expect(await screen.findByText('KH02 — Khách hai')).toBeInTheDocument()
    expect(quoteCalls(fetchMock)).toHaveLength(quotesBeforeTyping)
    expect(screen.getByLabelText('Đơn giá, dòng 1')).toHaveValue('95000')
  })

  it('đổi khách hỏi lại giá cho dòng chưa gõ tay với partner_id mới; chọn bảng giá ép thì gửi price_list_id', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/partners': {
        status: 200,
        body: {
          items: [catalogRow(CUSTOMER_ID, 'KH01', 'CT CP Xây dựng Nam Long'), catalogRow(602, 'KH02', 'Khách hai')],
          total: 2,
        },
      },
      '/partners/602/overview': OVERVIEW_ROUTE,
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    await pickCustomerAndOperation(user)
    await typeItemAndQuantity(user)
    await waitFor(() => {
      expect(screen.getByLabelText('Đơn giá, dòng 1')).toHaveValue('100000')
    })
    const before = quoteCalls(fetchMock).length

    await user.click(screen.getByRole('button', { name: 'Bỏ chọn' }))
    const customerInput = (await screen.findAllByLabelText('Khách hàng *'))[0]
    await user.type(customerInput!, 'KH02')
    await user.keyboard('{Enter}')
    await waitFor(() => {
      expect(quoteCalls(fetchMock).length).toBeGreaterThan(before)
    })
    const afterCustomer = quoteCalls(fetchMock).at(-1)
    expect((afterCustomer?.lines as Record<string, unknown>[])[0]).toMatchObject({ item_id: ITEM_ID, partner_id: 602 })

    await user.click(screen.getByRole('button', { name: /Mở rộng/ }))
    const priceListInput = screen.getByLabelText('Bảng giá')
    await user.type(priceListInput, 'BG-DL')
    await user.keyboard('{Enter}')
    await waitFor(() => {
      const last = quoteCalls(fetchMock).at(-1)
      expect((last?.lines as Record<string, unknown>[])[0]).toMatchObject({ price_list_id: PRICE_LIST_ID })
    })
  })

  it('kết quả hỏi giá về muộn cho mã hàng CŨ không đè giá của mã hàng mới', async () => {
    // Mã hàng đầu tiên (VT0231) trả lời chậm 150ms với giá 100.000; mã thứ hai
    // (VT0999) trả lời ngay với 200.000 — đáp án cũ về sau phải bị bỏ.
    const resolvers: (() => void)[] = []
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/items': {
        status: 200,
        body: {
          items: [catalogRow(ITEM_ID, 'VT0231', 'Thép hộp 40x80'), catalogRow(7002, 'VT0999', 'Thép tấm')],
          total: 2,
        },
      },
    })
    const original = fetchMock.getMockImplementation() as (url: string, init?: RequestInit) => Promise<Response>
    // `vi.fn` của `mockServer` khai kiểu trả `void`; ở đây ta cần trả `Promise<Response>` như fetch thật.
    // eslint-disable-next-line @typescript-eslint/no-misused-promises
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (String(url).endsWith('/pricing/quote-batch')) {
        const lines = (parseJsonBody(init).lines ?? []) as Record<string, unknown>[]
        const price = lines[0]?.item_id === ITEM_ID ? '100000' : '200000'
        const reply = new Response(
          JSON.stringify({
            items: lines.map(() => ({
              unit_price: price,
              quoted_price: price,
              is_tax_inclusive: false,
              source: 'item_default',
              price_list_id: null,
              level: 1,
              discount_percent: '0',
            })),
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
        if (lines[0]?.item_id === ITEM_ID) {
          return new Promise<Response>((resolve) => {
            resolvers.push(() => {
              resolve(reply)
            })
          })
        }
        return Promise.resolve(reply)
      }
      return original(url, init)
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    await pickCustomerAndOperation(user)
    const itemCell = await screen.findByLabelText('Mã hàng, dòng 1')
    await user.click(itemCell)
    await user.type(itemCell, 'VT0231')
    await user.keyboard('{Tab}')
    await waitFor(() => {
      expect(resolvers).toHaveLength(1)
    })
    // Đổi sang mã thứ hai trong lúc câu hỏi thứ nhất còn treo.
    await user.click(within(screen.getByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })).getByText('VT0231'))
    const again = await screen.findByLabelText('Mã hàng, dòng 1')
    await user.clear(again)
    await user.type(again, 'VT0999')
    await user.keyboard('{Tab}')
    const grid = screen.getByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })
    expect(await within(grid).findByText('200000')).toBeInTheDocument()
    // Giờ đáp án cũ mới về — ô đơn giá phải vẫn là giá của mã mới.
    resolvers[0]!()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(within(grid).getByText('200000')).toBeInTheDocument()
    expect(within(grid).queryByText('100000')).not.toBeInTheDocument()
  })

  it('hỏi giá lỗi (thiếu quyền bảng giá): băng cảnh báo riêng, form vẫn cất được và băng lỗi chứng từ không bị đụng', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/pricing/quote-batch': {
        status: 403,
        body: {
          type: 'https://konek.vn/errors/auth.forbidden',
          title: 'auth.forbidden',
          status: 403,
          detail: 'Không có quyền',
          error_code: 'auth.forbidden',
        },
      },
      '/sales/invoices': (init) => (init?.method === 'POST' ? { status: 201, body: CREATED_INVOICE } : EMPTY_INVOICE_LIST),
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    await pickCustomerAndOperation(user)
    await typeItemAndQuantity(user)
    expect(await screen.findByText(/Không lấy được giá tự động/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    const priceCell = screen.getByLabelText('Đơn giá, dòng 1')
    await user.type(priceCell, '90000')
    await typeAmounts(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (entry) => String(entry[0]).endsWith('/api/v1/sales/invoices') && (entry[1] as RequestInit).method === 'POST',
        ),
      ).toBe(true)
    })
  })

  it('không tầng giá nào trả lời (source=none): đơn giá để trống, nguồn ghi "chưa khai giá"', async () => {
    mockServer({ ...formRoutes(), '/pricing/quote-batch': quoteReply('none') })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    await pickCustomerAndOperation(user)
    await typeItemAndQuantity(user)

    const grid = screen.getByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })
    expect(await within(grid).findByText('chưa khai giá')).toBeInTheDocument()
    expect(screen.getByLabelText('Đơn giá, dòng 1')).toHaveValue('')
  })

  it('422 toàn cảnh báo (ngưỡng nợ khách): băng "Vẫn ghi sổ" gửi lại với acknowledge_warnings=true', async () => {
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
            code: 'receivables.credit_limit_exceeded',
            message: 'Khách vượt ngưỡng nợ 500.000.000.',
            details: { warning: 1 },
          },
        ],
      },
    }
    const fetchMock = mockServer({
      ...formRoutes(),
      '/sales/invoices': (init, url) =>
        init?.method !== 'POST'
          ? EMPTY_INVOICE_LIST
          : String(url).includes('acknowledge_warnings=true')
            ? { status: 201, body: CREATED_INVOICE }
            : warningReply,
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=0')
    await pickCustomerAndOperation(user)
    await typeItemAndQuantity(user)
    await typeAmounts(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(await screen.findByText('Chứng từ chưa ghi sổ — có cảnh báo cần xác nhận:')).toBeInTheDocument()
    expect(screen.getByText('Khách vượt ngưỡng nợ 500.000.000.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Vẫn ghi sổ' }))

    await waitFor(() => {
      expect(fetchMock.mock.calls.some((entry) => String(entry[0]).includes('acknowledge_warnings=true'))).toBe(true)
    })
  })

  it('form sửa dựng lại lưới (giá, CK, nguồn) và tổng server; KHÔNG hỏi lại bộ định giá', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      [`/sales/invoices/${INVOICE_ID}`]: { status: 200, body: CREATED_INVOICE },
    })

    renderFeatureAt(`/ban-hang/chung-tu/${INVOICE_ID}`)
    expect(await screen.findByRole('heading', { name: 'Chứng từ BH26-00001' })).toBeInTheDocument()

    const lines = await screen.findByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    expect(within(lines).getByText('Thép hộp 40x80')).toBeInTheDocument()
    expect(within(lines).getByText('100000')).toBeInTheDocument()
    expect(within(lines).getByText('5')).toBeInTheDocument()
    expect(within(lines).getByText('bảng giá')).toBeInTheDocument()
    expect(quoteCalls(fetchMock)).toHaveLength(0)

    const summary = screen.getByRole('region', { name: 'Tổng chứng từ' })
    expect(summary).toHaveTextContent('950.000')
    expect(summary).toHaveTextContent('50.000')
    expect(summary).toHaveTextContent('95.000')
    expect(summary).toHaveTextContent('1.045.000')
    expect(screen.getByText('KH01 — CT CP Xây dựng Nam Long')).toBeInTheDocument()
    expect(screen.getByText('BG-DL — Bảng giá đại lý')).toBeInTheDocument()
  })

  it('sửa rồi Cất: PUT vọng lại TRỌN thân — NV bán không tra được, giá vốn, quy cách, order_id, nguồn giá, row_version', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      [`/sales/invoices/${INVOICE_ID}`]: (init) =>
        init?.method === 'PUT' ? { status: 200, body: { ...CREATED_INVOICE, row_version: 2 } } : { status: 200, body: CREATED_INVOICE },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/ban-hang/chung-tu/${INVOICE_ID}`)
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    await user.type(screen.getByLabelText('Diễn giải'), 'sửa lần 2')
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => (entry[1] as RequestInit | undefined)?.method === 'PUT')
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({
        row_version: 1,
        kind: 0,
        operation_code: 'ban-hang-hoa',
        customer_id: CUSTOMER_ID,
        receivable_account_id: 131,
        salesperson_id: 88,
        ship_to: '12 Nguyễn Huệ',
        recipient_name: 'Anh Long',
        payment_term_id: 5,
        due_date: '2026-10-25',
        price_list_id: PRICE_LIST_ID,
        is_stock_issue: true,
        description: 'sửa lần 2',
        settlements: [],
      })
      const lines = body.lines as Record<string, unknown>[]
      expect(lines).toHaveLength(1)
      expect(lines[0]).toMatchObject({
        item_id: ITEM_ID,
        unit_id: UNIT_ID,
        warehouse_id: WAREHOUSE_ID,
        variant_id: 501,
        quantity: '10',
        unit_price_fc: '100000',
        discount_percent: '5',
        discount_amount_fc: '50000',
        amount_fc: '950000',
        vat_rate: '10',
        vat_amount_fc: '95000',
        account_id: 5111,
        vat_account_id: 33311,
        cogs_account_id: 632,
        inventory_account_id: 156,
        unit_cost_fc: '80000',
        price_list_id: PRICE_LIST_ID,
        price_source: 'price_list',
        order_id: 77,
        extended: [{ dimension_id: 3, value_id: 31 }],
      })
    })
  })

  it('PUT bị 409 (người khác đã sửa): báo lỗi và tải lại chứng từ', async () => {
    let reads = 0
    mockServer({
      ...formRoutes(),
      [`/sales/invoices/${INVOICE_ID}`]: (init) => {
        if (init?.method === 'PUT') {
          return {
            status: 409,
            body: {
              type: 'https://konek.vn/errors/conflict.row_version',
              title: 'conflict.row_version',
              status: 409,
              detail: 'Chứng từ đã được người khác sửa',
              error_code: 'conflict.row_version',
            },
          }
        }
        reads += 1
        return { status: 200, body: CREATED_INVOICE }
      },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/ban-hang/chung-tu/${INVOICE_ID}`)
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    const readsBeforeSave = reads
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await waitFor(() => {
      expect(reads).toBeGreaterThan(readsBeforeSave)
    })
  })

  it('trả lại hàng (kind=2): khối đối trừ đọc hóa đơn gốc của khách và gửi settlements; TK phải thu là bên Có', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/sales/open-invoices': {
        status: 200,
        body: {
          items: [
            {
              target_kind: 0,
              target_id: DEBT_ID,
              document_id: ORIGINAL_ID,
              invoice_no: 'BH26-00001',
              invoice_date: '2026-09-10',
              due_date: '2026-10-25',
              currency_code: 'VND',
              amount_fc: '1100000',
              remaining_fc: '1100000',
            },
          ],
        },
      },
      '/sales/invoices': (init) =>
        init?.method === 'POST' ? { status: 201, body: { ...CREATED_INVOICE, kind: 2 } } : EMPTY_INVOICE_LIST,
    })
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=2')
    expect(await screen.findByRole('heading', { name: 'Tạo chứng từ bán hàng — Hàng bán bị trả lại' })).toBeInTheDocument()

    const customerInput = await screen.findByLabelText('Khách hàng *')
    await user.type(customerInput, 'KH01')
    await user.keyboard('{Enter}')
    // Chỉ nghiệp vụ có bên Có là TK theo dõi khách mới hợp — bán hàng thường bị loại.
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Hàng bán bị trả lại' })).toBeInTheDocument()
    })
    expect(screen.queryByRole('option', { name: 'Bán hàng hóa trong nước' })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Nghiệp vụ *'), 'tra-lai-hang-ban')

    await waitFor(() => {
      const url = fetchMock.mock.calls.map((entry) => String(entry[0])).find((entry) => entry.includes('/sales/open-invoices'))
      expect(url).toBeDefined()
      expect(url).toContain(`customer_id=${String(CUSTOMER_ID)}`)
      expect(url).toContain('branch_id=1')
      expect(url).not.toContain('side=')
    })
    await user.type(await screen.findByLabelText('Số đối trừ cho BH26-00001'), '330000')
    expect(screen.getByText('Đang đối trừ 1 hóa đơn gốc.')).toBeInTheDocument()

    await typeItemAndQuantity(user)
    await typeAmounts(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/api/v1/sales/invoices') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({
        kind: 2,
        operation_code: 'tra-lai-hang-ban',
        receivable_account_id: 131,
        settlements: [{ target_kind: 0, target_id: DEBT_ID, amount_fc: '330000' }],
      })
      const lines = body.lines as Record<string, unknown>[]
      expect(lines[0]).toMatchObject({ account_id: 521 })
    })
  })

  it('điều chỉnh giảm (kind=6) qua deep-link: thẻ nêu chứng từ gốc, POST mang adjusts_voucher_id + đối trừ', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/sales/open-invoices': { status: 200, body: { items: [] } },
      [`/sales/invoices/${ORIGINAL_ID}`]: { status: 200, body: { ...CREATED_INVOICE, id: ORIGINAL_ID, voucher_no: 'BH26-00007' } },
      '/sales/invoices': (init) =>
        init?.method === 'POST' ? { status: 201, body: { ...CREATED_INVOICE, kind: 6 } } : EMPTY_INVOICE_LIST,
    })
    const user = userEvent.setup()

    renderFeatureAt(`/ban-hang/chung-tu/moi?kind=6&adjusts_voucher_id=${ORIGINAL_ID}`)
    expect(await screen.findByText('Chứng từ này mang phần chênh của chứng từ bán BH26-00007.')).toBeInTheDocument()
    expect(screen.queryByText(/chỉ lập từ luồng xử lý hóa đơn sai sót/)).not.toBeInTheDocument()

    const customerInput = await screen.findByLabelText('Khách hàng *')
    await user.type(customerInput, 'KH01')
    await user.keyboard('{Enter}')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Điều chỉnh giảm hóa đơn đã phát hành' })).toBeInTheDocument()
    })
    await user.selectOptions(screen.getByLabelText('Nghiệp vụ *'), 'dieu-chinh-giam-hoa-don')
    await typeItemAndQuantity(user)
    await typeAmounts(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/api/v1/sales/invoices') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toMatchObject({ kind: 6, adjusts_voucher_id: ORIGINAL_ID, settlements: [] })
    })
  })

  it('mở chứng từ điều chỉnh đã lưu: thẻ nêu tờ gốc và PUT vọng lại adjusts_voucher_id', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/sales/open-invoices': { status: 200, body: { items: [] } },
      [`/sales/invoices/${ORIGINAL_ID}`]: { status: 200, body: { ...CREATED_INVOICE, id: ORIGINAL_ID, voucher_no: 'BH26-00007' } },
      [`/sales/invoices/${INVOICE_ID}`]: (init) =>
        init?.method === 'PUT'
          ? { status: 200, body: { ...CREATED_INVOICE, kind: 6, adjusts_voucher_id: ORIGINAL_ID, row_version: 2 } }
          : { status: 200, body: { ...CREATED_INVOICE, kind: 6, adjusts_voucher_id: ORIGINAL_ID, operation_code: 'dieu-chinh-giam-hoa-don' } },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/ban-hang/chung-tu/${INVOICE_ID}`)
    expect(await screen.findByText('Chứng từ này mang phần chênh của chứng từ bán BH26-00007.')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    await user.click(screen.getByRole('button', { name: 'Cất' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => (entry[1] as RequestInit | undefined)?.method === 'PUT')
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toMatchObject({ kind: 6, adjusts_voucher_id: ORIGINAL_ID, row_version: 1 })
    })
  })

  it('điều chỉnh (kind=5) mở tay không có chứng từ gốc: cảnh báo và không cất', async () => {
    const fetchMock = mockServer(formRoutes())
    const user = userEvent.setup()

    renderFeatureAt('/ban-hang/chung-tu/moi?kind=5')
    expect(await screen.findByText(/chỉ lập từ luồng xử lý hóa đơn sai sót/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cất' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('phải có chứng từ gốc')
    expect(fetchMock.mock.calls.some((entry) => (entry[1] as RequestInit | undefined)?.method === 'POST' && String(entry[0]).endsWith('/sales/invoices'))).toBe(false)
  })

  it('xóa vướng tờ HĐĐT nháp: câu tiếng Việt nói bước phải làm + liên kết sang nhóm 02', async () => {
    mockServer({
      ...formRoutes(),
      [`/sales/invoices/${INVOICE_ID}`]: { status: 200, body: CREATED_INVOICE },
      [`/vouchers/${INVOICE_ID}`]: {
        status: 422,
        body: {
          type: 'https://konek.vn/errors/data.reference_not_found',
          title: 'data.reference_not_found',
          status: 422,
          detail: 'Bản ghi được tham chiếu không tồn tại',
          error_code: 'data.reference_not_found',
          details: { constraint: 'fk_einvoices_source_voucher' },
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/ban-hang/chung-tu/${INVOICE_ID}`)
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    await user.click(screen.getByRole('button', { name: 'Xóa' }))
    await user.click(screen.getByRole('button', { name: 'Bấm lần nữa để xóa hẳn' }))

    expect(
      await screen.findByText(/Chứng từ còn tờ hóa đơn điện tử nháp. Hủy tờ nháp ở Hóa đơn điện tử trước/),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Mở hóa đơn điện tử của chứng từ' })).toHaveAttribute(
      'href',
      `/hoa-don-dien-tu?source_voucher_id=${INVOICE_ID}`,
    )
  })
})
