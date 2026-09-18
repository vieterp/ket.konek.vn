/**
 * Thanh chọn bên trong nhóm màn hình Hóa đơn điện tử (nhóm 02, lát 7H-3):
 * lưới hóa đơn (phát hành + đầu vào là hai tab trên cùng trang) và báo cáo
 * hóa đơn — dẫn sang danh mục báo cáo (7G-3).
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
      aria-label={t('nav.hoa-don-dien-tu')}
      className="w-[224px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
    >
      <ul>
        <li>
          {/* Không `end`: /dau-vao và /:id/xu-ly vẫn đánh dấu mục này. */}
          <NavLink to="/hoa-don-dien-tu" className={LINK_CLASS}>
            {t('einvoice.nav.invoices')}
          </NavLink>
        </li>
        <li>
          <NavLink to="/so-sach-thue/bao-cao" className={LINK_CLASS}>
            {t('einvoice.nav.reports')}
          </NavLink>
        </li>
      </ul>
    </nav>
  )
}
