/**
 * Thanh chọn bên trong nhóm màn hình Bán hàng (bộ xương màn 01 dùng lại, lát
 * 7H-2a): Chứng từ bán hàng và "Ai còn nợ mình" — dẫn sang danh mục báo cáo
 * (tuổi nợ phải thu của 7G-2b). Hóa đơn điện tử là nhóm 02, có sub-nav riêng.
 *
 * `NavLink` thật, không state cục bộ — cùng lý do với nhóm 01/03/07/09.
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
      aria-label={t('nav.ban-hang')}
      className="w-[224px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
    >
      <ul>
        <li>
          {/* Không `end`: form chung-tu/moi và chung-tu/:id vẫn đánh dấu mục này. */}
          <NavLink to="/ban-hang/chung-tu" className={LINK_CLASS}>
            {t('sales.nav.invoices')}
          </NavLink>
        </li>
        <li>
          <NavLink to="/so-sach-thue/bao-cao" className={LINK_CLASS}>
            {t('sales.nav.receivables')}
          </NavLink>
        </li>
      </ul>
    </nav>
  )
}
