/**
 * Đổi mật khẩu tạm — màn hình bắt buộc trước khi vào phần nghiệp vụ.
 *
 * Không phải một trang tùy chọn: server ép cùng luật này ở mọi endpoint khác
 * (`auth.password_change_required`, quyết định 2B-1b), nên client chỉ đưa
 * người dùng tới đúng chỗ sửa thay vì để họ đâm vào một chuỗi lỗi.
 *
 * Kiểm "hai lần nhập giống nhau" nằm ở client — đây là loại kiểm **duy nhất**
 * được phép ở client: nó không phải luật nghiệp vụ, nó chỉ bắt lỗi gõ trước khi
 * tốn một vòng mạng. Chính sách độ mạnh vẫn do server quyết
 * (`auth.password_too_weak`).
 */

import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button, TextField } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

export function ChangePasswordPage(): ReactElement {
  const { t } = useI18n()
  const { client, refresh } = useSession()

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    if (newPassword !== confirmation) {
      setError(t('passwordChange.mismatch'))
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await client.post<void>('/api/v1/auth/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
      })
      // Cờ `must_change_password` do server giữ; đọc lại `/auth/me` là cách duy
      // nhất biết chắc nó đã tắt, thay vì client tự đoán từ việc lệnh trên
      // không lỗi.
      await refresh()
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? translateErrorCode(t, caught.errorCode)
          : translateErrorCode(t, 'transport.unreachable'),
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthShell title={t('passwordChange.title')}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          void handleSubmit(event)
        }}
      >
        <p className="text-sm text-text-muted">{t('passwordChange.intro')}</p>

        {error !== null && <Alert tone="error">{error}</Alert>}

        <TextField
          label={t('passwordChange.currentPassword')}
          type="password"
          autoComplete="current-password"
          required
          autoFocus
          value={currentPassword}
          onChange={(event) => {
            setCurrentPassword(event.target.value)
          }}
        />
        <TextField
          label={t('passwordChange.newPassword')}
          type="password"
          autoComplete="new-password"
          required
          value={newPassword}
          onChange={(event) => {
            setNewPassword(event.target.value)
          }}
        />
        <TextField
          label={t('passwordChange.confirmPassword')}
          type="password"
          autoComplete="new-password"
          required
          value={confirmation}
          onChange={(event) => {
            setConfirmation(event.target.value)
          }}
        />

        <Alert tone="info">{t('passwordChange.otherSessionsWarning')}</Alert>

        <Button type="submit" disabled={submitting}>
          {t('passwordChange.submit')}
        </Button>
      </form>
    </AuthShell>
  )
}
