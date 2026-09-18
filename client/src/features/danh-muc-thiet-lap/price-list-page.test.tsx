/**
 * Màn hình một bảng giá (7H-2b): hồ sơ + thẻ dòng giá; mã hàng tra server,
 * ĐVT của dòng theo mã hàng đang chọn; nhóm không có dòng.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  catalogRow,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
} from './feature-test-utils'

const PRICE_LIST = catalogRow({
  id: 30,
  code: 'BG-DL',
  name: 'Bảng giá đại lý',
  direction: 1,
  partner_id: 10,
  contract_id: null,
  effective_from: '2026-01-01',
  effective_to: null,
})
const ITEM_HH01 = catalogRow({ id: 40, code: 'HH01', name: 'Nước suối 500ml', base_unit_id: 7 })
const UNIT_THUNG = catalogRow({ id: 8, code: 'THUNG', name: 'Thùng 24' })
const LINES = [{ id: 1, price_list_id: 30, item_id: 40, unit_id: 8, min_quantity: '10.000', price: '100000.000000', row_version: 2 }]

function routes(overrides: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/master/price_lists/30': { status: 200, body: PRICE_LIST },
    '/master/price_lists/30/lines': { status: 200, body: { items: LINES } },
    '/master/items': { status: 200, body: { items: [ITEM_HH01], total: 1 } },
    '/master/items/40/units': { status: 200, body: { items: [{ id: 1, item_id: 40, unit_id: 8, factor: '24', row_version: 1 }] } },
    '/master/units_of_measure': { status: 200, body: { items: [UNIT_THUNG], total: 1 } },
    '/master/partners': { status: 200, body: { items: [catalogRow({ id: 10, code: 'KH01', name: 'Công ty Alpha' })], total: 1 } },
    '/master/contracts': { status: 200, body: { items: [], total: 0 } },
    ...overrides,
  }
}

describe('màn hình bảng giá', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('hiện hồ sơ bảng giá và dòng giá với tên mã hàng, ĐVT', async () => {
    mockServer(routes())
    renderFeatureAt('/danh-muc-thiet-lap/bang-gia/30')

    expect(await screen.findByRole('heading', { name: 'BG-DL — Bảng giá đại lý' })).toBeInTheDocument()
    const info = screen.getByRole('region', { name: 'Thông tin bảng giá' })
    expect(info).toHaveTextContent('Giá bán')
    await waitFor(() => {
      expect(info).toHaveTextContent('KH01 — Công ty Alpha')
    })
    const lines = screen.getByRole('region', { name: 'Dòng giá' })
    await waitFor(() => {
      expect(lines).toHaveTextContent('HH01 — Nước suối 500ml')
      expect(lines).toHaveTextContent('THUNG — Thùng 24')
    })
    expect(within(lines).getByText('100000.000000')).toBeInTheDocument()
  })

  it('thêm dòng: chọn mã hàng, ĐVT quy đổi của mã đó, POST đúng thân', async () => {
    const fetchMock = mockServer(
      routes({
        '/master/price_lists/30/lines': (init) =>
          init?.method === 'POST' ? { status: 201, body: { ...LINES[0], id: 2 } } : { status: 200, body: { items: LINES } },
      }),
    )
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/bang-gia/30')

    await user.click(await screen.findByRole('button', { name: 'Thêm dòng' }))
    expect(screen.getByLabelText('Đơn vị tính')).toBeDisabled()
    await user.type(screen.getByLabelText('Mã hàng *'), 'HH01')
    await user.keyboard('{Enter}')
    await waitFor(() => {
      expect(within(screen.getByLabelText('Đơn vị tính')).getByText('THUNG — Thùng 24')).toBeInTheDocument()
    })
    await user.selectOptions(screen.getByLabelText('Đơn vị tính'), '8')
    const minQuantity = screen.getByLabelText('Số lượng từ *')
    expect(minQuantity).toHaveValue('1')
    await user.clear(minQuantity)
    await user.type(minQuantity, '5')
    await user.type(screen.getByLabelText('Đơn giá *'), '98000')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/master/price_lists/30/lines') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toEqual({ item_id: 40, unit_id: 8, min_quantity: '5', price: '98000' })
    })
  })

  it('sửa dòng: PUT trọn bộ kèm row_version', async () => {
    const fetchMock = mockServer(routes({ '/master/price_lists/30/lines/1': { status: 200, body: LINES[0] } }))
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/bang-gia/30')

    const lines = await screen.findByRole('region', { name: 'Dòng giá' })
    await user.click(await within(lines).findByRole('button', { name: 'Sửa' }))
    expect(screen.getByLabelText('Đơn giá *')).toHaveValue('100000.000000')
    const price = screen.getByLabelText('Đơn giá *')
    await user.clear(price)
    await user.type(price, '99000')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/master/price_lists/30/lines/1') && (entry[1] as RequestInit).method === 'PUT',
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toEqual({
        item_id: 40,
        unit_id: 8,
        min_quantity: '10.000',
        price: '99000',
        row_version: 2,
      })
    })
  })

  it('nhóm bảng giá: không có thẻ dòng', async () => {
    mockServer(routes({ '/master/price_lists/30': { status: 200, body: { ...PRICE_LIST, is_group: true, direction: null } } }))
    renderFeatureAt('/danh-muc-thiet-lap/bang-gia/30')
    expect(await screen.findByText('Nhóm bảng giá không có dòng giá — chọn một bảng giá cụ thể trong nhóm.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Dòng giá' })).not.toBeInTheDocument()
  })
})
