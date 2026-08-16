/**
 * Đa ngôn ngữ (FR-NFR-034) và biên giới giữa mã lỗi của server và câu hiển thị.
 *
 * Bất biến "hai bản dịch cùng bộ khóa" **không** kiểm ở đây — nó là lỗi `tsc`
 * nhờ `Record<TranslationKey, string>` trong `en.ts` (quyết định H4). Tệp này
 * kiểm ba thứ chỉ lộ ra lúc chạy: đổi ngôn ngữ có vẽ lại không, nội suy tham
 * số có đúng không, và mã lỗi chưa có bản dịch thì hiện gì.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactElement } from 'react'
import { describe, expect, it } from 'vitest'

import { I18nProvider, translateErrorCode, useI18n } from '@/lib/i18n'

function Probe(): ReactElement {
  const { t, locale, setLocale } = useI18n()
  return (
    <div>
      <p data-testid="label">{t('login.title')}</p>
      <p data-testid="interpolated">{t('common.version', { version: '1.2.3' })}</p>
      <p data-testid="known">{translateErrorCode(t, 'auth.invalid_credentials')}</p>
      <p data-testid="unknown">{translateErrorCode(t, 'costing.step_not_configured')}</p>
      <button
        type="button"
        onClick={() => {
          setLocale(locale === 'vi' ? 'en' : 'vi')
        }}
      >
        đổi
      </button>
    </div>
  )
}

describe('i18n', () => {
  it('mặc định tiếng Việt và đổi được sang tiếng Anh không cần khởi động lại', async () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    )

    expect(screen.getByTestId('label')).toHaveTextContent('Đăng nhập')

    await userEvent.click(screen.getByRole('button', { name: 'đổi' }))

    expect(screen.getByTestId('label')).toHaveTextContent('Sign in')
  })

  it('nội suy tham số trong chuỗi', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    )

    expect(screen.getByTestId('interpolated')).toHaveTextContent('Phiên bản 1.2.3')
  })

  it('mã lỗi chưa có bản dịch vẫn cho người dùng thứ đọc cho bộ phận hỗ trợ', () => {
    render(
      <I18nProvider>
        <Probe />
      </I18nProvider>,
    )

    expect(screen.getByTestId('known')).toHaveTextContent('Tên đăng nhập hoặc mật khẩu không đúng.')
    // Phase sau thêm mã lỗi mới liên tục. Rơi về một ô trống hay một khóa lạ là
    // cách chắc chắn để người dùng không báo được sự cố.
    expect(screen.getByTestId('unknown')).toHaveTextContent('costing.step_not_configured')
  })
})
