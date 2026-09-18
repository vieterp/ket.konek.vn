/**
 * Bộ dựng test cho nhóm màn hình Mua hàng — máy giả lập server và phiên dùng
 * lại của nhóm 03 (bài test import thẳng `tien-vao-tien-ra/feature-test-utils`;
 * không re-export ở đây vì eslint react-refresh coi đó là "tệp trộn component
 * và hằng"). Tệp này chỉ thêm cây route của nhóm và một trang bắt đường "Lập
 * phiếu chi" để bài test đọc được query đã chuyển sang.
 *
 * Tên tệp không mang `.test` nên vitest không thu thập nó như một bài test.
 */

import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { MemoryRouter, Navigate, Route, Routes } from 'react-router-dom'

import { AppProviders } from '@/app/providers'
import { SessionGate } from '@/app/session-gate'

import { CashPaymentStub } from './cash-payment-stub'
import { PurchaseInvoiceForm } from './purchase-invoice-form'
import { PurchaseListPage } from './purchase-list-page'

/** Vẽ ứng dụng tại một đường dẫn của nhóm màn hình Mua hàng. */
export function renderFeatureAt(path: string): void {
  render(
    (
      <AppProviders>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/" element={<SessionGate />}>
              <Route path="mua-hang">
                <Route index element={<Navigate to="chung-tu" replace />} />
                <Route path="chung-tu" element={<PurchaseListPage />} />
                <Route path="chung-tu/moi" element={<PurchaseInvoiceForm />} />
                <Route path="chung-tu/:id" element={<PurchaseInvoiceForm />} />
              </Route>
              <Route path="tien-vao-tien-ra/giao-dich/phieu/moi" element={<CashPaymentStub />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </AppProviders>
    ) as ReactElement,
  )
}

/** Một bản ghi danh mục tối thiểu cho lookup (`GET /master/{slug}`). */
export function catalogRow(id: number, code: string, name: string): Record<string, unknown> {
  return {
    id,
    uid: `uid-${String(id)}`,
    code,
    name,
    name_en: null,
    parent_id: null,
    path: String(id),
    level: 0,
    is_group: false,
    is_active: true,
    branch_id: null,
    row_version: 1,
  }
}

/** Một dòng lưới chứng từ mua — ghi đè trường cần cho từng bài. */
export function listItem(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 'bbbbbbbb-0000-0000-0000-000000000001',
    voucher_no: 'MH26-00001',
    branch_id: 1,
    document_date: '2026-09-10',
    posting_date: '2026-09-10',
    status: 2,
    currency_code: 'VND',
    kind: 0,
    vendor_id: 501,
    vendor_code: 'NCC01',
    vendor_name: 'CT TNHH Thép Việt Nhật',
    vendor_invoice_status: 0,
    vendor_invoice_form: '1',
    vendor_invoice_serial: 'C26TVN',
    vendor_invoice_no: '0009120',
    total_fc: '642600000',
    remaining_fc: '642600000',
    due_date: '2026-10-25',
    days_overdue: null,
    ...overrides,
  }
}
