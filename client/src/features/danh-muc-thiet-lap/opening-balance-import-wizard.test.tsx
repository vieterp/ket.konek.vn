/**
 * Wizard nhập Excel số dư ban đầu — lượt kiểm sạch (`error_rows === 0`) mở
 * khóa nút "Nhập khẩu", rồi lượt ghi chạy job thứ hai và báo đã ghi. Job nền
 * giả lập trả `done` ngay từ lượt hỏi đầu, cùng lý do với
 * `import-wizard.test.tsx`: đường "job còn chạy" đã nằm trong hợp đồng
 * `refetchInterval` của `useJob`, không cần đồng hồ thật để chứng minh.
 *
 * Không dùng `renderFeatureAt` (chưa biết route `so-du-ban-dau`) — dựng bộ
 * định tuyến tối giản tại đây, tái dùng `mockServer`/`seedSession`/`baseRoutes`.
 */

import type { ReactElement } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'

import { baseRoutes, mockServer, seedSession } from './feature-test-utils'
import { OpeningBalancePage } from './opening-balance-page'

function renderOpeningBalancePage(): void {
  seedSession()
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={['/danh-muc-thiet-lap/so-du-ban-dau']}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="danh-muc-thiet-lap/so-du-ban-dau" element={<OpeningBalancePage />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}

const FISCAL_YEARS = {
  status: 200,
  body: {
    items: [
      {
        id: 1,
        code: '2025',
        start_date: '2025-01-01',
        end_date: '2025-12-31',
        is_closed: false,
        accounting_scheme: 'full',
        base_currency: 'VND',
        periods: [],
      },
    ],
  },
}

const EMPTY_OPENING_BALANCES = {
  status: 200,
  body: { items: [], total: 0, page: 1, page_size: 50, total_debit: '0', total_credit: '0', balanced: true },
}

function jobDone(result: Record<string, unknown>, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 'job-1',
    type: 'posting.opening_balances.import_validate',
    status: 'done',
    progress: 100,
    message: null,
    attempt: 1,
    branch_id: 1,
    cancel_requested: false,
    requested_by: 1,
    created_at: '2026-08-19T00:00:00Z',
    started_at: '2026-08-19T00:00:00Z',
    finished_at: '2026-08-19T00:00:01Z',
    resume_semantics: 'restart',
    result,
    ...overrides,
  }
}

function openingReport(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    fiscal_year_id: 1,
    ledger: 0,
    file_name: 'so-du-mau.xlsx',
    content_hash: 'abc123',
    total_rows: 3,
    error_rows: 0,
    errors: [],
    warnings: [],
    rows_by_kind: { '0': 3 },
    invoice_rows: 0,
    replaced_kinds: [0],
    total_debit: '3000000',
    total_credit: '3000000',
    committed: false,
    ...overrides,
  }
}

describe('wizard nhập Excel số dư ban đầu', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('tệp sạch: kiểm tra mở khóa Nhập khẩu, lượt ghi chạy job thứ hai và báo đã ghi', async () => {
    let commitRequested = false
    mockServer({
      ...baseRoutes(),
      '/fiscal-years': FISCAL_YEARS,
      '/opening-balances': EMPTY_OPENING_BALANCES,
      '/opening-balances/import/validate': {
        status: 202,
        body: { job_id: 'job-1', fiscal_year_id: 1, ledger: 0, file_name: 'so-du-mau.xlsx', content_hash: 'abc123' },
      },
      '/opening-balances/import/commit': (init) => {
        commitRequested = init?.method === 'POST'
        return { status: 202, body: { job_id: 'job-2' } }
      },
      '/jobs/job-1': { status: 200, body: jobDone(openingReport({})) },
      '/jobs/job-2': {
        status: 200,
        body: jobDone(openingReport({ committed: true }), { id: 'job-2', type: 'posting.opening_balances.import_commit' }),
      },
    })

    renderOpeningBalancePage()

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Nhập từ Excel' }))
    const fileInput = await screen.findByLabelText('Tệp Excel (.xlsx)')
    await user.upload(
      fileInput,
      new File(['noi dung'], 'so-du.xlsx', {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      }),
    )
    await user.click(screen.getByRole('button', { name: 'Kiểm tra dữ liệu' }))

    expect(await screen.findByText('Dữ liệu hợp lệ. Bấm "Nhập khẩu" để ghi.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Nhập khẩu' })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: 'Nhập khẩu' }))

    expect(
      await screen.findByText('Đã ghi xong. Đối chiếu lại tại danh sách số dư ban đầu.'),
    ).toBeInTheDocument()
    expect(commitRequested).toBe(true)
    expect(screen.getByRole('button', { name: 'Hoàn tất' })).toBeInTheDocument()
  })
})
