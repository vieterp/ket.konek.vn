/**
 * Màn hình chi tiết mã hàng (7H-2b): thẻ thông tin, thẻ mức giá và thẻ bậc
 * chiết khấu đọc/ghi thẳng ba bảng con của `items`; nhóm không có thẻ giá.
 * Thẻ định mức NVL (8C-2) chỉ hiện với hàng qua kho.
 */

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'

import {
  baseRoutes,
  catalogRow,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
} from './feature-test-utils'

const ITEM = catalogRow({
  id: 40,
  code: 'HH01',
  name: 'Nước suối 500ml',
  nature: 'goods',
  base_unit_id: 7,
  warehouse_id: null,
  description: null,
  price_is_tax_inclusive: null,
})
const UNIT_CHAI = catalogRow({ id: 7, code: 'CHAI', name: 'Chai' })
const UNIT_THUNG = catalogRow({ id: 8, code: 'THUNG', name: 'Thùng 24' })

const PRICE_LEVELS = [
  { id: 1, item_id: 40, unit_id: null, direction: 1, level: 1, price: '5000.000000', label: 'Bán lẻ', row_version: 1 },
  { id: 2, item_id: 40, unit_id: 8, direction: 1, level: 1, price: '110000.000000', label: null, row_version: 3 },
]
const TIERS = [{ id: 5, item_id: 40, min_quantity: '100.000', discount_percent: '2.50', row_version: 1 }]
const SCREW = catalogRow({ id: 41, code: 'VIT', name: 'Vít M4', nature: 'goods', base_unit_id: 7 })
const BOM = [{ id: 3, item_id: 40, component_item_id: 41, quantity: '4.000000', allocation_ratio: '1.000000', row_version: 1 }]

function routes(overrides: FakeRoutes = {}): FakeRoutes {
  return {
    ...baseRoutes(),
    '/master/items/40': { status: 200, body: ITEM },
    '/master/items/40/prices': { status: 200, body: { items: PRICE_LEVELS } },
    '/master/items/40/discount-tiers': { status: 200, body: { items: TIERS } },
    '/master/items/40/bom': { status: 200, body: { items: BOM } },
    '/master/items': { status: 200, body: { items: [ITEM, SCREW], total: 2 } },
    '/master/items/40/units': { status: 200, body: { items: [{ id: 1, item_id: 40, unit_id: 8, factor: '24', row_version: 1 }] } },
    '/master/units_of_measure': { status: 200, body: { items: [UNIT_CHAI, UNIT_THUNG], total: 2 } },
    '/master/warehouses': { status: 200, body: { items: [], total: 0 } },
    ...overrides,
  }
}

