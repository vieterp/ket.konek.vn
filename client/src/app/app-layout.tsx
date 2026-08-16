/**
 * Khung trang của phần nghiệp vụ: sidebar + topbar + thanh trạng thái.
 *
 * Bố cục theo design Konek Screens 2a (khung viền navy 2px, nền màn hình
 * `#F5F7FA`, icon lucide). Sidebar liệt kê **nhóm màn hình**, không phải module
 * backend — xem `navigation.ts`.
 *
 * Thanh trạng thái dưới cùng trả lời ba câu mà kế toán viên hỏi liên tục trong
 * ngày: *đang ở doanh nghiệp nào, chi nhánh nào, có ghi được không*. Câu thứ ba
 * (`readOnly`) quan trọng ngang hai câu đầu: một bản client cũ vẫn mở được mọi
 * màn hình, và nếu không nói rõ thì người dùng chỉ biết khi bấm Lưu.
 */

import type { ReactElement, ReactNode } from 'react'
import { NavLink } from 'react-router-dom'

import { Button, SelectField } from '@/design-system/components'
import { NAVIGATION } from '@/app/navigation'
import { useAccess } from '@/lib/access'
import { APP_VERSION } from '@/lib/app-version'
import type { Locale } from '@/lib/i18n'
import { LOCALES, useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

function Sidebar(): ReactElement {
  const { t } = useI18n()

  return (
    <nav aria-label={t('common.appName')} className="w-56 shrink-0 border-r border-border-default bg-surface">
      <div className="border-b-2 border-navy-700 px-4 py-4">
        <p className="font-semibold text-navy-700">{t('common.appName')}</p>
        <p className="text-xs text-text-muted">{t('common.tagline')}</p>
      </div>
      <ul className="flex flex-col gap-1 p-2">
        {NAVIGATION.map((item) => (
          <li key={item.path}>
            <NavLink
              to={item.path}
              end={item.path === '/'}
              className={({ isActive }) =>
                `flex items-center gap-2 rounded px-3 py-2 text-sm ${
                  isActive
                    ? 'bg-navy-700 text-white'
                    : 'text-text-default hover:bg-navy-50 hover:text-navy-700'
                }`
              }
            >
              <item.icon size={16} aria-hidden />
              {t(item.labelKey)}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}

function Topbar(): ReactElement {
  const { t, locale, setLocale } = useI18n()
  const { me, logout } = useSession()

  return (
    <header className="flex items-center justify-between gap-4 border-b border-border-default bg-white px-6 py-3">
      <span className="text-sm text-text-default">{me?.username ?? ''}</span>
      <div className="flex items-center gap-3">
        <SelectField
          label={t('common.language')}
          labelHidden
          value={locale}
          onChange={(event) => {
            setLocale(event.target.value as Locale)
          }}
          options={LOCALES.map((code) => ({
            value: code,
            label: code === 'vi' ? t('common.vietnamese') : t('common.english'),
          }))}
        />
        <Button
          variant="ghost"
          onClick={() => {
            void logout()
          }}
        >
          {t('common.signOut')}
        </Button>
      </div>
    </header>
  )
}

function StatusBar(): ReactElement {
  const { t } = useI18n()
  const { datasetCode, readOnly } = useSession()
  const access = useAccess()

  const branchCount = access.data?.branch_ids.length ?? 0
  const branchLabel =
    branchCount === 0
      ? t('status.noBranch')
      : branchCount === 1
        ? `#${String(access.data?.branch_ids[0] ?? '')}`
        : t('status.allBranches')

  return (
    <footer className="flex items-center gap-4 border-t border-border-default bg-surface px-6 py-2 text-xs text-text-muted">
      <span>
        {t('status.dataset')}: {datasetCode ?? '—'}
      </span>
      <span>
        {t('status.branch')}: {branchLabel}
      </span>
      {readOnly && (
        <span className="rounded bg-amber-100 px-2 py-0.5 font-semibold text-amber-900">
          {t('status.readOnly')}
        </span>
      )}
      <span className="ml-auto">{t('common.version', { version: APP_VERSION })}</span>
    </footer>
  )
}

export function AppLayout({ children }: { children: ReactNode }): ReactElement {
  return (
    <div className="flex min-h-screen bg-screen text-text-default">
      <Sidebar />
      <div className="flex min-h-screen flex-1 flex-col">
        <Topbar />
        <main className="flex-1 p-6">{children}</main>
        <StatusBar />
      </div>
    </div>
  )
}
