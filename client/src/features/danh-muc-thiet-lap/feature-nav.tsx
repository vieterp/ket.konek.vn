/**
 * Thanh chọn bên trong nhóm màn hình Danh mục & Thiết lập.
 *
 * Đây là hai nơi **duy nhất** có "cấu hình" trong toàn ứng dụng (nguyên tắc
 * nhóm 07): mọi danh mục xếp theo mạch công việc, và mục Thiết lập đứng riêng
 * ở cuối. Dùng `NavLink` thật thay vì state: đường dẫn là địa chỉ gửi được cho
 * đồng nghiệp ("mở giúp danh mục kho"), và nút Back của trình duyệt phải chạy.
 */

import type { ReactElement } from 'react'
import { NavLink } from 'react-router-dom'
import { Settings } from 'lucide-react'

import { useI18n } from '@/lib/i18n'

import { CATALOG_GROUPS, catalogBySlug } from './catalog-registry'

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
      aria-label={t('nav.danh-muc-thiet-lap')}
      className="w-[224px] shrink-0 overflow-y-auto rounded border border-border-default bg-background p-3"
    >
      {CATALOG_GROUPS.map((group) => (
        <div key={group.labelKey} className="mb-3">
          <p className="mb-1 px-2 text-xs font-semibold uppercase tracking-wide text-text-muted">
            {t(group.labelKey)}
          </p>
          <ul>
            {group.slugs.map((slug) => {
              const catalog = catalogBySlug(slug)
              if (catalog === undefined) {
                return null
              }
              return (
                <li key={slug}>
                  <NavLink
                    to={`/danh-muc-thiet-lap/danh-muc/${catalog.urlSegment}`}
                    className={LINK_CLASS}
                  >
                    {t(catalog.titleKey)}
                  </NavLink>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
      <div className="border-t border-border-default pt-2">
        <NavLink to="/danh-muc-thiet-lap/thiet-lap" className={LINK_CLASS}>
          <span className="flex items-center gap-2">
            <Settings size={14} aria-hidden />
            {t('setup.title')}
          </span>
        </NavLink>
      </div>
    </nav>
  )
}
