/**
 * Form hóa đơn mua: nghiệp vụ điền sẵn TK phải trả + TK dòng, POST đủ thân
 * hóa đơn kèm khóa chống trùng; vòng "Vẫn ghi sổ?" cho cảnh báo ngưỡng nợ NCC
 * (FR-SYS-032/062); form sửa dựng lại lưới, chi phí mua và cách phân bổ; tờ
 * trả lại hàng đối trừ hóa đơn gốc qua `/purchase/open-invoices`.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

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

import {
  catalogRow,
  renderFeatureAt,
} from './feature-test-utils'

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
      account(156, '156', 'Hàng hóa'),
      account(331, '331', 'Phải trả người bán', ['vendor']),
      account(1331, '1331', 'Thuế GTGT được khấu trừ'),
      account(1111, '1111', 'Tiền mặt VND'),
      account(33, '33', 'Nợ phải trả (tổng hợp)', null, true),
    ],
  },
}

const OPERATIONS_ROUTE: RouteReply = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      {
        operation_code: 'mua-hang-hoa',
        operation_name: 'Mua hàng hóa nhập kho',
        debit_account_code: '156',
        credit_account_code: '331',
        requires_partner: true,
        partner_kind: 1,
        display_order: 1,
      },
      {
        operation_code: 'tra-lai-hang-mua',
        operation_name: 'Trả lại hàng mua',
        debit_account_code: '331',
        credit_account_code: '156',
        requires_partner: true,
        partner_kind: 1,
        display_order: 2,
      },
    ],
  },
}

const VENDOR_ID = 501
const ITEM_ID = 7001
const UNIT_ID = 7101
const WAREHOUSE_ID = 7201
const INVOICE_ID = 'cccccccc-0000-0000-0000-000000000001'
const DEBT_ID = 'dddddddd-0000-0000-0000-000000000001'

const PARTNERS_ROUTE: RouteReply = {
  status: 200,
  body: { items: [catalogRow(VENDOR_ID, 'NCC01', 'CT TNHH Thép Việt Nhật')], total: 1 },
}

const OVERVIEW_ROUTE: RouteReply = {
  status: 200,
  body: {
    partner: {
      ...catalogRow(VENDOR_ID, 'NCC01', 'CT TNHH Thép Việt Nhật'),
      is_customer: false,
      is_vendor: true,
      is_organization: true,
      tax_code: '0301447712',
      payment_term_id: null,
    },
    debt: { receivable: null, payable: null, credit_limit: null, credit_available: null },
  },
}

const CREATED_INVOICE = {
  id: INVOICE_ID,
  document_type: 'PUR',
  voucher_no: 'MH26-00001',
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
  operation_code: 'mua-hang-hoa',
  vendor_id: VENDOR_ID,
  buyer_id: 88,
  vendor_invoice_status: 0,
  vendor_invoice_form: '1',
  vendor_invoice_serial: 'C26TVN',
  vendor_invoice_no: '0009120',
  vendor_invoice_date: '2026-09-18',
  payment_term_id: 5,
  due_date: '2026-10-25',
  payable_account_id: 331,
  landed_cost_allocation: 2,
  total_before_tax_fc: '1000000',
  total_vat_fc: '100000',
  total_landed_cost_fc: '50000',
  total_fc: '1150000',
  lines: [
    {
      id: 'eeeeeeee-0000-0000-0000-000000000001',
      line_no: 1,
      description: 'Thép hộp',
      item_id: ITEM_ID,
      unit_id: UNIT_ID,
      warehouse_id: WAREHOUSE_ID,
      quantity: '10',
      unit_price_fc: '100000',
      amount_fc: '1000000',
      vat_rate: '10',
      vat_amount_fc: '100000',
      landed_cost_fc: '50000',
      account_id: 156,
      vat_account_id: 1331,
      cost_object_id: null,
      project_id: null,
      order_id: 77,
      contract_id: null,
      expense_item_id: null,
      extended_dimensions: { '3': 31 },
    },
  ],
  landed_costs: [
    {
      id: 'ffffffff-0000-0000-0000-000000000001',
      line_no: 1,
      description: 'Vận chuyển',
      vendor_id: VENDOR_ID,
      credit_account_id: 1111,
      amount_fc: '50000',
      vat_rate: null,
      vat_amount_fc: '0',
      vat_account_id: null,
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
    [`/partners/${String(VENDOR_ID)}/overview`]: OVERVIEW_ROUTE,
    '/purchase/pending-issues': { status: 200, body: { side: 'purchase', as_of: '2026-09-18', groups: [] } },
  }
}

async function pickVendorAndOperation(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const vendorInput = await screen.findByLabelText('Nhà cung cấp *')
  await user.type(vendorInput, 'NCC01')
  await user.keyboard('{Enter}')
  const operationSelect = screen.getByLabelText('Nghiệp vụ *')
  await waitFor(() => {
    expect(screen.getByRole('option', { name: 'Mua hàng hóa nhập kho' })).toBeInTheDocument()
  })
  await user.selectOptions(operationSelect, 'mua-hang-hoa')
}

/** Ô sống duy nhất của lưới là ô đang chọn (0,0 = "Mã hàng") — đi bằng Tab qua các cột nhập. */
async function fillFirstLine(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const itemCell = await screen.findByLabelText('Mã hàng, dòng 1')
  await user.click(itemCell)
  await user.type(itemCell, 'VT0231')
  await user.keyboard('{Tab}') // → ĐVT
  await user.keyboard('{Tab}') // → Kho
  await user.keyboard('{Tab}') // → Diễn giải
  await user.keyboard('{Tab}') // → Số lượng
  await user.keyboard('{Tab}') // → Đơn giá
  await user.keyboard('{Tab}') // → Thành tiền
  const amountCell = await screen.findByLabelText('Thành tiền, dòng 1')
  await user.type(amountCell, '1000000')
  await user.keyboard('{Tab}') // → Thuế %
  await user.keyboard('{Tab}') // → Tiền thuế
  const vatCell = await screen.findByLabelText('Tiền thuế, dòng 1')
  await user.type(vatCell, '100000')
  await user.keyboard('{Tab}')
}

