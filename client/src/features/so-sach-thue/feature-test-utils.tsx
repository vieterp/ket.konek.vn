/**
 * Bộ dựng test cho nhóm màn hình Sổ sách & Thuế — cùng triết lý với
 * `danh-muc-thiet-lap/feature-test-utils.tsx`: dựng cả cây (providers → phiên
 * → truy vấn → định tuyến), chỉ giả lập `fetch`.
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

import { JournalVoucherForm } from './journal-voucher-form'
import { ReportCatalogPage } from './report-catalog-page'
import { TrialBalancePage } from './trial-balance-page'
import { VoucherListPage } from './voucher-list-page'

export interface RouteReply {
  readonly status: number
  readonly body: unknown
}

export type FakeRoutes = Record<
  string,
  RouteReply | ((init?: RequestInit, url?: string) => RouteReply)
>

/** Giả lập server: khớp theo pathname (bỏ query) với phần đuôi của khóa — cùng khuôn nhóm 07. */
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

/** Trang trống — trả cho lượt gọi danh mục/tra cứu không tham gia bài test. */
export const EMPTY_LIST: RouteReply = { status: 200, body: { items: [], total: 0 } }
export const EMPTY_PENDING_ISSUES: RouteReply = {
  status: 200,
  body: { unposted: [], recalc_pending: [] },
}

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

/** Vẽ ứng dụng tại một đường dẫn của nhóm màn hình Sổ sách & Thuế. */
/**
 * Vẽ MỘT thành phần tùy ý bên trong đúng cái cây mà màn hình thật chạy trong
 * đó (providers → phiên → truy vấn → định tuyến).
 *
 * Có để kiểm được các hook đọc dữ liệu mà không phải lái cả một form qua lưới
 * nhập liệu — `resolveMissingCodes` là ca đầu tiên (review 6G-2: đột biến "bỏ
 * lời gọi `fetchCodes`" sống sót vì không bài nào chạm tới đường ấy).
 */
export function renderWithSession(element: ReactElement): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route index element={element} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}


export function renderFeatureAt(path: string): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="so-sach-thue">
                <Route index element={<Navigate to="chung-tu" replace />} />
                <Route path="chung-tu" element={<VoucherListPage />} />
                <Route path="chung-tu/moi" element={<JournalVoucherForm />} />
                <Route path="chung-tu/:id" element={<JournalVoucherForm />} />
                <Route path="bang-can-doi-tai-khoan" element={<TrialBalancePage />} />
                <Route path="bao-cao" element={<ReportCatalogPage />} />
              </Route>
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}
