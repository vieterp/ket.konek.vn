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
      {
        id: 10,
        code: '111',
        name: 'Tiền mặt (tổng hợp)',
        balance_nature: 1,
        detail_tracking: null,
        is_summary: true,
        is_foreign_currency: false,
        level: 1,
        parent_id: null,
      },
      {
        id: 31,
        code: '131',
        name: 'Phải thu khách hàng',
        balance_nature: 1,
        detail_tracking: ['customer'],
        is_summary: false,
        is_foreign_currency: false,
        level: 1,
        parent_id: null,
      },
    ],
  },
}

/** Một bản ghi danh mục đối tác/nhân viên tối thiểu cho lookup. */
function catalogRow(id: number, code: string, name: string): Record<string, unknown> {
  return {
    id,
    uid: `uid-${String(id)}`,
    code,
    name,
    name_en: null,
    parent_id: null,
    path: String(id),
    level: 0,
    is_group: false,
    is_active: true,
    branch_id: null,
    row_version: 1,
  }
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
      {
        operation_code: 'thu-cong-no-kh',
        operation_name: 'Thu công nợ khách hàng',
        debit_account_code: '1111',
        credit_account_code: '131',
        requires_partner: true,
        partner_kind: 0,
        display_order: 2,
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

  it('TK tổng hợp trên lưới: chặn ngay trên form, không POST', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/cash-book/vouchers': { status: 201, body: CREATED_VOUCHER },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    const operationSelect = await screen.findByLabelText('Nghiệp vụ')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Thu khác' })).toBeInTheDocument()
    })
    await user.selectOptions(operationSelect, 'thu-khac')
    const cashInput = screen.getByLabelText('TK quỹ')
    await user.type(cashInput, '1111')
    await user.keyboard('{Enter}')

    // Gõ đè TK tổng hợp "111" vào ô TK Nợ (đang giữ giá trị prefill).
    const firstCell = await screen.findByLabelText('TK Nợ, dòng 1')
    await user.click(firstCell)
    await user.clear(firstCell)
    await user.type(firstCell, '111')
    await user.keyboard('{Tab}') // TK Nợ → TK Có
    await user.keyboard('{Tab}') // TK Có → Diễn giải
    await user.keyboard('{Tab}') // Diễn giải → Số tiền
    const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
    await user.type(amountInput, '1000000')
    await user.keyboard('{Tab}')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(
      await screen.findByText('Dòng 1: TK "111" là TK tổng hợp, không định khoản trực tiếp.'),
    ).toBeInTheDocument()
    const posted = fetchMock.mock.calls.find(
      (entry) =>
        String(entry[0]).endsWith('/cash-book/vouchers') &&
        (entry[1] as RequestInit | undefined)?.method === 'POST',
    )
    expect(posted).toBeUndefined()
  })

  it('nghiệp vụ KHÔNG ghi đè mã TK người dùng đã gõ vào dòng đầu', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/cash-book/vouchers': { status: 201, body: CREATED_VOUCHER },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    // Người dùng gõ TK Nợ TRƯỚC khi chọn nghiệp vụ.
    const firstCell = await screen.findByLabelText('TK Nợ, dòng 1')
    await user.click(firstCell)
    await user.type(firstCell, '711')
    await user.keyboard('{Tab}') // chốt ô TK Nợ

    const operationSelect = screen.getByLabelText('Nghiệp vụ')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Thu khác' })).toBeInTheDocument()
    })
    await user.selectOptions(operationSelect, 'thu-khac')

    const cashInput = screen.getByLabelText('TK quỹ')
    await user.type(cashInput, '1111')
    await user.keyboard('{Enter}')
    // Tab từ ô TK Có đang chọn tới Số tiền.
    await user.click(screen.getByLabelText('TK Có, dòng 1'))
    await user.keyboard('{Tab}') // TK Có → Diễn giải
    await user.keyboard('{Tab}') // Diễn giải → Số tiền
    const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
    await user.type(amountInput, '1000000')
    await user.keyboard('{Tab}')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/cash-book/vouchers'),
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      // Dòng giữ đúng TK 711 người dùng gõ — nghiệp vụ không được ghi đè.
      expect((body.lines as Record<string, unknown>[])[0]).toMatchObject({
        debit_account_id: 71,
        credit_account_id: null,
      })
    })
  })

  it('chiều của dòng = HỢP hai TK: TK Có đòi đối tượng thì cột hiện và mã được gửi', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/partners': { status: 200, body: { items: [catalogRow(5, 'KH01', 'Công ty A')], total: 1 } },
      '/cash-book/open-invoices': { status: 200, body: { items: [] } },
      '/cash-book/vouchers': { status: 201, body: CREATED_VOUCHER },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    const operationSelect = await screen.findByLabelText('Nghiệp vụ')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Thu công nợ khách hàng' })).toBeInTheDocument()
    })
    await user.selectOptions(operationSelect, 'thu-cong-no-kh')

    const cashInput = screen.getByLabelText('TK quỹ')
    await user.type(cashInput, '1111')
    await user.keyboard('{Enter}')
    // Nghiệp vụ đòi đối tác — chọn ở ô header.
    const partnerInput = screen.getByLabelText('Đối tượng (bắt buộc)')
    await user.type(partnerInput, 'KH01')
    await user.keyboard('{Enter}')

    // TK Có (131) khai chiều customer → cột "Mã đối tượng" phải hiện dù TK Nợ
    // (1111) không đòi chiều nào.
    const firstCell = await screen.findByLabelText('TK Nợ, dòng 1')
    await user.click(firstCell)
    await user.keyboard('{Tab}') // TK Nợ → TK Có
    await user.keyboard('{Tab}') // TK Có → Diễn giải
    await user.keyboard('{Tab}') // Diễn giải → Số tiền
    const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
    await user.type(amountInput, '1000000')
    await user.keyboard('{Tab}') // Số tiền → Mã đối tượng
    const dimInput = await screen.findByLabelText('Mã đối tượng, dòng 1')
    await user.type(dimInput, 'KH01')
    await user.keyboard('{Tab}')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/cash-book/vouchers'),
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({ partner_id: 5, partner_kind: 0 })
      expect((body.lines as Record<string, unknown>[])[0]).toMatchObject({
        partner_id: 5,
        partner_kind: 0,
      })
    })
  })
})