describe('màn hình mã hàng', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('hiện thông tin, mức giá với ĐVT tra tên, và bậc chiết khấu', async () => {
    mockServer(routes())
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    expect(await screen.findByRole('heading', { name: 'HH01 — Nước suối 500ml' })).toBeInTheDocument()
    const info = screen.getByRole('region', { name: 'Thông tin mã hàng' })
    expect(info).toHaveTextContent('Hàng hóa')
    expect(info).toHaveTextContent('Theo thiết lập hệ thống')
    await waitFor(() => {
      expect(info).toHaveTextContent('CHAI — Chai')
    })

    const prices = screen.getByRole('region', { name: 'Mức giá' })
    expect(await within(prices).findByText('Bán lẻ')).toBeInTheDocument()
    expect(within(prices).getByText('5000.000000')).toBeInTheDocument()
    await waitFor(() => {
      expect(prices).toHaveTextContent('Đơn vị chính (CHAI — Chai)')
      expect(prices).toHaveTextContent('THUNG — Thùng 24')
    })
    const tiers = screen.getByRole('region', { name: 'Bậc chiết khấu theo số lượng' })
    expect(within(tiers).getByText('100.000')).toBeInTheDocument()
    expect(within(tiers).getByText('2.50')).toBeInTheDocument()
    // Định mức NVL (8C-2): linh kiện tra tên theo id, số lượng và tỷ lệ.
    const bom = screen.getByRole('region', { name: 'Định mức nguyên vật liệu' })
    expect(await within(bom).findByText('VIT — Vít M4')).toBeInTheDocument()
    expect(within(bom).getByText('4.000000')).toBeInTheDocument()
  })

  it('thêm dòng định mức: chọn linh kiện, POST đúng thân với tỷ lệ mặc định 1', async () => {
    const fetchMock = mockServer(
      routes({
        '/master/items/40/bom': (init) =>
          init?.method === 'POST' ? { status: 201, body: { ...BOM[0], id: 4 } } : { status: 200, body: { items: BOM } },
      }),
    )
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    await user.click(await screen.findByRole('button', { name: 'Thêm linh kiện' }))
    await user.type(screen.getByLabelText('Linh kiện *'), 'VIT')
    await user.keyboard('{Enter}')
    await user.type(screen.getByLabelText('Số lượng *'), '6')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/master/items/40/bom') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toEqual({ component_item_id: 41, quantity: '6', allocation_ratio: '1' })
    })
  })

  it('dịch vụ: không có thẻ định mức (server cũng từ chối)', async () => {
    const fetchMock = mockServer(
      routes({ '/master/items/40': { status: 200, body: { ...ITEM, nature: 'service', base_unit_id: null } } }),
    )
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    expect(await screen.findByRole('region', { name: 'Mức giá' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Định mức nguyên vật liệu' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some((entry) => String(entry[0]).endsWith('/master/items/40/bom'))).toBe(false)
  })

  it('thêm mức giá theo đơn vị quy đổi gửi POST kèm khóa chống trùng, unit_id là id đơn vị', async () => {
    const fetchMock = mockServer(
      routes({
        '/master/items/40/prices': (init) =>
          init?.method === 'POST'
            ? { status: 201, body: { ...PRICE_LEVELS[0], id: 9 } }
            : { status: 200, body: { items: PRICE_LEVELS } },
      }),
    )
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    await user.click(await screen.findByRole('button', { name: 'Thêm mức giá' }))
    await user.selectOptions(screen.getByLabelText('Chiều giá'), '0')
    const level = screen.getByLabelText('Mức *')
    await user.clear(level)
    await user.type(level, '2')
    await waitFor(() => {
      expect(within(screen.getByLabelText('Đơn vị tính')).getByText('THUNG — Thùng 24')).toBeInTheDocument()
    })
    await user.selectOptions(screen.getByLabelText('Đơn vị tính'), '8')
    await user.type(screen.getByLabelText('Đơn giá *'), '95000')
    await user.type(screen.getByLabelText('Tên thang giá'), 'Đại lý')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/master/items/40/prices') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(init.headers).toHaveProperty(IDEMPOTENCY_HEADER)
      expect(parseJsonBody(init)).toEqual({ direction: 0, level: 2, unit_id: 8, price: '95000', label: 'Đại lý' })
    })
  })

  it('sửa mức giá: form điền sẵn, PUT gửi trọn bộ kèm row_version; xóa hai bước', async () => {
    const fetchMock = mockServer(
      routes({
        '/master/items/40/prices/2': (init) =>
          init?.method === 'DELETE' ? { status: 204, body: null } : { status: 200, body: PRICE_LEVELS[1] },
      }),
    )
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    const prices = await screen.findByRole('region', { name: 'Mức giá' })
    const editButtons = await within(prices).findAllByRole('button', { name: 'Sửa' })
    await user.click(editButtons[1]!)
    expect(screen.getByLabelText('Đơn giá *')).toHaveValue('110000.000000')
    await waitFor(() => {
      expect(screen.getByLabelText('Đơn vị tính')).toHaveValue('8')
    })
    const price = screen.getByLabelText('Đơn giá *')
    await user.clear(price)
    await user.type(price, '108000')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/master/items/40/prices/2') && (entry[1] as RequestInit).method === 'PUT',
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toEqual({
        direction: 1,
        level: 1,
        unit_id: 8,
        price: '108000',
        label: null,
        row_version: 3,
      })
    })

    const removeButtons = within(prices).getAllByRole('button', { name: 'Xóa' })
    await user.click(removeButtons[1]!)
    expect(within(prices).getByRole('button', { name: 'Bấm lần nữa để xóa' })).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some((entry) => (entry[1] as RequestInit | undefined)?.method === 'DELETE'),
    ).toBe(false)
    await user.click(within(prices).getByRole('button', { name: 'Bấm lần nữa để xóa' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) => String(entry[0]).endsWith('/master/items/40/prices/2') && (entry[1] as RequestInit).method === 'DELETE',
      )
      expect(call).toBeDefined()
    })
  })

  it('thêm bậc chiết khấu gửi POST đúng thân', async () => {
    const fetchMock = mockServer(
      routes({
        '/master/items/40/discount-tiers': (init) =>
          init?.method === 'POST' ? { status: 201, body: { ...TIERS[0], id: 6 } } : { status: 200, body: { items: TIERS } },
      }),
    )
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    await user.click(await screen.findByRole('button', { name: 'Thêm bậc' }))
    await user.type(screen.getByLabelText('Số lượng từ *'), '500')
    await user.type(screen.getByLabelText('Chiết khấu % *'), '5')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (entry) =>
          String(entry[0]).endsWith('/master/items/40/discount-tiers') && (entry[1] as RequestInit).method === 'POST',
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toEqual({ min_quantity: '500', discount_percent: '5' })
    })
  })

  it('nhóm vật tư: không có thẻ giá, nói thẳng lý do', async () => {
    const fetchMock = mockServer(
      routes({ '/master/items/40': { status: 200, body: { ...ITEM, is_group: true, nature: null, base_unit_id: null } } }),
    )
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    expect(await screen.findByText('Nhóm vật tư không khai giá — chọn một mã hàng cụ thể trong nhóm.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Mức giá' })).not.toBeInTheDocument()
    expect(fetchMock.mock.calls.some((entry) => String(entry[0]).includes('/prices'))).toBe(false)
  })

  it('sửa bậc chiết khấu gặp 409: nạp lại danh sách và đóng form để lượt sửa sau cầm row_version mới (review M-4)', async () => {
    let tierListReads = 0
    const fetchMock = mockServer(
      routes({
        '/master/items/40/discount-tiers': () => {
          tierListReads += 1
          return { status: 200, body: { items: TIERS } }
        },
        '/master/items/40/discount-tiers/5': {
          status: 409,
          body: { type: 'about:blank', title: 'Conflict', status: 409, error_code: 'concurrency.stale_row_version' },
        },
      }),
    )
    const user = userEvent.setup()
    renderFeatureAt('/danh-muc-thiet-lap/vat-tu-hang-hoa/40')

    const tiers = await screen.findByRole('region', { name: 'Bậc chiết khấu theo số lượng' })
    await user.click(await within(tiers).findByRole('button', { name: 'Sửa' }))
    const readsBefore = tierListReads
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await waitFor(() => {
      expect(fetchMock.mock.calls.some((entry) => (entry[1] as RequestInit | undefined)?.method === 'PUT')).toBe(true)
      expect(tierListReads).toBeGreaterThan(readsBefore)
    })
    // Form đóng: không còn ô nhập, nút Thêm trở lại.
    expect(screen.queryByLabelText('Chiết khấu % *')).not.toBeInTheDocument()
    expect(within(tiers).getByRole('button', { name: 'Thêm bậc' })).toBeInTheDocument()
  })
})
