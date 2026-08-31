/**
 * Form chứng từ nghiệp vụ khác — tạo mới (kèm khóa chống trùng) và hiện toàn
 * bộ vi phạm khi server trả `posting.invalid`.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

import {
  EMPTY_LIST,
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
} from './feature-test-utils'

const ACCOUNTS_ROUTE = {
  status: 200,
  body: {
    package_id: 1,
    items: [
      {
        id: 1,
        code: '111',
        name: 'Tiền mặt',
        balance_nature: 1,
        detail_tracking: null,
        is_summary: false,
        is_foreign_currency: false,
        level: 1,
        parent_id: null,
      },
    ],
  },
}

/** Danh mục chiều — trống ở mọi bài test này (TK dùng không đòi chiều nào). */
function dimensionCatalogRoutes(): Record<string, typeof EMPTY_LIST> {
  return {
    '/master/partners': EMPTY_LIST,
    '/master/employees': EMPTY_LIST,
    '/master/cost_objects': EMPTY_LIST,
    '/master/projects': EMPTY_LIST,
    '/master/contracts': EMPTY_LIST,
    '/master/expense_items': EMPTY_LIST,
    '/master/items': EMPTY_LIST,
    '/master/warehouses': EMPTY_LIST,
    '/master/company_bank_accounts': EMPTY_LIST,
  }
}

/** Gõ TK + Nợ vào dòng đầu tiên của lưới — điều hướng bằng Tab đúng thứ tự cột sửa được. */
async function fillFirstLine(user: ReturnType<typeof userEvent.setup>, code: string, debit: string): Promise<void> {
  const accountInput = await screen.findByLabelText('TK, dòng 1')
  await user.type(accountInput, code)
  await user.keyboard('{Tab}') // TK → Diễn giải
  await user.keyboard('{Tab}') // Diễn giải → Nợ
  const debitInput = await screen.findByLabelText('Nợ, dòng 1')
  await user.type(debitInput, debit)
  await user.keyboard('{Tab}') // Nợ → Có, chốt ô Nợ
}

describe('form chứng từ nghiệp vụ khác', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('tạo mới: gửi POST kèm khóa chống trùng rồi về danh sách', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      ...dimensionCatalogRoutes(),
      '/gl/journal-vouchers': {
        status: 200,
        body: {
          id: 'aaaaaaaa-0000-0000-0000-000000000001',
          document_type: 'gl_journal',
          voucher_no: 'GLE00001',
          branch_id: 1,
          document_date: '2026-08-19',
          posting_date: '2026-08-19',
          period_id: 1,
          currency_code: 'VND',
          exchange_rate: '1',
          description: 'Chi phí văn phòng',
          status: 1,
          row_version: 1,
          created_at: '2026-08-19T00:00:00Z',
          created_by: 1,
          posted_at: null,
          posted_by: null,
          cashflow_activity: null,
          lines: [],
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/so-sach-thue/chung-tu/moi')

    await user.type(await screen.findByLabelText('Diễn giải'), 'Chi phí văn phòng')
    await fillFirstLine(user, '111', '1000000')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/gl/journal-vouchers'))
      expect(call).toBeDefined()
    })
    const call = fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/gl/journal-vouchers'))
    const init = call?.[1] as RequestInit
    expect(init.headers).toHaveProperty(IDEMPOTENCY_HEADER)
    const body = parseJsonBody(init)
    expect(body.description).toBe('Chi phí văn phòng')
    // Cờ bản chất bút toán (LD-17) phải LUÔN có mặt — PUT/POST thiếu nó là
    // server nhận mặc định và mỗi lần sửa âm thầm reset cờ (review 4F, H4/N1).
    expect(body.entry_kind).toBe(0)
    expect(Array.isArray(body.lines)).toBe(true)
    expect((body.lines as Record<string, unknown>[])[0]).toMatchObject({ account_id: 1, debit_fc: '1000000' })

    // Lưu xong quay về danh sách chứng từ.
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Chứng từ' })).toBeInTheDocument()
    })
  })

  it('lưu thất bại vì chứng từ không hợp lệ: hiện đủ mọi vi phạm kèm số dòng', async () => {
    mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      ...dimensionCatalogRoutes(),
      '/gl/journal-vouchers': {
        status: 422,
        body: {
          type: 'https://konek.vn/errors/posting.invalid',
          title: 'posting.invalid',
          status: 422,
          detail: 'Chứng từ không hợp lệ',
          error_code: 'posting.invalid',
          violations: [
            { code: 'posting.unbalanced', message: 'Chứng từ không cân: nợ 1.000.000, có 0.', line_no: 1 },
          ],
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/so-sach-thue/chung-tu/moi')

    await user.type(await screen.findByLabelText('Diễn giải'), 'Chi phí văn phòng')
    await fillFirstLine(user, '111', '1000000')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    expect(await screen.findByText('Chứng từ chưa hợp lệ để ghi sổ. Xem danh sách lỗi bên dưới.')).toBeInTheDocument()
    expect(
      screen.getByText('Dòng 1: Chứng từ không cân: nợ 1.000.000, có 0.'),
    ).toBeInTheDocument()
  })

  it('sửa chứng từ kết chuyển: PUT giữ nguyên entry_kind, không âm thầm reset', async () => {
    const voucherId = 'aaaaaaaa-0000-0000-0000-000000000009'
    const closingVoucher = {
      id: voucherId,
      document_type: 'gl_journal',
      voucher_no: 'GLE00009',
      branch_id: 1,
      document_date: '2026-03-31',
      posting_date: '2026-03-31',
      period_id: 3,
      currency_code: 'VND',
      exchange_rate: '1',
      description: 'Kết chuyển cuối quý',
      status: 1,
      row_version: 1,
      created_at: '2026-03-31T00:00:00Z',
      created_by: 1,
      posted_at: null,
      posted_by: null,
      cashflow_activity: null,
      entry_kind: 1,
      lines: [
        {
          id: 'bbbbbbbb-0000-0000-0000-000000000001',
          line_no: 1,
          account_id: 1,
          corresponding_account_id: null,
          currency_code: 'VND',
          exchange_rate: '1',
          debit_fc: '1000000',
          credit_fc: '0',
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
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/accounts': ACCOUNTS_ROUTE,
      ...dimensionCatalogRoutes(),
      [`/gl/journal-vouchers/${voucherId}`]: { status: 200, body: closingVoucher },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/so-sach-thue/chung-tu/${voucherId}`)

    // Form sửa chứng từ kết chuyển phải mở sẵn khối "Mở rộng" và hiện đúng cờ.
    const kindSelect = await screen.findByLabelText('Bản chất bút toán')
    expect((kindSelect as HTMLSelectElement).value).toBe('1')

    await user.click(screen.getByRole('button', { name: 'Cất' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) =>
          String(entry[0]).endsWith(`/gl/journal-vouchers/${voucherId}`) &&
          (entry[1] as RequestInit | undefined)?.method === 'PUT',
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body.entry_kind).toBe(1)
      expect(body.row_version).toBe(1)
    })
  })
})
