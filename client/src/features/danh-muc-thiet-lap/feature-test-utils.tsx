/**
 * Bộ dựng test cho nhóm màn hình Danh mục & Thiết lập.
 *
 * Cùng triết lý với `login-flow.test.tsx`: dựng **cả cây** (providers → phiên →
 * truy vấn → định tuyến) và chỉ giả lập `fetch`. Khác một điểm: các test ở đây
 * không đi qua màn hình đăng nhập — phiên được gieo sẵn vào `localStorage`
 * (đúng đường "khôi phục phiên" của SessionProvider), vì thứ đang kiểm là màn
 * hình nghiệp vụ, không phải luồng đăng nhập vốn đã có bộ test riêng.
 *
 * Tên tệp không mang `.test` nên vitest không thu thập nó như một bài test.
 */

import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { MemoryRouter, Navigate, Route, Routes } from 'react-router-dom'
import { vi } from 'vitest'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'
import { APP_VERSION } from '@/lib/app-version'

import { CatalogListPage } from './catalog-list-page'
import { PartnerPage } from './partner-page'
import { SettingsPage } from './settings-page'

export interface RouteReply {
  readonly status: number
  readonly body: unknown
}

export type FakeRoutes = Record<
  string,
  RouteReply | ((init?: RequestInit, url?: string) => RouteReply)
>

/**
 * Giả lập server: khớp theo **pathname** (bỏ query) với phần đuôi của khóa.
 * Handler dạng hàm nhận cả `init` lẫn `url` đầy đủ (để soi query string).
 * Trả về mock để bài test soi lại lời gọi (method, body, header).
 */
export function mockServer(routes: FakeRoutes): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const pathname = url.split('?')[0] ?? url
    const match = Object.keys(routes).find((path) => pathname.endsWith(path))
    if (match === undefined) {
      return Promise.resolve(new Response('{}', { status: 404 }))
    }
    const entry = routes[match]
    const reply = typeof entry === 'function' ? entry(init, url) : (entry as RouteReply)
    return Promise.resolve(
      new Response(JSON.stringify(reply.body), {
        status: reply.status,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Thân JSON của một lời gọi ghi mà mock bắt được — `{}` khi không phải chuỗi. */
export function parseJsonBody(init?: RequestInit): Record<string, unknown> {
  const body = init?.body
  if (typeof body !== 'string') {
    return {}
  }
  return JSON.parse(body) as Record<string, unknown>
}

export const HANDSHAKE: RouteReply = {
  status: 200,
  body: {
    server_version: APP_VERSION,
    min_client_version: APP_VERSION,
    control_schema_version: '4',
    deployment_mode: 'standalone',
  },
}

export const ME: RouteReply = {
  status: 200,
  body: {
    user_id: 1,
    username: 'ke_toan',
    locale: 'vi',
    must_change_password: false,
    expires_at: '2099-01-01T00:00:00Z',
    session_scope: 'full',
  },
}

export const ACCESS: RouteReply = {
  status: 200,
  body: { dataset_code: 'alpha', permissions: [], branch_ids: [1], acting_branch_id: 1 },
}

/** Bộ route nền mà mọi màn hình nghiệp vụ đều cần khi khởi động. */
export function baseRoutes(): FakeRoutes {
  return {
    '/system/handshake': HANDSHAKE,
    '/auth/me': ME,
    '/system/access': ACCESS,
  }
}

/** Một trang danh mục rỗng — trả cho các lượt nạp cây/lookup không tham gia bài test. */
export const EMPTY_PAGE: RouteReply = { status: 200, body: { items: [], total: 0 } }

/** Gieo phiên hợp lệ vào localStorage để SessionProvider khôi phục thẳng vào `ready`. */
export function seedSession(): void {
  localStorage.setItem(
    `ket.session:${window.location.origin}`,
    JSON.stringify({
      token: 'phien-test',
      expiresAt: '2099-01-01T00:00:00Z',
      datasetCode: 'alpha',
    }),
  )
}

/** Vẽ ứng dụng tại một đường dẫn của nhóm màn hình 07, với bộ route đã khai. */
export function renderFeatureAt(path: string): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="danh-muc-thiet-lap">
                <Route index element={<Navigate to="danh-muc/doi-tac" replace />} />
                <Route path="danh-muc/:segment" element={<CatalogListPage />} />
                <Route path="doi-tac/:id" element={<PartnerPage />} />
                <Route path="thiet-lap" element={<SettingsPage />} />
              </Route>
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}

/** Dòng danh mục đủ trường chung — đắp thêm trường riêng bằng spread. */
export function catalogRow(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 1,
    uid: '00000000-0000-7000-8000-000000000001',
    code: 'MA01',
    name: 'Bản ghi mẫu',
    name_en: null,
    parent_id: null,
    path: '/1',
    level: 0,
    is_group: false,
    is_active: true,
    branch_id: null,
    row_version: 1,
    ...overrides,
  }
}
