/**
 * Màn đối chiếu hai khung (U5): dòng ĐÃ khớp mờ + gỡ khớp được; dòng CHƯA khớp
 * chọn vào thì khung phải hiện candidates và Ghép POST đúng thân; khớp tự động
 * báo lại con số ba phần. Khung phải mặc định = phía sổ còn lệch.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  baseRoutes,
  mockServer,
  parseJsonBody,
  renderFeatureAt,
  seedSession,
  type FakeRoutes,
} from './feature-test-utils'

const STATEMENT_ID = 'bbbbbbbb-0000-0000-0000-000000000001'
const MATCHED_VOUCHER = 'cccccccc-0000-0000-0000-000000000001'
const LINE_MATCHED = 'dddddddd-0000-0000-0000-000000000001'
const LINE_OPEN = 'dddddddd-0000-0000-0000-000000000002'
const CANDIDATE_VOUCHER = 'cccccccc-0000-0000-0000-000000000002'

const DETAIL_ROUTE = {
  status: 200,
  body: {
    statement: {
      id: STATEMENT_ID,
      bank_account_id: 21,
      statement_date: '2026-08-20',
      opening_balance: null,
      closing_balance: null,
      profile_id: 7,
      content_hash: 'hash-1',
      imported_by: 1,
      imported_at: '2026-08-21T03:00:00Z',
    },
    lines: [
      {
        id: LINE_MATCHED,
        line_no: 1,
        txn_date: '2026-08-19',
        reference_no: 'REF-1',
        description: 'đã khớp',
        debit: '0',
        credit: '500000',
        matched_voucher_id: MATCHED_VOUCHER,
        match_kind: 1,
      },
      {
        id: LINE_OPEN,
        line_no: 2,
        txn_date: '2026-08-20',
        reference_no: 'REF-2',
        description: 'chưa khớp',
        debit: '250000',
        credit: '0',
        matched_voucher_id: null,
        match_kind: 0,
      },
    ],
  },
}

function pageRoutes(): FakeRoutes {
  return {
    ...baseRoutes(),
    [`/bank/statements/${STATEMENT_ID}`]: DETAIL_ROUTE,
    '/bank/reconciliation': {
      status: 200,
      body: {
        bank_account_id: 21,
        as_of: '2026-08-20',
        unmatched_statement_lines: [],
        unmatched_vouchers: [
          {
            voucher_id: CANDIDATE_VOUCHER,
            voucher_no: 'UNC26-00009',
            posting_date: '2026-08-18',
            kind: 1,
            reference_no: null,
            description: 'chưa thấy trên sao kê',
            net_fc: '-250000',
          },
        ],
        statement_total_unmatched_in: '0',
        statement_total_unmatched_out: '250000',
      },
    },
    [`/bank/statements/lines/${LINE_OPEN}/candidates`]: {
      status: 200,
      body: {
        items: [
          {
            voucher_id: CANDIDATE_VOUCHER,
            voucher_no: 'UNC26-00009',
            posting_date: '2026-08-18',
            kind: 1,
            reference_no: null,
            description: 'ứng viên',
            net_fc: '-250000',
          },
        ],
      },
    },
  }
}

describe('màn đối chiếu hai khung', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    seedSession()
  })

  it('dòng đã khớp mờ đi; khung phải mặc định là phía sổ còn lệch', async () => {
    mockServer(pageRoutes())

    renderFeatureAt(`/tien-vao-tien-ra/doi-chieu/${STATEMENT_ID}`)

    const matchedRow = await screen.findByRole('button', { name: /đã khớp/ })
    expect(matchedRow.className).toContain('opacity-50')

    expect(await screen.findByText('Phía sổ kế toán còn lệch')).toBeInTheDocument()
    expect(screen.getByText(/UNC26-00009/)).toBeInTheDocument()
  })

  it('chọn dòng chưa khớp → candidates hiện, Ghép POST voucher_id đúng', async () => {
    const fetchMock = mockServer({
      ...pageRoutes(),
      [`/bank/statements/lines/${LINE_OPEN}/actions/match`]: { status: 204, body: null },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/tien-vao-tien-ra/doi-chieu/${STATEMENT_ID}`)

    await user.click(await screen.findByRole('button', { name: /chưa khớp/ }))
    await user.click(await screen.findByRole('button', { name: 'Ghép' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith(`/bank/statements/lines/${LINE_OPEN}/actions/match`),
      )
      expect(call).toBeDefined()
      expect(parseJsonBody(call?.[1] as RequestInit)).toEqual({
        voucher_id: CANDIDATE_VOUCHER,
      })
    })
  })

  it('chọn dòng đã khớp → nút Gỡ khớp POST unmatch', async () => {
    const fetchMock = mockServer({
      ...pageRoutes(),
      [`/bank/statements/lines/${LINE_MATCHED}/actions/unmatch`]: { status: 204, body: null },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/tien-vao-tien-ra/doi-chieu/${STATEMENT_ID}`)

    await user.click(await screen.findByRole('button', { name: /đã khớp/ }))
    await user.click(await screen.findByRole('button', { name: 'Gỡ khớp' }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) =>
        String(entry[0]).endsWith(`/bank/statements/lines/${LINE_MATCHED}/actions/unmatch`),
      )
      expect(call).toBeDefined()
    })
  })

  it('khớp tự động: POST auto-match rồi báo lại ba con số', async () => {
    mockServer({
      ...pageRoutes(),
      [`/bank/statements/${STATEMENT_ID}/actions/auto-match`]: {
        status: 200,
        body: { matched: 4, unmatched_lines: 2, ambiguous_lines: 1 },
      },
    })
    const user = userEvent.setup()

    renderFeatureAt(`/tien-vao-tien-ra/doi-chieu/${STATEMENT_ID}`)

    await user.click(await screen.findByRole('button', { name: 'Khớp tự động' }))
    expect(
      await screen.findByText(
        'Đã khớp 4 dòng; còn 2 dòng chưa khớp, 1 dòng nhập nhằng cần khớp tay.',
      ),
    ).toBeInTheDocument()
  })
})
