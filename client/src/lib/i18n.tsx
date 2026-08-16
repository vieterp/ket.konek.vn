/**
 * Đa ngôn ngữ vi/en (FR-NFR-034) — tự viết, không thêm i18next (quyết định H4).
 *
 * Nhu cầu thật của v1 gọn hơn nhiều so với một thư viện i18n đầy đủ: tra khóa
 * phẳng và nội suy `{tên}`. Số, tiền và ngày **không** đi qua đây — chúng dùng
 * `Intl` của trình duyệt (`formatters.ts`), nơi luật số nhiều và định dạng địa
 * phương đã có sẵn và đúng hơn bất kỳ bảng nào ta tự viết.
 *
 * Đổi ngôn ngữ **không** khởi động lại ứng dụng: `locale` là state của React,
 * nên mọi màn hình đang mở vẽ lại ngay (tiêu chí phát hành của phase 2).
 */

import type { ReactElement, ReactNode } from 'react'
import { createContext, use, useCallback, useMemo, useState } from 'react'

import { en } from '@/locales/en'
import type { TranslationKey } from '@/locales/vi'
import { vi } from '@/locales/vi'

export type Locale = 'vi' | 'en'

export const LOCALES: readonly Locale[] = ['vi', 'en']

const CATALOGS: Record<Locale, Record<TranslationKey, string>> = { vi, en }

const LOCALE_STORAGE_KEY = 'ket.locale'

export type Translate = (key: TranslationKey, params?: Readonly<Record<string, string>>) => string

interface I18n {
  readonly locale: Locale
  readonly t: Translate
  readonly setLocale: (locale: Locale) => void
}

const I18nContext = createContext<I18n | null>(null)

function interpolate(template: string, params?: Readonly<Record<string, string>>): string {
  if (params === undefined) {
    return template
  }
  return template.replace(/\{(\w+)\}/g, (whole, name: string) => params[name] ?? whole)
}

/** Ngôn ngữ đã chọn lần trước, mặc định tiếng Việt (người dùng đích là kế toán VN). */
function initialLocale(): Locale {
  const stored = localStorage.getItem(LOCALE_STORAGE_KEY)
  return stored === 'en' ? 'en' : 'vi'
}

export function I18nProvider({ children }: { children: ReactNode }): ReactElement {
  const [locale, setLocaleState] = useState<Locale>(initialLocale)

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    localStorage.setItem(LOCALE_STORAGE_KEY, next)
    document.documentElement.lang = next
  }, [])

  const value = useMemo<I18n>(() => {
    const catalog = CATALOGS[locale]
    return {
      locale,
      setLocale,
      t: (key, params) => interpolate(catalog[key], params),
    }
  }, [locale, setLocale])

  return <I18nContext value={value}>{children}</I18nContext>
}

export function useI18n(): I18n {
  const value = use(I18nContext)
  if (value === null) {
    throw new Error('useI18n phải nằm trong <I18nProvider>')
  }
  return value
}

/**
 * Câu hiển thị cho một mã lỗi của server.
 *
 * Server trả **mã** ổn định, client dựng câu (hợp đồng lỗi, FR-NFR-050) — nên
 * chỗ này là biên giới giữa hai hệ. Mã chưa có bản dịch rơi về câu chung **kèm
 * chính mã đó**: người dùng vẫn có thứ đọc cho bộ phận hỗ trợ, thay vì một ô
 * trống hay một khóa lạ.
 */
export function translateErrorCode(t: Translate, errorCode: string): string {
  const key = `error.${errorCode}` as TranslationKey
  if (key in vi) {
    return t(key)
  }
  return t('error.unknown', { code: errorCode })
}
