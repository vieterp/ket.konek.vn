/**
 * Thanh chọn bên trong nhóm màn hình Mua hàng (design màn 01, sub-nav trái):
 * Chứng từ mua hàng (lát 7H-1) và "Mình còn nợ ai" — dẫn sang danh mục báo cáo
 * (tuổi nợ phải trả của 7G-1). Hai mục design vẽ nhưng chưa có phân hệ ("Đơn
 * mua hàng", "Trả tiền · Đối trừ nợ") không dựng trước.
 *
 * `NavLink` thật, không state cục bộ — cùng lý do với nhóm 03/07/09.
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
      aria-label={t('nav.mua-hang')}
      className="w-[224px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
    >
      <ul>
        <li>
          {/* Không `end`: form chung-tu/moi và chung-tu/:id vẫn đánh dấu mục này. */}
          <NavLink to="/mua-hang/chung-tu" className={LINK_CLASS}>
            {t('purchase.nav.invoices')}
          </NavLink>
        </li>
        <li>
          <NavLink to="/so-sach-thue/bao-cao" className={LINK_CLASS}>
            {t('purchase.nav.payables')}
          </NavLink>
        </li>
      </ul>
    </nav>
  )
}
