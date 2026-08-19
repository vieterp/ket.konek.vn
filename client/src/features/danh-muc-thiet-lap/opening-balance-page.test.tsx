/**
 * Màn hình Số dư ban đầu — không dùng `renderFeatureAt` của
 * `feature-test-utils.tsx` (tệp đó chưa biết route `so-du-ban-dau`, và không
 * thuộc phạm vi sở hữu tệp của lát này); dựng một bộ định tuyến tối giản
 * ngay tại đây, tái dùng `mockServer`/`seedSession`/`baseRoutes` — cùng
 * triết lý, không chép lại cách giả lập `fetch`.
 */

import type { ReactElement } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'

import { baseRoutes, mockServer, parseJsonBody, seedSession } from './feature-test-utils'
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
        id: 2,
        code: '2025',
        start_date: '2025-01-01',
        end_date: '2025-12-31',
        is_closed: false,
        accounting_scheme: 'full',
        base_currency: 'VND',
        periods: [],
      },
      {
        id: 1,
        code: '2024',
        start_date: '2024-01-01',
        end_date: '2024-12-31',
        is_closed: false,
        accounting_scheme: 'full',
        base_currency: 'VND',
        periods: [],
      },
    ],
  },
}

function openingBalanceRow(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 1,
    detail_kind: 0,
    account_code: '111',
    account_name: 'Tiền mặt',
    partner_code: null,
    partner_name: null,
    currency_code: 'VND',
    exchange_rate: '1',
    debit_fc: '1000000',
    credit_fc: '0',
    debit: '1000000',
    credit: '0',
    ...overrides,
  }
}

function jobDone(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 'job-1',
    type: 'posting.opening_balances.clear_group',
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
    result: null,
    ...overrides,
  }
}

describe('màn hình Số dư ban đầu', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('vẽ tab, dòng số dư và hai tổng; balanced=false hiện băng cảnh báo (FR-OPB-006)', async () => {
    mockServer({
      ...baseRoutes(),
      '/fiscal-years': FISCAL_YEARS,
      '/opening-balances': {
        status: 200,
        body: {
          items: [openingBalanceRow({})],
          total: 1,
          page: 1,
          page_size: 50,
          total_debit: '1500000',
          total_credit: '500000',
          balanced: false,
        },
      },
    })

    renderOpeningBalancePage()

    expect(await screen.findByText('111')).toBeInTheDocument()
    expect(screen.getByText('Tiền mặt')).toBeInTheDocument()
    expect(screen.getByText('TK thường')).toBeInTheDocument()
    expect(screen.getByText('1.000.000')).toBeInTheDocument()
    expect(screen.getByText('1.500.000')).toBeInTheDocument()
    expect(screen.getByText('500.000')).toBeInTheDocument()
    expect(
      screen.getByText(
        'Tổng dư Nợ và dư Có của (năm, sổ) này chưa cân — đối chiếu lại trước khi ghi sổ.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('Lệch Nợ/Có')).toBeInTheDocument()
  })

  it('xóa số dư nhóm: bấm hai lần mới xác nhận, xếp job clear_group, poll xong thì báo và nạp lại danh sách', async () => {
    let listCalls = 0
    let clearBody: Record<string, unknown> | null = null

    mockServer({
      ...baseRoutes(),
      '/fiscal-years': FISCAL_YEARS,
      '/opening-balances': () => {
        listCalls += 1
        return {
          status: 200,
          body: {
            items: [openingBalanceRow({})],
            total: 1,
            page: 1,
            page_size: 50,
            total_debit: '1000000',
            total_credit: '1000000',
            balanced: true,
          },
        }
      },
      '/jobs': (init) => {
        clearBody = parseJsonBody(init)
        return { status: 202, body: jobDone({}) }
      },
      '/jobs/job-1': {
        status: 200,
        body: jobDone({
          result: { fiscal_year_id: 2, ledger: 0, detail_kind: 0, deleted_rows: 5 },
        }),
      },
    })

    renderOpeningBalancePage()
    await screen.findByText('111')

    const user = userEvent.setup()
    const clearButton = await screen.findByRole('button', { name: 'Xóa số dư nhóm này' })
    await user.click(clearButton)
    expect(await screen.findByRole('button', { name: 'Bấm lần nữa để xác nhận xóa' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Bấm lần nữa để xác nhận xóa' }))

    expect(await screen.findByText('Đã xóa 5 dòng.')).toBeInTheDocument()
    expect(clearBody).toEqual({
      type: 'posting.opening_balances.clear_group',
      params: { fiscal_year_id: 2, ledger: 0, detail_kind: 0 },
    })
    await waitFor(() => {
      expect(listCalls).toBeGreaterThan(1)
    })
  })
})
