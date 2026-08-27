/**
 * Màn danh sách sao kê (lát 6F-2): chọn TK → danh sách; nhập sao kê gửi đúng
 * multipart (file + bank_account_id + profile_id — hồ sơ tra từ endpoint lọc
 * theo ngân hàng CỦA tài khoản); nhập trùng 409 ra thông điệp bảo xóa bản cũ.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
  type RouteReply,
} from './feature-test-utils'

const BANK_ACCOUNTS_ROUTE: RouteReply = {
  status: 200,
  body: {
    items: [
      {
        id: 21,
        uid: 'uid-21',
        code: 'VCB-001',
        name: 'VCB thanh toán',
        name_en: null,
        parent_id: null,
        path: '21',
        level: 0,
        is_group: false,
        is_active: true,
        branch_id: null,
        row_version: 1,
      },
    ],
    total: 1,
  },
}

const STATEMENT = {
  id: 'bbbbbbbb-0000-0000-0000-000000000001',
  bank_account_id: 21,
  statement_date: '2026-08-20',
  opening_balance: '1000000',
  closing_balance: '1500000',
  profile_id: 7,
  content_hash: 'hash-1',
  imported_by: 1,
  imported_at: '2026-08-21T03:00:00Z',
}

function pageRoutes(): FakeRoutes {
  return {
    ...baseRoutes(),
    '/master/company_bank_accounts': BANK_ACCOUNTS_ROUTE,
    '/bank/statements/profiles': {
      status: 200,
      body: { items: [{ id: 7, bank_id: 3, name: 'VCB CSV chuẩn' }] },
    },
    '/bank/statements': { status: 200, body: { items: [STATEMENT] } },
  }
}

describe('màn danh sách sao kê', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('chọn TK ngân hàng thì danh sách sao kê hiện; chưa chọn thì chỉ có câu hướng dẫn', async () => {
    mockServer(pageRoutes())
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/doi-chieu')

    expect(
      await screen.findByText('Chọn một tài khoản ngân hàng để xem sao kê đã nhập.'),
    ).toBeInTheDocument()

    const accountInput = await screen.findByLabelText('Tài khoản ngân hàng')
    await user.type(accountInput, 'VCB-001')
    await user.keyboard('{Enter}')

    // `dateStyle: 'short'` của vi: '20/8/26'.
    expect(await screen.findByText('20/8/26')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Nhập sao kê' })).toBeInTheDocument()
  })

  it('nhập sao kê: gửi multipart đúng thân; 409 trùng tệp ra thông điệp xóa-bản-cũ', async () => {
    let importCalls = 0
    const fetchMock = mockServer({
      ...pageRoutes(),
      '/bank/statements/import': () => {
        importCalls += 1
        return importCalls === 1
          ? {
              status: 201,
              body: {
                statement: STATEMENT,
                line_count: 3,
                total_credit: '500000',
                total_debit: '0',
              },
            }
          : { status: 409, body: { error_code: 'bank_statement.duplicate' } }
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/doi-chieu')

    const accountInput = await screen.findByLabelText('Tài khoản ngân hàng')
    await user.type(accountInput, 'VCB-001')
    await user.keyboard('{Enter}')

    await user.click(await screen.findByRole('button', { name: 'Nhập sao kê' }))
    // Hồ sơ lọc theo ngân hàng của TK — server endpoint mới của lát này.
    const profileSelect = await screen.findByLabelText('Hồ sơ định dạng')
    await user.selectOptions(profileSelect, '7')
    const file = new File(['Ngay GD;Ghi co\n20/08/2026;500000\n'], 'saoke.csv', {
      type: 'text/csv',
    })
    await user.upload(screen.getByLabelText('Tệp sao kê (CSV/Excel)'), file)
    await user.click(screen.getByRole('button', { name: 'Nhập' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/bank/statements/import'),
      )
      expect(call).toBeDefined()
      const body = (call?.[1] as RequestInit).body as FormData
      expect(body.get('bank_account_id')).toBe('21')
      expect(body.get('profile_id')).toBe('7')
      expect(body.get('file')).toBeInstanceOf(File)
    })

    // Lượt hai cùng tệp: 409 → thông điệp nghiệp vụ, không phải lỗi mạng.
    await user.click(await screen.findByRole('button', { name: 'Nhập sao kê' }))
    await user.selectOptions(await screen.findByLabelText('Hồ sơ định dạng'), '7')
    await user.upload(screen.getByLabelText('Tệp sao kê (CSV/Excel)'), file)
    await user.click(screen.getByRole('button', { name: 'Nhập' }))
    expect(
      await screen.findByText('Tệp sao kê này đã được nhập — xóa sao kê cũ trước nếu muốn nhập lại.'),
    ).toBeInTheDocument()
  })

  it('Hủy rồi mở lại drawer: tệp đã hủy KHÔNG được gửi — phải báo thiếu tệp (review 6F-2 H-1)', async () => {
    const fetchMock = mockServer({
      ...pageRoutes(),
      '/bank/statements/import': { status: 201, body: {} },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/doi-chieu')

    const accountInput = await screen.findByLabelText('Tài khoản ngân hàng')
    await user.type(accountInput, 'VCB-001')
    await user.keyboard('{Enter}')

    // Lượt 1: chọn đủ hồ sơ + tệp rồi HỦY.
    await user.click(await screen.findByRole('button', { name: 'Nhập sao kê' }))
    await user.selectOptions(await screen.findByLabelText('Hồ sơ định dạng'), '7')
    const file = new File(['x'], 'saoke.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('Tệp sao kê (CSV/Excel)'), file)
    await user.click(screen.getByRole('button', { name: 'Hủy' }))

    // Lượt 2: mở lại — ô tệp trống thì bấm Nhập phải BÁO, không gửi tệp ma.
    await user.click(await screen.findByRole('button', { name: 'Nhập sao kê' }))
    await user.selectOptions(await screen.findByLabelText('Hồ sơ định dạng'), '7')
    await user.click(screen.getByRole('button', { name: 'Nhập' }))

    expect(await screen.findByText('Chưa chọn tệp sao kê.')).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.find((entry) => String(entry[0]).endsWith('/bank/statements/import')),
    ).toBeUndefined()
  })

  it('xóa sao kê phải qua bước xác nhận rồi mới DELETE', async () => {
    const fetchMock = mockServer({
      ...pageRoutes(),
      [`/bank/statements/${STATEMENT.id}`]: { status: 204, body: null },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/doi-chieu')

    const accountInput = await screen.findByLabelText('Tài khoản ngân hàng')
    await user.type(accountInput, 'VCB-001')
    await user.keyboard('{Enter}')

    await user.click(await screen.findByRole('button', { name: 'Xóa' }))
    // Chưa DELETE — mới hiện nút xác nhận.
    expect(
      fetchMock.mock.calls.find((entry) => (entry[1] as RequestInit | undefined)?.method === 'DELETE'),
    ).toBeUndefined()
    await user.click(screen.getByRole('button', { name: 'Xóa hẳn?' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => (entry[1] as RequestInit | undefined)?.method === 'DELETE',
      )
      expect(String(call?.[0])).toContain(`/bank/statements/${STATEMENT.id}`)
    })
  })
})
