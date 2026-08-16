/**
 * Màn hình đăng nhập (FR-NFR-010/016).
 *
 * Chi tiết quan trọng nhất ở đây là **mã 2FA chỉ hiện khi server đòi**. Hợp
 * đồng của `/auth/login` (2B-1a) là: gửi lần đầu không kèm mã; tài khoản nào
 * bắt buộc hai lớp thì server trả `auth.totp_required`, client hỏi mã rồi gửi
 * lại. Nhờ vậy phần lớn người dùng — những người không bật 2FA — không bao giờ
 * nhìn thấy một ô mà họ không có gì để điền.
 *
 * Mật khẩu **không** bị xóa giữa hai lần gửi đó: người dùng vừa gõ nó xong,
 * bắt gõ lại chỉ vì hệ thống cần thêm một mã là ma sát do ta tự tạo ra.
 */

import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button, TextField } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

const TOTP_REQUIRED = 'auth.totp_required'

export function LoginPage(): ReactElement {
  const { t } = useI18n()
  const { login } = useSession()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [totpCode, setTotpCode] = useState('')
  const [needsTotp, setNeedsTotp] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await login(username, password, needsTotp ? totpCode : undefined)
    } catch (caught) {
      if (caught instanceof ApiError && caught.errorCode === TOTP_REQUIRED) {
        // Không phải lỗi của người dùng — chỉ là bước còn thiếu. Hiện ô mã và
        // giữ nguyên mọi thứ đã gõ.
        setNeedsTotp(true)
      } else if (caught instanceof ApiError) {
        setError(translateErrorCode(t, caught.errorCode))
      } else {
        setError(translateErrorCode(t, 'transport.unreachable'))
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthShell title={t('login.title')}>
      <form
        className="flex flex-col gap-4"
        onSubmit={(event) => {
          void handleSubmit(event)
        }}
      >
        <p className="text-sm text-text-muted">{t('login.subtitle')}</p>

        {error !== null && <Alert tone="error">{error}</Alert>}

        <TextField
          label={t('login.username')}
          name="username"
          autoComplete="username"
          required
          autoFocus
          value={username}
          onChange={(event) => {
            setUsername(event.target.value)
          }}
        />
        <TextField
          label={t('login.password')}
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => {
            setPassword(event.target.value)
          }}
        />
        {needsTotp && (
          <TextField
            label={t('login.totpCode')}
            name="totpCode"
            inputMode="numeric"
            autoComplete="one-time-code"
            required
            autoFocus
            hint={t('login.totpHint')}
            value={totpCode}
            onChange={(event) => {
              setTotpCode(event.target.value)
            }}
          />
        )}

        <Button type="submit" disabled={submitting}>
          {submitting ? t('login.submitting') : t('login.submit')}
        </Button>
      </form>
    </AuthShell>
  )
}
