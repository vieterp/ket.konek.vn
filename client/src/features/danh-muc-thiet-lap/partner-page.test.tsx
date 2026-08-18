/**
 * Màn hình chi tiết đối tác: thông tin + thẻ công nợ giữ chỗ (H56) + tài khoản
 * ngân hàng (FR-SYS-033). Đọc thẳng router module, không BFF.
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

const PARTNER = {
  status: 200,
  body: catalogRow({
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
  }),
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

  it('hiện thông tin, hai vai trò, thẻ công nợ giữ chỗ và bảng tài khoản', async () => {
    mockServer({
      ...baseRoutes(),
      '/master/partners/10': PARTNER,
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
    // Thẻ công nợ là chỗ giữ chỗ có chủ đích tới phase 7 (H56).
    expect(screen.getByText(/Số liệu công nợ hiện ở đây khi phân hệ Mua hàng/)).toBeInTheDocument()
    // Tài khoản ngân hàng với tên ngân hàng tra từ danh mục.
    expect(await screen.findByText('00110002345')).toBeInTheDocument()
    expect(screen.getByText('VCB — Ngoại thương Việt Nam')).toBeInTheDocument()
  })

  it('thêm tài khoản ngân hàng gửi POST vào bảng con của đối tác', async () => {
    let postBody: Record<string, unknown> | null = null
    mockServer({
      ...baseRoutes(),
      '/master/partners/10': PARTNER,
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
