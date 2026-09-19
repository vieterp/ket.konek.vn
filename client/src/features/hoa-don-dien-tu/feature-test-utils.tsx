/**
 * Bộ dựng test cho nhóm màn hình Hóa đơn điện tử — máy giả lập server và
 * phiên dùng lại của nhóm 03 (bài test import thẳng
 * `tien-vao-tien-ra/feature-test-utils`). Tệp này chỉ thêm cây route của nhóm
 * và hai trang bắt đường sang nhóm 01/02 (form mua, form bán) để bài test đọc
 * được đường đã chuyển sang.
 *
 * Tên tệp không mang `.test` nên vitest không thu thập nó như một bài test.
 */

import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'

import { CashPaymentStub } from '@/features/mua-hang/cash-payment-stub'

import { EInvoiceListPage } from './einvoice-list-page'
import { ErrorWizard } from './error-wizard'

/** Vẽ ứng dụng tại một đường dẫn của nhóm màn hình Hóa đơn điện tử. */
export function renderFeatureAt(path: string): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="hoa-don-dien-tu">
                <Route index element={<EInvoiceListPage mode="outbound" />} />
                <Route path="dau-vao" element={<EInvoiceListPage mode="inbound" />} />
                <Route path=":id/xu-ly" element={<ErrorWizard />} />
              </Route>
              {/* Stub in đường + query để bài test đọc nơi đã chuyển sang. */}
              <Route path="ban-hang/chung-tu/*" element={<CashPaymentStub />} />
              <Route path="mua-hang/chung-tu/*" element={<CashPaymentStub />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}

export const INVOICE_ID = 'eeeeeeee-1111-0000-0000-000000000001'
export const VOUCHER_ID = 'bbbbbbbb-1111-0000-0000-000000000001'

/** Một hàng lưới hóa đơn — ghi đè trường cần cho từng bài. */
export function listItem(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: INVOICE_ID,
    branch_id: 1,
    source_voucher_id: VOUCHER_ID,
    invoice_form_id: 7,
    invoice_no: '00004129',
    invoice_date: '2026-08-12',
    status: 3,
    tax_authority_status: 2,
    tax_authority_code: 'M1-26',
    tax_authority_message: null,
    lookup_code: null,
    replaces_invoice_id: null,
    adjusts_invoice_id: null,
    issued_at: '2026-08-12T08:12:00+07:00',
    sent_at: null,
    sent_to: null,
    voucher_no: 'BH26-00001',
    customer_id: 601,
    customer_code: 'KH01',
    customer_name: 'CT TNHH Hoàng Long',
    currency_code: 'VND',
    total_fc: '367740000',
    form_no: '1',
    serial: 'C26TKN',
    supersedes_no: null,
    superseded_by_no: null,
    ...overrides,
  }
}

export function listReply(
  items: Record<string, unknown>[],
  counts: Record<string, number> = {},
): { status: number; body: unknown } {
  return {
    status: 200,
    body: { items, total: items.length, page: 1, page_size: 50, counts_by_status: counts },
  }
}

export const OUTBOX_EMPTY = { status: 200, body: { items: [], total: 0, due_now: 0 } }

/** Bảng quyết định năm dòng — khuôn `docs/srs/07` §4.4 mà server seed. */
export const ERROR_FLOWS = {
  status: 200,
  body: [
    { code: 'TT-CHUA', error_kind: 0, buyer_declared: false, remedy: 0, legal_basis: 'NĐ 123 Đ.19 k.2a' },
    { code: 'TT-ROI', error_kind: 0, buyer_declared: true, remedy: 1, legal_basis: 'NĐ 123 Đ.19 k.2b' },
    { code: 'TIEN-CHUA', error_kind: 1, buyer_declared: false, remedy: 0, legal_basis: 'NĐ 123 Đ.19 k.2a' },
    { code: 'TIEN-ROI', error_kind: 1, buyer_declared: true, remedy: 2, legal_basis: 'NĐ 123 Đ.19 k.2b' },
    { code: 'HUY', error_kind: 2, buyer_declared: null, remedy: 3, legal_basis: 'NĐ 123 Đ.19 k.1' },
  ],
}