describe('form hóa đơn mua hàng', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('nghiệp vụ điền sẵn TK phải trả + TK dòng; Cất gửi đủ thân hóa đơn kèm khóa chống trùng', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/purchase/invoices': (init) =>
        init?.method === 'POST'
          ? { status: 201, body: CREATED_INVOICE }
          : EMPTY_INVOICE_LIST,
    })
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu/moi?kind=0')
    expect(await screen.findByRole('heading', { name: 'Tạo chứng từ mua hàng — Mua hàng hóa' })).toBeInTheDocument()
    await pickVendorAndOperation(user)
    // Dòng gợi ý dưới ô NCC đọc MST từ thẻ đối tác.
    expect(await screen.findByText('MST 0301447712')).toBeInTheDocument()
    await fillFirstLine(user)

    // TK phải trả ở khối Mở rộng đã được nghiệp vụ điền (Có 331).
    await user.click(screen.getByRole('button', { name: /Mở rộng/ }))
    expect(screen.getByText('331 — Phải trả người bán')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/api/v1/purchase/invoices') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(new Headers(init.headers).get(IDEMPOTENCY_HEADER)).toBeTruthy()
      const body = parseJsonBody(init)
      expect(body).toMatchObject({
        kind: 0,
        operation_code: 'mua-hang-hoa',
        vendor_id: VENDOR_ID,
        payable_account_id: 331,
        branch_id: 1,
        vendor_invoice_status: 0,
        landed_cost_allocation: 0,
        landed_costs: [],
        settlements: [],
      })
      const lines = body.lines as Record<string, unknown>[]
      expect(lines).toHaveLength(1)
      expect(lines[0]).toMatchObject({
        item_id: ITEM_ID,
        amount_fc: '1000000',
        vat_amount_fc: '100000',
        landed_cost_fc: '0',
        account_id: 156,
      })
    })
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Chứng từ mua hàng' })).toBeInTheDocument()
    })
  })

  it('422 toàn cảnh báo (ngưỡng nợ NCC): băng "Vẫn ghi sổ" gửi lại với acknowledge_warnings=true', async () => {
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
            message: 'NCC vượt ngưỡng nợ 500.000.000.',
            details: { warning: 1 },
          },
        ],
      },
    }
    const fetchMock = mockServer({
      ...formRoutes(),
      '/purchase/invoices': (init, url) =>
        init?.method !== 'POST'
          ? EMPTY_INVOICE_LIST
          : String(url).includes('acknowledge_warnings=true')
            ? { status: 201, body: CREATED_INVOICE }
            : warningReply,
    })
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu/moi?kind=0')
    await pickVendorAndOperation(user)
    await fillFirstLine(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(await screen.findByText('Chứng từ chưa ghi sổ — có cảnh báo cần xác nhận:')).toBeInTheDocument()
    expect(screen.getByText('NCC vượt ngưỡng nợ 500.000.000.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Vẫn ghi sổ' }))

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((entry) => String(entry[0]).includes('acknowledge_warnings=true')),
      ).toBe(true)
    })
  })

  it('form sửa dựng lại lưới hàng, chi phí mua, cách phân bổ thủ công và tổng server', async () => {
    mockServer({
      ...formRoutes(),
      [`/purchase/invoices/${INVOICE_ID}`]: { status: 200, body: CREATED_INVOICE },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/mua-hang/chung-tu/${INVOICE_ID}`)
    expect(await screen.findByRole('heading', { name: 'Chứng từ MH26-00001' })).toBeInTheDocument()

    const lines = await screen.findByRole('grid', { name: 'Dòng hàng hóa, dịch vụ' })
    // Ô (0,0) là ô đang chọn nên là một <input> mang giá trị; các ô khác là chữ.
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    expect(within(lines).getByText('Thép hộp 40x80')).toBeInTheDocument()
    expect(within(lines).getByText('KHCM')).toBeInTheDocument()
    expect(within(lines).getByText('Cay')).toBeInTheDocument()
    expect(within(lines).getByText('50000')).toBeInTheDocument()

    const costs = screen.getByRole('grid', { name: 'Khoản chi phí mua hàng' })
    expect(screen.getByLabelText('Khoản chi phí, chi phí 1')).toHaveValue('Vận chuyển')
    expect(within(costs).getByText('1111')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Thủ công' })).toBeChecked()

    // Phân bổ thủ công → cột "Chi phí mua" trên dòng mở khóa: bấm vào là ra ô nhập.
    await user.click(within(lines).getByText('50000'))
    expect(await screen.findByLabelText('Chi phí mua, dòng 1')).toBeInTheDocument()

    // Bốn con số của thẻ tổng là của server.
    const summary = screen.getByRole('region', { name: 'Tổng chứng từ' })
    expect(summary).toHaveTextContent('1.000.000')
    expect(summary).toHaveTextContent('50.000')
    expect(summary).toHaveTextContent('100.000')
    expect(summary).toHaveTextContent('1.150.000')
    expect(screen.getByText('NCC01 — CT TNHH Thép Việt Nhật')).toBeInTheDocument()
  })


  it('sửa rồi Cất: PUT vọng lại TRỌN thân hóa đơn — người mua không tra được, order_id, chiều mở rộng, NCC chi phí, row_version', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      [`/purchase/invoices/${INVOICE_ID}`]: (init) =>
        init?.method === 'PUT'
          ? { status: 200, body: { ...CREATED_INVOICE, row_version: 2 } }
          : { status: 200, body: CREATED_INVOICE },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/mua-hang/chung-tu/${INVOICE_ID}`)
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    // Chỉ đổi diễn giải — mọi thứ khác phải đi lên y như đã lưu.
    const description = screen.getByLabelText('Diễn giải')
    await user.type(description, 'sửa lần 2')
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => (entry[1] as RequestInit | undefined)?.method === 'PUT')
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({
        row_version: 1,
        kind: 0,
        operation_code: 'mua-hang-hoa',
        vendor_id: VENDOR_ID,
        payable_account_id: 331,
        // Nhân viên 88 không có trong danh mục (đã ngừng) — vẫn vọng lại, không thành null.
        buyer_id: 88,
        vendor_invoice_status: 0,
        vendor_invoice_form: '1',
        vendor_invoice_serial: 'C26TVN',
        vendor_invoice_no: '0009120',
        vendor_invoice_date: '2026-09-18',
        payment_term_id: 5,
        due_date: '2026-10-25',
        landed_cost_allocation: 2,
        description: 'sửa lần 2',
        settlements: [],
      })
      const lines = body.lines as Record<string, unknown>[]
      expect(lines).toHaveLength(1)
      expect(lines[0]).toMatchObject({
        item_id: ITEM_ID,
        unit_id: UNIT_ID,
        warehouse_id: WAREHOUSE_ID,
        quantity: '10',
        unit_price_fc: '100000',
        amount_fc: '1000000',
        vat_rate: '10',
        vat_amount_fc: '100000',
        // Phân bổ thủ công → số đã lưu đi theo dòng.
        landed_cost_fc: '50000',
        account_id: 156,
        vat_account_id: 1331,
        order_id: 77,
        extended: [{ dimension_id: 3, value_id: 31 }],
      })
      const costs = body.landed_costs as Record<string, unknown>[]
      expect(costs).toEqual([
        {
          description: 'Vận chuyển',
          vendor_id: VENDOR_ID,
          credit_account_id: 1111,
          amount_fc: '50000',
          vat_rate: null,
          vat_amount_fc: '0',
          vat_account_id: null,
        },
      ])
    })
  })

  it('PUT bị 409 (người khác đã sửa): báo lỗi và tải lại chứng từ', async () => {
    let reads = 0
    const fetchMock = mockServer({
      ...formRoutes(),
      [`/purchase/invoices/${INVOICE_ID}`]: (init) => {
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

    renderFeatureAt(`/mua-hang/chung-tu/${INVOICE_ID}`)
    await waitFor(() => {
      expect(screen.getByLabelText('Mã hàng, dòng 1')).toHaveValue('VT0231')
    })
    const readsBeforeSave = reads
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await waitFor(() => {
      expect(reads).toBeGreaterThan(readsBeforeSave)
    })
    expect(fetchMock.mock.calls.some((entry) => (entry[1] as RequestInit | undefined)?.method === 'PUT')).toBe(true)
  })

  it('mở tờ trả lại đã lưu: khối đối trừ hiện đúng số đã gõ trên hóa đơn gốc', async () => {
    mockServer({
      ...formRoutes(),
      '/purchase/open-invoices': {
        status: 200,
        body: {
          items: [
            {
              target_kind: 1,
              target_id: DEBT_ID,
              document_id: INVOICE_ID,
              invoice_no: 'MH26-00001',
              invoice_date: '2026-09-10',
              due_date: '2026-10-25',
              currency_code: 'VND',
              amount_fc: '1100000',
              remaining_fc: '770000',
            },
          ],
        },
      },
      [`/purchase/invoices/${INVOICE_ID}`]: {
        status: 200,
        body: {
          ...CREATED_INVOICE,
          kind: 4,
          operation_code: 'tra-lai-hang-mua',
          landed_costs: [],
          settlements: [{ id: 'aaaaaaaa-1111-0000-0000-000000000001', target_kind: 1, target_id: DEBT_ID, amount_fc: '330000', amount: '330000', fx_diff: '0' }],
        },
      },
    })

    renderFeatureAt(`/mua-hang/chung-tu/${INVOICE_ID}`)
    expect(await screen.findByLabelText('Số đối trừ cho MH26-00001')).toHaveValue('330000')
    expect(screen.getByText('Đang đối trừ 1 hóa đơn gốc.')).toBeInTheDocument()
    expect(screen.queryByRole('grid', { name: 'Khoản chi phí mua hàng' })).not.toBeInTheDocument()
  })

  it('trả lại hàng (kind=4): khối đối trừ đọc hóa đơn gốc của NCC và gửi settlements', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/purchase/open-invoices': {
        status: 200,
        body: {
          items: [
            {
              target_kind: 1,
              target_id: DEBT_ID,
              document_id: INVOICE_ID,
              invoice_no: 'MH26-00001',
              invoice_date: '2026-09-10',
              due_date: '2026-10-25',
              currency_code: 'VND',
              amount_fc: '1100000',
              remaining_fc: '1100000',
            },
          ],
        },
      },
      '/purchase/invoices': (init) =>
        init?.method === 'POST'
          ? { status: 201, body: { ...CREATED_INVOICE, kind: 4 } }
          : EMPTY_INVOICE_LIST,
    })
    const user = userEvent.setup()

    renderFeatureAt('/mua-hang/chung-tu/moi?kind=4')
    expect(await screen.findByRole('heading', { name: 'Tạo chứng từ mua hàng — Trả lại hàng mua' })).toBeInTheDocument()
    // Không có khối chi phí mua trên tờ trả lại.
    expect(screen.queryByRole('grid', { name: 'Khoản chi phí mua hàng' })).not.toBeInTheDocument()

    const vendorInput = await screen.findByLabelText('Nhà cung cấp *')
    await user.type(vendorInput, 'NCC01')
    await user.keyboard('{Enter}')
    // Tờ trả lại chỉ có MỘT nghiệp vụ hợp (bên phải trả là bên Nợ) → chọn sẵn,
    // và nghiệp vụ mua không có trong danh sách (review 7H-1 M-3).
    await waitFor(() => {
      expect(screen.getByLabelText('Nghiệp vụ *')).toHaveValue('tra-lai-hang-mua')
    })
    expect(screen.queryByRole('option', { name: 'Mua hàng hóa nhập kho' })).not.toBeInTheDocument()

    await waitFor(() => {
      const url = fetchMock.mock.calls.map((entry) => String(entry[0])).find((entry) => entry.includes('/purchase/open-invoices'))
      expect(url).toBeDefined()
      expect(url).toContain(`vendor_id=${String(VENDOR_ID)}`)
      expect(url).toContain('branch_id=1')
      expect(url).not.toContain('side=')
    })
    await user.type(await screen.findByLabelText('Số đối trừ cho MH26-00001'), '330000')

    await fillFirstLine(user)
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/api/v1/purchase/invoices') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({
        kind: 4,
        operation_code: 'tra-lai-hang-mua',
        // Trả lại đảo chiều: bên phải trả là bên Nợ của nghiệp vụ.
        payable_account_id: 331,
        settlements: [{ target_kind: 1, target_id: DEBT_ID, amount_fc: '330000' }],
      })
      const lines = body.lines as Record<string, unknown>[]
      expect(lines[0]).toMatchObject({ account_id: 156 })
    })
  })
})
