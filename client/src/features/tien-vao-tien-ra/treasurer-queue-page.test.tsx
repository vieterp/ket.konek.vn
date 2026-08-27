/**
 * Màn thủ quỹ (U6): hàng đợi → chọn phiếu → Ghi sổ quỹ POST một lô đúng thân
 * kèm khóa chống trùng; ngày tùy chọn có trần `max` = hôm nay (quyết định user
 * 2026-08-27 — server vẫn là cổng chính, ô ngày chặn sớm); 422 ngày sai ra
 * thông điệp đọc được.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { IDEMPOTENCY_HEADER } from '@/lib/api-client'
import { todayIso } from '@/features/so-sach-thue/local-date'

import {
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
} from './feature-test-utils'

const RECEIPT_ID = 'aaaaaaaa-0000-0000-0000-000000000001'
const PAYMENT_ID = 'aaaaaaaa-0000-0000-0000-000000000002'

const QUEUE_ROUTE = {
  status: 200,
  body: {
    items: [
      {
        voucher_id: RECEIPT_ID,
        voucher_no: 'PT26-00001',
        document_type: 'PT',
        branch_id: 1,
        posting_date: '2026-08-20',
        cash_account_id: 11,
        is_receipt: true,
        amount: '400000',
        payer_receiver_name: 'Người nộp A',
        description: 'thu tiền',
      },
      {
        voucher_id: PAYMENT_ID,
        voucher_no: 'PC26-00002',
        document_type: 'PC',
        branch_id: 1,
        posting_date: '2026-08-21',
        cash_account_id: 11,
        is_receipt: false,
        amount: '150000',
        payer_receiver_name: null,
        description: 'chi tiền',
      },
    ],
  },
}

function pageRoutes(): FakeRoutes {
  return {
    ...baseRoutes(),
    '/treasurer/queue': QUEUE_ROUTE,
  }
}

describe('màn thủ quỹ — hàng đợi + ghi sổ hàng loạt', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('chọn 2 phiếu, ghi theo ngày chứng từ: POST một lô đúng thân + khóa chống trùng', async () => {
    const fetchMock = mockServer({
      ...pageRoutes(),
      '/treasurer/queue/actions/book': { status: 200, body: { booked_count: 2 } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/thu-quy')

    expect(await screen.findByText('PT26-00001')).toBeInTheDocument()
    await user.click(screen.getByLabelText('Chọn phiếu PT26-00001'))
    await user.click(screen.getByLabelText('Chọn phiếu PC26-00002'))
    await user.click(screen.getByRole('button', { name: 'Ghi sổ quỹ (2 phiếu)' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/treasurer/queue/actions/book'),
      )
      expect(call).toBeDefined()
      const init = call?.[1] as RequestInit
      expect(parseJsonBody(init)).toEqual({
        voucher_ids: [RECEIPT_ID, PAYMENT_ID],
        book_date_mode: 'posting_date',
        book_date: null,
      })
      expect(new Headers(init.headers).get(IDEMPOTENCY_HEADER)).toBeTruthy()
    })
    expect(await screen.findByText('Đã ghi sổ quỹ 2 phiếu.')).toBeInTheDocument()
  })

  it('ngày tùy chọn: ô ngày có max = hôm nay và thân POST mang ngày đã chọn', async () => {
    const fetchMock = mockServer({
      ...pageRoutes(),
      '/treasurer/queue/actions/book': { status: 200, body: { booked_count: 1 } },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/thu-quy')

    await user.click(await screen.findByLabelText('Chọn phiếu PT26-00001'))
    await user.click(screen.getByRole('radio', { name: 'Ngày tùy chọn' }))

    const dateInput = screen.getByLabelText('Ngày ghi sổ')
    // Trần trên tại chính ô nhập — sổ quỹ ghi việc ĐÃ làm.
    expect(dateInput).toHaveAttribute('max', todayIso())

    await user.click(screen.getByRole('button', { name: 'Ghi sổ quỹ (1 phiếu)' }))
    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith('/treasurer/queue/actions/book'),
      )
      expect(call).toBeDefined()
      const body = parseJsonBody(call?.[1] as RequestInit)
      expect(body.book_date_mode).toBe('custom')
      expect(body.book_date).toBe(todayIso())
    })
  })

  it('422 ngày ghi sổ sai từ server ra thông điệp đọc được', async () => {
    mockServer({
      ...pageRoutes(),
      '/treasurer/queue/actions/book': {
        status: 422,
        body: { error_code: 'treasurer.book_date_invalid' },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt('/tien-vao-tien-ra/thu-quy')

    await user.click(await screen.findByLabelText('Chọn phiếu PT26-00001'))
    await user.click(screen.getByRole('button', { name: 'Ghi sổ quỹ (1 phiếu)' }))

    expect(
      await screen.findByText(
        'Ngày ghi sổ quỹ không hợp lệ — không được nhỏ hơn ngày hạch toán trên chứng từ và không được ở tương lai.',
      ),
    ).toBeInTheDocument()
  })
})
