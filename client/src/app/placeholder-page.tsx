/**
 * Trang tạm của một nhóm màn hình chưa tới lượt thi công.
 *
 * Có mặt để **đường đi** của ứng dụng đúng ngay từ lát này: sidebar, URL và
 * điều hướng bàn phím kiểm được thật, và lát sau chỉ việc thay ruột từng nhóm.
 * Trang ghi rõ phase sở hữu nhóm đó — người mở nhầm không phải đi tra kế hoạch
 * để biết nó "hỏng" hay "chưa làm".
 */

import type { ReactElement } from 'react'

import { useI18n } from '@/lib/i18n'
import type { NavigationItem } from '@/app/navigation'

export function PlaceholderPage({ item }: { readonly item: NavigationItem }): ReactElement {
  const { t } = useI18n()

  return (
    <section className="rounded border border-border-default bg-white p-6">
      <h1 className="mb-2 text-lg font-semibold text-navy-700">
        {t('placeholder.title', { group: t(item.labelKey) })}
      </h1>
      <p className="text-sm text-text-muted">{t('placeholder.body')}</p>
      <p className="mt-2 text-xs text-text-muted">Phase {item.phase}</p>
    </section>
  )
}
