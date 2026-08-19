/**
 * Màn hình danh sách chứng từ — tab "việc còn thiếu" lấy từ BFF pending-issues,
 * chọn tab lọc đúng loại chứng từ + trạng thái "Đã cất".
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { baseRoutes, mockServer, renderFeatureAt, seedSession } from './feature-test-utils'

describe('màn hình danh sách chứng từ', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('hiện tab "Tất cả" + tab theo loại kèm số lượng từ BFF việc còn thiếu', async () => {
    mockServer({
      ...baseRoutes(),
      '/vouchers/pending-issues': {
        status: 200,
        body: {
          unposted: [
            {
              document_type: 'gl_journal',
              title: 'Chứng từ nghiệp vụ khác',
              count: 3,
              next_action: 'post',
              sample: [],
            },
          ],
          recalc_pending: [],
        },
      },
      '/vouchers': {
        status: 200,
        body: {
          items: [
            {
              id: 'aaaaaaaa-0000-0000-0000-000000000001',
              document_type: 'gl_journal',
              voucher_no: 'GLE00001',
              branch_id: 1,
              document_date: '2026-08-01',
              posting_date: '2026-08-01',
              period_id: 1,
              currency_code: 'VND',
              exchange_rate: '1',
              description: 'Chi phí văn phòng',
              status: 1,
              row_version: 1,
              created_at: '2026-08-01T00:00:00Z',
              created_by: 1,
              posted_at: null,
              posted_by: null,
              cashflow_activity: null,
            },
          ],
          page: 1,
          page_size: 50,
          total: 1,
        },
      },
    })

    renderFeatureAt('/so-sach-thue/chung-tu')

    expect(await screen.findByRole('tab', { name: /Tất cả/ })).toBeInTheDocument()
    expect(await screen.findByRole('tab', { name: /Chứng từ nghiệp vụ khác/ })).toHaveTextContent('3')
    expect(await screen.findByText('GLE00001')).toBeInTheDocument()
    // Chưa ghi sổ (status 1) phải trông KHÁC "Đã ghi sổ" — tông "todo".
    expect(screen.getByText('Chưa ghi sổ')).toHaveAttribute('data-tone', 'todo')
  })

  it('nút "Ghi sổ" ở cột việc-tiếp-theo POST actions/post kèm khóa idempotency', async () => {
    const voucherId = 'aaaaaaaa-0000-0000-0000-000000000001'
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/vouchers/pending-issues': {
        status: 200,
        body: { unposted: [], recalc_pending: [] },
      },
      [`/vouchers/${voucherId}/actions/post`]: {
        status: 200,
        body: {
          id: voucherId,
          document_type: 'gl_journal',
          voucher_no: 'GLE00001',
          branch_id: 1,
          document_date: '2026-08-01',
          posting_date: '2026-08-01',
          period_id: 1,
          currency_code: 'VND',
          exchange_rate: '1',
          description: 'Chi phí văn phòng',
          status: 2,
          row_version: 2,
          created_at: '2026-08-01T00:00:00Z',
          created_by: 1,
          posted_at: '2026-08-01T00:00:00Z',
          posted_by: 1,
          cashflow_activity: null,
        },
      },
      '/vouchers': {
        status: 200,
        body: {
          items: [
            {
              id: voucherId,
              document_type: 'gl_journal',
              voucher_no: 'GLE00001',
              branch_id: 1,
              document_date: '2026-08-01',
              posting_date: '2026-08-01',
              period_id: 1,
              currency_code: 'VND',
              exchange_rate: '1',
              description: 'Chi phí văn phòng',
              status: 1,
              row_version: 1,
              created_at: '2026-08-01T00:00:00Z',
              created_by: 1,
              posted_at: null,
              posted_by: null,
              cashflow_activity: null,
            },
          ],
          page: 1,
          page_size: 50,
          total: 1,
        },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/so-sach-thue/chung-tu')
    await user.click(await screen.findByRole('button', { name: 'Ghi sổ' }))

    await waitFor(() => {
      const postCall = fetchMock.mock.calls.find((call) =>
        String(call[0]).includes(`/vouchers/${voucherId}/actions/post`),
      )
      expect(postCall).toBeDefined()
      const init = postCall?.[1] as RequestInit
      expect(init.method).toBe('POST')
      // RT-12: hành động đổi-trạng-thái phải mang khóa idempotency.
      expect(new Headers(init.headers).get('X-Idempotency-Key')).toBeTruthy()
    })
  })

  it('chọn một tab việc còn thiếu thì lọc danh sách theo đúng loại + trạng thái 1', async () => {
    const fetchMock = mockServer({
      ...baseRoutes(),
      '/vouchers/pending-issues': {
        status: 200,
        body: {
          unposted: [
            {
              document_type: 'gl_journal',
              title: 'Chứng từ nghiệp vụ khác',
              count: 1,
              next_action: 'post',
              sample: [],
            },
          ],
          recalc_pending: [],
        },
      },
      '/vouchers': { status: 200, body: { items: [], page: 1, page_size: 50, total: 0 } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/so-sach-thue/chung-tu')
    await user.click(await screen.findByRole('tab', { name: /Chứng từ nghiệp vụ khác/ }))

    await waitFor(() => {
      const listCalls = fetchMock.mock.calls
        .map((call) => String(call[0]))
        .filter((url) => url.includes('/api/v1/vouchers?'))
      expect(
        listCalls.some((url) => url.includes('type=gl_journal') && url.includes('status=1')),
      ).toBe(true)
    })
  })
})
