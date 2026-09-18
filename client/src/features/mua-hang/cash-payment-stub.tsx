/**
 * Trang đích giả của "Lập phiếu chi" trong bài test nhóm Mua hàng — in ra query
 * để bài test khẳng định NCC được điền sẵn. Tách tệp riêng vì eslint
 * react-refresh không cho một tệp vừa có component vừa export hàm thường.
 * Không dùng ngoài test.
 */

import type { ReactElement } from 'react'
import { useLocation } from 'react-router-dom'

export function CashPaymentStub(): ReactElement {
  const location = useLocation()
  return <p data-testid="cash-payment-stub">{location.search}</p>
}