describe('form phiếu chi — sửa và đối trừ', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  const SAVED_PAYMENT = {
    ...CREATED_VOUCHER,
    id: 'aaaaaaaa-0000-0000-0000-000000000042',
    document_type: 'PC',
    voucher_no: 'PC26-00042',
    kind: 1,
    operation_code: 'chi-tam-ung-ngung-hieu-luc',
    partner_id: 99,
    partner_kind: 2,
    lines: [
      {
        id: 'dddddddd-0000-0000-0000-000000000001',
        line_no: 1,
        debit_account_id: 71,
        credit_account_id: 11,
        amount_fc: '500000',
        partner_id: null,
        partner_kind: null,
        cost_object_id: null,
        project_id: null,
        order_id: null,
        contract_id: null,
        expense_item_id: null,
        item_id: null,
        warehouse_id: null,
        extended_dimensions: null,
        description: null,
      },
    ],
  }

  it('sửa phiếu có đối tác NHÂN VIÊN và nghiệp vụ hết hiệu lực: đối tác vẫn hiện và PUT giữ nguyên', async () => {
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/employees': {
        status: 200,
        body: { items: [catalogRow(99, 'NV01', 'Nguyễn Văn A')], total: 1 },
      },
      [`/cash-book/vouchers/${SAVED_PAYMENT.id}`]: { status: 200, body: SAVED_PAYMENT },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/tien-vao-tien-ra/giao-dich/phieu/${SAVED_PAYMENT.id}`)

    // Đối tác nhân viên phải hydrate từ voucher.partner_kind (danh sách
    // operations KHÔNG chứa nghiệp vụ của phiếu — đường tất định của C-1).
    await waitFor(() => {
      expect(screen.getByText('NV01 — Nguyễn Văn A')).toBeInTheDocument()
    })

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) =>
          String(entry[0]).endsWith(`/cash-book/vouchers/${SAVED_PAYMENT.id}`) &&
          (entry[1] as RequestInit | undefined)?.method === 'PUT',
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({ partner_id: 99, partner_kind: 2, row_version: 1 })
      expect((body.lines as Record<string, unknown>[])[0]).toMatchObject({
        debit_account_id: 71,
        credit_account_id: 11,
        amount_fc: '500000',
      })
    })
  })

  it('đối trừ: dòng gõ rồi xóa trắng không được gửi lên server', async () => {
    const invoice1 = {
      target_kind: 2,
      target_id: 'eeeeeeee-0000-0000-0000-000000000011',
      partner_kind: 0,
      partner_id: 5,
      branch_id: 1,
      account_id: 31,
      invoice_no: 'HD-011',
      invoice_date: '2026-07-01',
      due_date: null,
      currency_code: 'VND',
      exchange_rate: '1',
      amount_fc: '3000000',
      remaining_fc: '3000000',
      remaining: '3000000',
      description: null,
    }
    const invoice2 = { ...invoice1, target_id: 'eeeeeeee-0000-0000-0000-000000000012', invoice_no: 'HD-012' }
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/partners': {
        status: 200,
        body: { items: [catalogRow(5, 'KH01', 'Công ty A')], total: 1 },
      },
      '/cash-book/open-invoices': { status: 200, body: { items: [invoice1, invoice2] } },
      '/cash-book/vouchers': { status: 201, body: CREATED_VOUCHER },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    const operationSelect = await screen.findByLabelText('Nghiệp vụ')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Thu công nợ khách hàng' })).toBeInTheDocument()
    })
    await user.selectOptions(operationSelect, 'thu-cong-no-kh')
    const cashInput = screen.getByLabelText('TK quỹ')
    await user.type(cashInput, '1111')
    await user.keyboard('{Enter}')
    const partnerInput = screen.getByLabelText('Đối tượng (bắt buộc)')
    await user.type(partnerInput, 'KH01')
    await user.keyboard('{Enter}')

    const amount1 = await screen.findByLabelText('Số đối trừ cho HD-011')
    await user.type(amount1, '1000000')
    // Gõ rồi XÓA TRẮNG dòng thứ hai — khóa ở lại trong state với chuỗi rỗng.
    const amount2 = screen.getByLabelText('Số đối trừ cho HD-012')
    await user.type(amount2, '5')
    await user.clear(amount2)

    const firstCell = await screen.findByLabelText('TK Nợ, dòng 1')
    await user.click(firstCell)
    await user.keyboard('{Tab}')
    await user.keyboard('{Tab}')
    await user.keyboard('{Tab}')
    const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
    await user.type(amountInput, '1000000')
    await user.keyboard('{Tab}')
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/cash-book/vouchers'),
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      const settlements = body.settlements as Record<string, unknown>[]
      expect(settlements).toHaveLength(1)
      expect(settlements[0]).toMatchObject({
        target_kind: 2,
        target_id: 'eeeeeeee-0000-0000-0000-000000000011',
        amount_fc: '1000000',
      })
    })
  })

  it('đối trừ: chỉ gửi dòng có số; đổi đối tác thì số đã gõ của đối tác cũ không đi theo', async () => {
    const invoiceA = {
      target_kind: 2,
      target_id: 'eeeeeeee-0000-0000-0000-000000000001',
      partner_kind: 0,
      partner_id: 5,
      branch_id: 1,
      account_id: 31,
      invoice_no: 'HD-001',
      invoice_date: '2026-07-01',
      due_date: null,
      currency_code: 'VND',
      exchange_rate: '1',
      amount_fc: '3000000',
      remaining_fc: '3000000',
      remaining: '3000000',
      description: null,
    }
    const invoiceB = { ...invoiceA, target_id: 'eeeeeeee-0000-0000-0000-000000000002', invoice_no: 'HD-002' }
    const fetchMock = mockServer({
      ...formRoutes(),
      '/master/partners': {
        status: 200,
        body: {
          items: [catalogRow(5, 'KH01', 'Công ty A'), catalogRow(6, 'KH02', 'Công ty B')],
          total: 2,
        },
      },
      '/cash-book/open-invoices': (_init, url) =>
        String(url).includes('partner_id=5')
          ? { status: 200, body: { items: [invoiceA, invoiceB] } }
          : { status: 200, body: { items: [] } },
      '/cash-book/vouchers': { status: 201, body: CREATED_VOUCHER },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/giao-dich/phieu/moi?kind=0')

    const operationSelect = await screen.findByLabelText('Nghiệp vụ')
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Thu công nợ khách hàng' })).toBeInTheDocument()
    })
    await user.selectOptions(operationSelect, 'thu-cong-no-kh')
    const cashInput = screen.getByLabelText('TK quỹ')
    await user.type(cashInput, '1111')
    await user.keyboard('{Enter}')
    const partnerInput = screen.getByLabelText('Đối tượng (bắt buộc)')
    await user.type(partnerInput, 'KH01')
    await user.keyboard('{Enter}')

    // Gõ số đối trừ vào MỘT trong hai hóa đơn.
    const amountA = await screen.findByLabelText('Số đối trừ cho HD-001')
    await user.type(amountA, '1000000')

    // Đổi đối tác: số đã gõ của KH01 phải bị bỏ, không gửi dòng vô hình.
    // Hai ô lookup đang có giá trị (TK quỹ, Đối tượng) — nút X thứ hai là của
    // ô Đối tượng theo thứ tự DOM.
    const clearButtons = screen.getAllByLabelText('Bỏ chọn')
    await user.click(clearButtons[clearButtons.length - 1] as HTMLElement)
    const partnerInputAgain = await screen.findByRole('combobox', {
      name: 'Đối tượng (bắt buộc)',
    })
    await user.type(partnerInputAgain, 'KH02')
    await user.keyboard('{Enter}')

    // Điền dòng tối thiểu rồi cất.
    const firstCell = await screen.findByLabelText('TK Nợ, dòng 1')
    await user.click(firstCell)
    await user.keyboard('{Tab}')
    await user.keyboard('{Tab}')
    await user.keyboard('{Tab}')
    const amountInput = await screen.findByLabelText('Số tiền, dòng 1')
    await user.type(amountInput, '1000000')
    await user.keyboard('{Tab}')
    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/cash-book/vouchers'),
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body).toMatchObject({ partner_id: 6 })
      expect(body.settlements).toEqual([])
    })
  })
})
