/**
 * Bảng cân đối tài khoản — chọn năm/kỳ mới nhất mặc định, băng cảnh báo khi
 * `stale=true` (số tính thẳng từ sổ cái, chưa tính lại snapshot).
 */

import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { baseRoutes, mockServer, renderFeatureAt, seedSession } from './feature-test-utils'

describe('bảng cân đối tài khoản', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('kỳ đang chờ tính lại thì hiện băng cảnh báo, số vẫn hiện định dạng nghìn', async () => {
    mockServer({
      ...baseRoutes(),
      '/fiscal-years': {
        status: 200,
        body: {
          items: [
            {
              id: 1,
              code: '2026',
              start_date: '2026-01-01',
              end_date: '2026-12-31',
              accounting_scheme: 'tt200',
              base_currency: 'VND',
              is_closed: false,
              periods: [
                {
                  id: 10,
                  fiscal_year_id: 1,
                  period_no: 1,
                  start_date: '2026-01-01',
                  end_date: '2026-01-31',
                  locked_at: null,
                  locked_by: null,
                },
              ],
            },
          ],
        },
      },
      '/ledger/trial-balance': {
        status: 200,
        body: {
          ledger: 0,
          period_id: 10,
          branch_id: null,
          stale: true,
          rows: [
            {
              account_id: 1,
              account_code: '111',
              account_name: 'Tiền mặt',
              opening_debit: '1000000',
              opening_credit: '0',
              period_debit: '500000',
              period_credit: '0',
              closing_debit: '1500000',
              closing_credit: '0',
            },
          ],
        },
      },
    })

    renderFeatureAt('/so-sach-thue/bang-can-doi-tai-khoan')

    expect(
      await screen.findByText('Số đang tính thẳng từ sổ cái — chờ tính lại snapshot.'),
    ).toBeInTheDocument()
    expect(await screen.findByText('Tiền mặt')).toBeInTheDocument()
    expect(screen.getByText('1.500.000')).toBeInTheDocument()
  })
})
