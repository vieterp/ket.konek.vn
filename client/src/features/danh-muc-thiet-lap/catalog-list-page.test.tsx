/**
 * Màn hình danh mục, kiểm qua cả cây (phiên khôi phục từ storage → truy vấn →
 * định tuyến), chỉ giả lập `fetch` — cùng triết lý với `login-flow.test.tsx`.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

import {
  EMPTY_PAGE,
  baseRoutes,
  catalogRow,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
} from './feature-test-utils'

describe('màn hình danh mục', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('hiện lưới đối tác với cột riêng, phân trang và bộ lọc vai trò', async () => {
    mockServer({
      ...baseRoutes(),
      '/master/partners': {
        status: 200,
        body: {
          items: [
            catalogRow({ id: 10, code: 'KH01', name: 'Công ty Alpha', tax_code: '0301234567' }),
            catalogRow({ id: 11, code: 'NHOM-KH', name: 'Khách miền Nam', is_group: true }),
          ],
          total: 2,
        },
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/doi-tac')

    expect(await screen.findByText('Công ty Alpha')).toBeInTheDocument()
    expect(screen.getByText('0301234567')).toBeInTheDocument()
    // Dòng nhóm là nút đi xuống nhánh, có biểu tượng thư mục.
    expect(screen.getByRole('button', { name: 'NHOM-KH' })).toBeInTheDocument()
    expect(screen.getByText('1–2 trong 2')).toBeInTheDocument()
    // Đối tác có bộ lọc vai trò (H62).
    expect(screen.getByRole('radiogroup', { name: 'Lọc theo vai trò' })).toBeInTheDocument()
  })

  it('bấm dòng nhóm thì lưới đọc con của nhóm đó', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/master/warehouses': (init) => {
        void init
        return {
          status: 200,
          body: {
            items: [catalogRow({ id: 5, code: 'KHO-MB', name: 'Kho miền Bắc', is_group: true })],
            total: 1,
          },
        }
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/kho')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'KHO-MB' }))
    await waitFor(() => {
      const listCalls = fetchMock.mock.calls
        .map((call) => String(call[0]))
        .filter((url) => url.includes('/master/warehouses?'))
      expect(listCalls.some((url) => url.includes('parent_id=5'))).toBe(true)
    })
  })

  it('thêm mới: drawer gửi POST kèm khóa chống trùng, mã trùng thì hiện đúng thông báo', async () => {
    const posts: { headers: Record<string, string> }[] = []
    mockServer({
      ...baseRoutes(),
      '/master/units_of_measure': (init) => {
        if (init?.method === 'POST') {
          posts.push({ headers: (init.headers ?? {}) as Record<string, string> })
          return {
            status: 409,
            body: {
              type: 'https://konek.vn/errors/data.duplicate',
              title: 'data.duplicate',
              status: 409,
              detail: 'trùng mã',
              error_code: 'data.duplicate',
            },
          }
        }
        return EMPTY_PAGE
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/don-vi-tinh')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Thêm mới' }))
    await user.type(await screen.findByLabelText('Mã *'), 'CAI')
    await user.type(screen.getByLabelText('Tên *'), 'Cái')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    expect(await screen.findByText('Mã này đã tồn tại trong danh mục.')).toBeInTheDocument()
    expect(posts).toHaveLength(1)
    const headers = posts[0]?.headers ?? {}
    expect(typeof headers[IDEMPOTENCY_HEADER]).toBe('string')
    expect(headers[IDEMPOTENCY_HEADER]).not.toHaveLength(0)
  })

  it('tạo đối tác chỉ tick một vai: bool gửi tường minh, không có null (review C-1)', async () => {
    let postBody: Record<string, unknown> | null = null
    mockServer({
      ...baseRoutes(),
      '/master/payment_terms': EMPTY_PAGE,
      '/master/partners': (init) => {
        if (init?.method === 'POST') {
          postBody = parseJsonBody(init)
          return { status: 201, body: catalogRow({ id: 99, code: 'KH99', name: 'Mới' }) }
        }
        return EMPTY_PAGE
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/doi-tac')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Thêm mới' }))
    await user.type(await screen.findByLabelText('Mã *'), 'KH99')
    await user.type(screen.getByLabelText('Tên *'), 'Mới')
    await user.click(screen.getByLabelText('Là khách hàng'))
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await vi.waitFor(() => {
      expect(postBody).not.toBeNull()
    })
    const body: Record<string, unknown> = postBody ?? {}
    // Ba cột bool là boolean thật — cột phía server không nhận null.
    expect(body.is_customer).toBe(true)
    expect(body.is_vendor).toBe(false)
    expect(body.is_organization).toBe(true)
    // Ô bỏ trống thì vắng khỏi thân request.
    expect(Object.hasOwn(body, 'tax_code')).toBe(false)
  })

  it('điều khoản thanh toán bỏ trống số ngày: khóa vắng khỏi body, server áp mặc định (review H-1)', async () => {
    let postBody: Record<string, unknown> | null = null
    mockServer({
      ...baseRoutes(),
      '/master/payment_terms': (init) => {
        if (init?.method === 'POST') {
          postBody = parseJsonBody(init)
          return { status: 201, body: catalogRow({ id: 7, code: 'NET0', name: 'Trả ngay' }) }
        }
        return EMPTY_PAGE
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/dieu-khoan-thanh-toan')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Thêm mới' }))
    await user.type(await screen.findByLabelText('Mã *'), 'NET0')
    await user.type(screen.getByLabelText('Tên *'), 'Trả ngay')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await vi.waitFor(() => {
      expect(postBody).not.toBeNull()
    })
    const body = postBody ?? {}
    expect(Object.hasOwn(body, 'due_days')).toBe(false)
    expect(Object.hasOwn(body, 'discount_days')).toBe(false)
    expect(Object.hasOwn(body, 'discount_percent')).toBe(false)
  })

  it('đường dẫn danh mục lạ rơi về danh mục đối tác', async () => {
    mockServer({ ...baseRoutes(), '/master/partners': EMPTY_PAGE })
    renderFeatureAt('/danh-muc-thiet-lap/danh-muc/khong-ton-tai')

    expect(
      await screen.findByRole('heading', { name: 'Đối tác' }),
    ).toBeInTheDocument()
  })
})
