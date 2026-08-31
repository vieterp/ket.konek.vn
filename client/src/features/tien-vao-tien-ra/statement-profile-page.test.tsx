/**
 * Màn khai hồ sơ định dạng sao kê (lát 6G-2).
 *
 * Ba điều đáng canh, tất cả là chỗ một bản sửa vô ý làm hỏng im lặng:
 * ô trống phải thành `null` chứ không chuỗi rỗng; lượt SỬA phải gửi kèm
 * `row_version` đang giữ; và 409 của server phải hiện ra chứ không nuốt.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import {
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
} from './feature-test-utils'

const PROFILE = {
  id: 7,
  bank_id: 3,
  name: 'Sao ke IB',
  file_kind: 'csv',
  header_row: 1,
  date_col: 'Ngay GD',
  date_format: '%d/%m/%Y',
  debit_col: 'Ghi no',
  credit_col: 'Ghi co',
  amount_col: null,
  sign_rule: null,
  ref_col: 'So CT',
  description_col: 'Dien giai',
  balance_col: null,
  decimal_sep: '.',
  thousand_sep: null,
  csv_delimiter: ';',
  row_version: 4,
}

function routes(extra: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/bank/statements/profiles/all': { status: 200, body: { items: [PROFILE] } },
    ...extra,
  }
}

describe('StatementProfilePage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('gửi lượt sửa kèm row_version và biến ô trống thành null', async () => {
    const fetchMock = mockServer(
      routes({
        '/bank/statements/profiles/7': { status: 200, body: PROFILE },
      }),
    )
    renderFeatureAt('/tien-vao-tien-ra/ho-so-sao-ke')

    const edit = await screen.findByRole('button', { name: /Sửa Sao ke IB/ })
    await userEvent.click(edit)

    // Xóa cột diễn giải: ô trống nghĩa là "tệp không có cột này" → `null`.
    const description = await screen.findByLabelText('Cột diễn giải')
    await userEvent.clear(description)
    await userEvent.click(screen.getByRole('button', { name: 'Lưu' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) =>
          String(entry[0]).includes('/bank/statements/profiles/7') &&
          (entry[1] as RequestInit | undefined)?.method === 'PUT',
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body.row_version).toBe(4)
      expect(body.description_col).toBeNull()
      // Ô không đụng tới giữ nguyên giá trị đã lưu.
      expect(body.date_col).toBe('Ngay GD')
    })
  })

  it('hiện lỗi 409 của server thay vì nuốt', async () => {
    mockServer(
      routes({
        '/bank/statements/profiles': {
          status: 409,
          body: { error_code: 'bank_statement_profile.conflict', detail: 'trùng tên' },
        },
      }),
    )
    renderFeatureAt('/tien-vao-tien-ra/ho-so-sao-ke')

    await userEvent.click(await screen.findByRole('button', { name: 'Khai hồ sơ mới' }))
    await userEvent.type(await screen.findByLabelText('Mã ngân hàng'), '3')
    await userEvent.type(screen.getByLabelText('Tên hồ sơ'), 'Trung ten')
    await userEvent.type(screen.getByLabelText('Cột ngày'), 'Ngay GD')
    await userEvent.click(screen.getByRole('button', { name: 'Lưu' }))

    expect(await screen.findByText(/trùng tên trong cùng ngân hàng/)).toBeInTheDocument()
  })
})
