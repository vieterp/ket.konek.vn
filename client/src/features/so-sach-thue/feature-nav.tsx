/**
 * Thanh chọn bên trong nhóm màn hình Sổ sách & Thuế — hai màn hình của lát 4E:
 * Chứng từ (mặc định) và Bảng cân đối tài khoản.
 *
 * `NavLink` thật, không state cục bộ: cùng lý do với nhóm 07 — đường dẫn gửi
 * được cho đồng nghiệp và nút Back của trình duyệt phải chạy đúng.
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
      aria-label={t('nav.so-sach-thue')}
      className="w-[224px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
    >
      <ul>
        <li>
          {/* Không `end`: các trang con (chung-tu/moi, chung-tu/:id) vẫn phải
              đánh dấu mục "Chứng từ" đang chọn. */}
          <NavLink to="/so-sach-thue/chung-tu" className={LINK_CLASS}>
            {t('gl.nav.vouchers')}
          </NavLink>
        </li>
        <li>
          <NavLink to="/so-sach-thue/bang-can-doi-tai-khoan" className={LINK_CLASS}>
            {t('trialBalance.nav.title')}
          </NavLink>
        </li>
      </ul>
    </nav>
  )
}
