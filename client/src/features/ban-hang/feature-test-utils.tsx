/**
 * Bộ dựng test cho nhóm màn hình Bán hàng — máy giả lập server và phiên dùng
 * lại của nhóm 03 (bài test import thẳng `tien-vao-tien-ra/feature-test-utils`;
 * không re-export ở đây vì eslint react-refresh coi đó là "tệp trộn component
 * và hằng"). Tệp này chỉ thêm cây route của nhóm và hai trang bắt đường "Lập
 * phiếu thu" / "Phát hành hóa đơn" để bài test đọc được query đã chuyển sang.
 *
 * Tên tệp không mang `.test` nên vitest không thu thập nó như một bài test.
 */

import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { MemoryRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'

import { CashPaymentStub } from '@/features/mua-hang/cash-payment-stub'

import { SalesInvoiceForm } from './sales-invoice-form'
import { SalesListPage } from './sales-list-page'

/** Vẽ ứng dụng tại một đường dẫn của nhóm màn hình Bán hàng. */
export function renderFeatureAt(path: string): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="ban-hang">
                <Route index element={<Navigate to="chung-tu" replace />} />
                <Route path="chung-tu" element={<SalesListPage />} />
                <Route path="chung-tu/moi" element={<SalesInvoiceForm />} />
                <Route path="chung-tu/:id" element={<SalesInvoiceForm />} />
              </Route>
              <Route path="tien-vao-tien-ra/giao-dich/phieu/moi" element={<CashPaymentStub />} />
              {/* Nhóm 02 chưa có màn (7H-3) — stub cùng ruột: in query để bài test đọc. */}
              <Route path="hoa-don-dien-tu" element={<CashPaymentStub />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}

/** Một dòng lưới chứng từ bán — ghi đè trường cần cho từng bài. */
export function listItem(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 'bbbbbbbb-1111-0000-0000-000000000001',
    voucher_no: 'BH26-00001',
    branch_id: 1,
    document_date: '2026-09-10',
    posting_date: '2026-09-10',
    status: 2,
    currency_code: 'VND',
    kind: 0,
    customer_id: 601,
    customer_code: 'KH01',
    customer_name: 'CT CP Xây dựng Nam Long',
    invoice_form: null,
    invoice_serial: null,
    invoice_no: null,
    invoice_date: null,
    has_live_einvoice: true,
    einvoice_serial: 'C26TGE',
    einvoice_no: '00000012',
    total_fc: '642600000',
    remaining_fc: '642600000',
    due_date: '2026-10-25',
    days_overdue: null,
    ...overrides,
  }
}
