/**
 * Bộ dựng test cho nhóm màn hình Tiền vào tiền ra — cùng triết lý với
 * `so-sach-thue/feature-test-utils.tsx`: dựng cả cây (providers → phiên →
 * truy vấn → định tuyến), chỉ giả lập `fetch`.
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

import { BankVoucherForm } from './bank-voucher-form'
import { CashflowPage } from './cashflow-page'
import { CashVoucherForm } from './cash-voucher-form'
import { CountSheetPage } from './count-sheet-page'

export interface RouteReply {
  readonly status: number
  readonly body: unknown
}

export type FakeRoutes = Record<
  string,
  RouteReply | ((init?: RequestInit, url?: string) => RouteReply)
>

/** Giả lập server: khớp theo pathname (bỏ query) với phần đuôi của khóa — cùng khuôn nhóm 07/09. */
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

/** Hàng thẻ trống — cho bài test không quan tâm màn danh sách sau khi lưu. */
export const EMPTY_OVERVIEW: RouteReply = {
  status: 200,
  body: {
    as_of: '2026-08-27',
    ledger: 0,
    cash_accounts: [],
    bank_accounts: [],
    unassigned_deposit: '0',
  },
}

/** Danh mục chiều — trống ở bài test không dùng chiều nào. */
export function dimensionCatalogRoutes(): FakeRoutes {
  return {
    '/master/partners': EMPTY_LIST,
    '/master/employees': EMPTY_LIST,
    '/master/cost_objects': EMPTY_LIST,
    '/master/projects': EMPTY_LIST,
    '/master/contracts': EMPTY_LIST,
    '/master/expense_items': EMPTY_LIST,
    '/master/items': EMPTY_LIST,
    '/master/warehouses': EMPTY_LIST,
  }
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

/** Vẽ ứng dụng tại một đường dẫn của nhóm màn hình Tiền vào tiền ra. */
export function renderFeatureAt(path: string): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="tien-vao-tien-ra">
                <Route index element={<Navigate to="giao-dich" replace />} />
                <Route path="giao-dich" element={<CashflowPage />} />
                <Route path="giao-dich/phieu/moi" element={<CashVoucherForm />} />
                <Route path="giao-dich/phieu/:id" element={<CashVoucherForm />} />
                <Route path="giao-dich/ngan-hang/moi" element={<BankVoucherForm />} />
                <Route path="giao-dich/ngan-hang/:id" element={<BankVoucherForm />} />
                <Route path="kiem-ke-quy" element={<CountSheetPage />} />
              </Route>
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}
