/**
 * Màn hình chi tiết đối tác: thông tin + thẻ công nợ (BFF `partners/{id}/overview`,
 * nợ H56 trả ở 7G-4) + tài khoản ngân hàng (FR-SYS-033, đọc thẳng router module).
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  catalogRow,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
} from './feature-test-utils'

const PARTNER_INFO = catalogRow({
  id: 10,
  code: 'KH01',
  name: 'Công ty Alpha',
  is_customer: true,
  is_vendor: true,
  is_organization: true,
  tax_code: '0301234567',
  address: '1 Lê Lợi, Quận 1',
  phone: '0281234567',
  email: null,
  website: null,
  contact_name: null,
  province: null,
  district: null,
  country: null,
  invoice_recipient: null,
  invoice_email: null,
  credit_limit: '500000000',
  payment_term_id: null,
})

/**
 * Thẻ công nợ như server trả: nửa phải thu có số, nửa phải trả `null` vì người
 * xem không có quyền mua hàng — thẻ phải nói "không có quyền" chứ không in 0.
 */
const OVERVIEW = {
  status: 200,
  body: {
    partner: PARTNER_INFO,
    debt: {
      as_of: '2026-09-30',
      receivable: {
        open_amount: '2600000',
        open_count: 3,
        overdue_amount: '1600000',
        overdue_count: 2,
        oldest_due_date: '2026-02-20',
      },
      payable: null,
      credit_limit: '500000000',
      credit_available: '497400000',
    },
  },
}

/**
 * Danh mục ngân hàng CÓ cấu trúc nhóm: VCB nằm trong nhóm "NH trong nước".
 * Tầng gốc chỉ trả nút nhóm; bản ghi thật về qua `subtree_of` — đúng hình dạng
 * dữ liệu mà review H-3 chỉ ra là bị bỏ sót khi chỉ đọc trang gốc.
 */
const BANK_GROUP = catalogRow({ id: 20, code: 'NH-VN', name: 'NH trong nước', is_group: true })
const BANK_VCB = catalogRow({
  id: 21,
  code: 'VCB',
  name: 'Ngoại thương Việt Nam',
  parent_id: 20,
  level: 1,
})

const BANKS: (init?: RequestInit, url?: string) => { status: number; body: unknown } = (
  _init,
  url,
) =>
  url?.includes('subtree_of=20') === true
    ? { status: 200, body: { items: [BANK_GROUP, BANK_VCB], total: 2 } }
    : { status: 200, body: { items: [BANK_GROUP], total: 1 } }

describe('màn hình đối tác', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('hiện thông tin, hai vai trò, thẻ công nợ và bảng tài khoản', async () => {
    mockServer({
      ...baseRoutes(),
      '/partners/10/overview': OVERVIEW,
      '/master/partners/10/bank-accounts': {
        status: 200,
        body: {
          items: [
            {
              id: 1,
              partner_id: 10,
              bank_id: 21,
              account_number: '00110002345',
              account_holder: 'CONG TY ALPHA',
              bank_branch: 'CN Sài Gòn',
              is_default: true,
              is_active: true,
              row_version: 1,
            },
          ],
        },
      },
      '/master/banks': BANKS,
    })
    renderFeatureAt('/danh-muc-thiet-lap/doi-tac/10')

    expect(await screen.findByRole('heading', { name: 'KH01 — Công ty Alpha' })).toBeInTheDocument()
    // Một bản ghi, cả hai vai (FR-SYS-031).
    expect(screen.getByText('Khách hàng · Nhà cung cấp')).toBeInTheDocument()
    expect(screen.getByText('0301234567')).toBeInTheDocument()
    // Thẻ công nợ: số từ BFF, định dạng tiền theo locale, không đi qua `Number`.
    const debtCard = screen.getByRole('region', { name: 'Công nợ' })
    expect(debtCard).toHaveTextContent('Tại ngày 30/9/26')
    expect(debtCard).toHaveTextContent('2.600.000')
    expect(debtCard).toHaveTextContent('1.600.000')
    expect(debtCard).toHaveTextContent('2 khoản quá hạn')
    expect(debtCard).toHaveTextContent('20/2/26')
    expect(debtCard).toHaveTextContent('497.400.000')
    // Nửa phải trả bị server giấu theo quyền → thẻ nói rõ, không in 0.
    expect(debtCard).toHaveTextContent('Bạn không có quyền xem chiều công nợ này.')
    // Tài khoản ngân hàng với tên ngân hàng tra từ danh mục.
    expect(await screen.findByText('00110002345')).toBeInTheDocument()
    expect(screen.getByText('VCB — Ngoại thương Việt Nam')).toBeInTheDocument()
  })

  it('không có quyền xem chiều nào thì thẻ nói rõ ở cả hai nửa và không có dòng ngưỡng', async () => {
    mockServer({
      ...baseRoutes(),
      '/partners/10/overview': {
        status: 200,
        body: {
          partner: PARTNER_INFO,
          debt: {
            as_of: '2026-09-30',
            receivable: null,
            payable: { open_amount: '0', open_count: 0, overdue_amount: '0', overdue_count: 0, oldest_due_date: null },
            credit_limit: '500000000',
            credit_available: null,
          },
        },
      },
      '/master/partners/10/bank-accounts': { status: 200, body: { items: [] } },
      '/master/banks': BANKS,
    })
    renderFeatureAt('/danh-muc-thiet-lap/doi-tac/10')

    await screen.findByRole('heading', { name: 'KH01 — Công ty Alpha' })
    const debtCard = screen.getByRole('region', { name: 'Công nợ' })
    expect(debtCard).toHaveTextContent('Bạn không có quyền xem chiều công nợ này.')
    // Nửa phải trả có quyền nhưng không có khoản nào: số 0 thật, không có nhãn quá hạn.
    expect(debtCard).not.toHaveTextContent('khoản quá hạn')
    expect(debtCard).not.toHaveTextContent('Hạn cũ nhất')
    // Không thấy nửa phải thu thì không có "còn được nợ".
    expect(debtCard).not.toHaveTextContent('Còn được nợ')
  })

  it('thêm tài khoản ngân hàng gửi POST vào bảng con của đối tác', async () => {
    let postBody: Record<string, unknown> | null = null
    mockServer({
      ...baseRoutes(),
      '/partners/10/overview': OVERVIEW,
      '/master/partners/10/bank-accounts': (init) => {
        if (init?.method === 'POST') {
          postBody = parseJsonBody(init)
          return {
            status: 201,
            body: {
              id: 2,
              partner_id: 10,
              bank_id: 21,
              account_number: '999',
              account_holder: 'CONG TY ALPHA',
              bank_branch: null,
              is_default: false,
              is_active: true,
              row_version: 1,
            },
          }
        }
        return { status: 200, body: { items: [] } }
      },
      '/master/banks': BANKS,
    })
    renderFeatureAt('/danh-muc-thiet-lap/doi-tac/10')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Thêm tài khoản' }))
    await user.type(screen.getByLabelText('Ngân hàng *'), 'vcb')
    await user.click(await screen.findByText('VCB — Ngoại thương Việt Nam'))
    await user.type(screen.getByLabelText('Số tài khoản *'), '999')
    await user.type(screen.getByLabelText('Chủ tài khoản *'), 'CONG TY ALPHA')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await vi.waitFor(() => {
      expect(postBody).not.toBeNull()
    })
    expect(postBody).toEqual({
      bank_id: 21,
      account_number: '999',
      account_holder: 'CONG TY ALPHA',
      bank_branch: null,
    })
  })
})
