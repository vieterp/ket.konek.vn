/**
 * Thanh chọn bên trong nhóm màn hình Tiền vào tiền ra — lát 6F-1 có hai màn:
 * Giao dịch (mặc định — hàng thẻ + lưới) và Kiểm kê quỹ. Lát 6F-2 thêm Đối
 * chiếu ngân hàng, Sao kê và Thủ quỹ.
 *
 * `NavLink` thật, không state cục bộ — cùng lý do với nhóm 07/09: đường dẫn
 * gửi được cho đồng nghiệp và nút Back của trình duyệt phải chạy đúng.
 */

import type { ReactElement } from 'react'
import { NavLink } from 'react-router-dom'

import { useI18n } from '@/lib/i18n'

const LINK_CLASS = ({ isActive }: { isActive: boolean }): string =>
  `block rounded px-2 py-1 text-sm ${
    isActive
      ? 'bg-navy-50 font-semibold text-primary'
      : 'text-text-default hover:bg-primary/10 hover:text-primary'
  }`

export function FeatureNav(): ReactElement {
  const { t } = useI18n()

  return (
    <nav
      aria-label={t('nav.tien-vao-tien-ra')}
      className="w-[224px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
    >
      <ul>
        <li>
          {/* Không `end`: các trang con (phieu/moi, chung-tu-ngan-hang/:id…)
              vẫn phải đánh dấu mục "Giao dịch" đang chọn. */}
          <NavLink to="/tien-vao-tien-ra/giao-dich" className={LINK_CLASS}>
            {t('cashflow.nav.transactions')}
          </NavLink>
        </li>
        <li>
          <NavLink to="/tien-vao-tien-ra/kiem-ke-quy" className={LINK_CLASS}>
            {t('cashflow.nav.countSheets')}
          </NavLink>
        </li>
      </ul>
    </nav>
  )
}
