/**
 * Màn hình Thiết lập — hai nhóm theo hệ quả (U14) + banner "chốt một lần"
 * (tiêu chí phase 3: banner ở UI thuộc 3D) + đường sửa một tùy chọn.
 */

import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
} from './feature-test-utils'

const GROUPS = {
  status: 200,
  body: {
    groups: [
      {
        key: 'anytime',
        title: 'Đổi được bất kỳ lúc nào',
        description: 'Chỉ ảnh hưởng hiển thị.',
        items: [
          {
            key: 'ui.language',
            title: 'Ngôn ngữ giao diện',
            value: 'vi',
            source: 'setting',
            fiscal_year_code: null,
            is_editable: true,
            locked_reason: null,
          },
        ],
      },
      {
        key: 'decided_once',
        title: 'Chốt một lần',
        description: 'Đổi là số liệu khác luật.',
        items: [
          {
            key: 'accounting_scheme',
            title: 'Chế độ kế toán',
            value: 'TT99',
            source: 'fiscal_year',
            fiscal_year_code: '2026',
            is_editable: false,
            locked_reason: 'Năm tài chính đã quyết toán',
          },
        ],
      },
    ],
  },
}

const SETTINGS = {
  status: 200,
  body: {
    items: [
      {
        key: 'ui.language',
        description: 'Ngôn ngữ giao diện',
        value: 'vi',
        value_type: 'string',
        source: 'default',
        scopes: ['system', 'user'],
        system_row_version: 2,
        user_row_version: null,
      },
    ],
  },
}

describe('màn hình Thiết lập', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('vẽ hai nhóm đúng thứ tự, banner cảnh báo nằm trong nhóm chốt một lần (U14)', async () => {
    mockServer({ ...baseRoutes(), '/setup/settings-groups': GROUPS, '/system/settings': SETTINGS })
    renderFeatureAt('/danh-muc-thiet-lap/thiet-lap')

    expect(await screen.findByText('Đổi được bất kỳ lúc nào')).toBeInTheDocument()
    expect(screen.getByText('Chốt một lần')).toBeInTheDocument()
    expect(
      screen.getByText(/Nhóm này chốt một lần: thay đổi làm số liệu ghi sau khác luật/),
    ).toBeInTheDocument()
    // Mục theo niên độ mang nhãn năm và lý do khóa của server.
    expect(screen.getByText('Niên độ 2026')).toBeInTheDocument()
    expect(screen.getByText('Năm tài chính đã quyết toán')).toBeInTheDocument()
    // Mục khóa không có nút sửa.
    expect(screen.queryByRole('button', { name: 'Sửa Chế độ kế toán' })).not.toBeInTheDocument()
  })

  it('sửa một tùy chọn: PUT mang scope hệ thống + row_version đang giữ', async () => {
    let putBody: Record<string, unknown> | null = null
    mockServer({
      ...baseRoutes(),
      '/setup/settings-groups': GROUPS,
      '/system/settings': SETTINGS,
      '/system/settings/ui.language': (init) => {
        putBody = parseJsonBody(init)
        return { status: 200, body: SETTINGS.body.items[0] }
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/thiet-lap')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Sửa Ngôn ngữ giao diện' }))
    // Không dùng `findByLabelText`: hộp thoại cũng mang nhãn này qua
    // `aria-labelledby` nên nhãn khớp cả dialog lẫn ô nhập.
    const input = await screen.findByRole('textbox', { name: 'Ngôn ngữ giao diện' })
    await user.clear(input)
    await user.type(input, 'en')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await vi.waitFor(() => {
      expect(putBody).not.toBeNull()
    })
    expect(putBody).toEqual({ value: 'en', scope: 'system', row_version: 2 })
  })

  it('lần lưu đầu tiên của một tùy chọn gửi row_version null, không phải 0 (review H-2)', async () => {
    let putBody: Record<string, unknown> | null = null
    const firstWriteSettings = {
      status: 200,
      body: {
        items: [{ ...(SETTINGS.body.items[0] ?? {}), system_row_version: null }],
      },
    }
    mockServer({
      ...baseRoutes(),
      '/setup/settings-groups': GROUPS,
      '/system/settings': firstWriteSettings,
      '/system/settings/ui.language': (init) => {
        putBody = parseJsonBody(init)
        return { status: 200, body: firstWriteSettings.body.items[0] }
      },
    })
    renderFeatureAt('/danh-muc-thiet-lap/thiet-lap')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Sửa Ngôn ngữ giao diện' }))
    const input = await screen.findByRole('textbox', { name: 'Ngôn ngữ giao diện' })
    await user.clear(input)
    await user.type(input, 'en')
    await user.click(screen.getByRole('button', { name: 'Lưu' }))

    await vi.waitFor(() => {
      expect(putBody).not.toBeNull()
    })
    expect(putBody).toEqual({ value: 'en', scope: 'system', row_version: null })
  })
})
