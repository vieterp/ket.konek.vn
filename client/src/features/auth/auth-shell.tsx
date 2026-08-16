/**
 * Khung chung của các màn hình trước khi vào ứng dụng.
 *
 * Bốn màn hình dùng nó (đăng nhập, đổi mật khẩu tạm, đăng ký 2FA, cần cập
 * nhật). Chúng là **cùng một** khoảnh khắc với người dùng — "chưa vào được" —
 * nên chúng phải trông như một chỗ, không phải bốn trang rời rạc.
 *
 * Địa chỉ máy chủ hiện ở chân trang: khi một máy trạm trỏ nhầm sang bản demo
 * (chuyện có thật lúc triển khai), đây là dòng duy nhất trả lời được câu "tại
 * sao tài khoản của tôi không đăng nhập được".
 */

import type { ReactElement, ReactNode } from 'react'

import { SelectField } from '@/design-system/components'
import type { Locale } from '@/lib/i18n'
import { LOCALES, useI18n } from '@/lib/i18n'
import { APP_VERSION } from '@/lib/app-version'
import { useSession } from '@/lib/session'

export function AuthShell({
  title,
  children,
}: {
  readonly title: string
  readonly children: ReactNode
}): ReactElement {
  const { t, locale, setLocale } = useI18n()
  const { serverUrl } = useSession()

  return (
    <div className="flex min-h-screen items-center justify-center bg-screen p-6">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-end justify-between gap-4">
          <div>
            <p className="text-h3 font-semibold text-navy-700">{t('common.appName')}</p>
            <p className="text-sm text-text-muted">{t('common.tagline')}</p>
          </div>
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
        </div>

        <section className="rounded border-2 border-navy-700 bg-white p-6 shadow-sm">
          <h1 className="mb-4 text-lg font-semibold text-navy-700">{title}</h1>
          {children}
        </section>

        <footer className="mt-4 flex justify-between gap-4 text-xs text-text-muted">
          <span>
            {t('login.serverLabel')}: {serverUrl}
          </span>
          <span>{t('common.version', { version: APP_VERSION })}</span>
        </footer>
      </div>
    </div>
  )
}
