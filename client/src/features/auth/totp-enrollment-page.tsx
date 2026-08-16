/**
 * Đăng ký thiết bị xác thực hai lớp (FR-NFR-016, quyết định E2).
 *
 * Đây là màn hình gỡ điểm chặn H4 của lát 2B-1b: tài khoản mang vai trò nhạy
 * cảm mà chưa có thiết bị sinh mã nhận một **phiên hạn chế**
 * (`session_scope = totp_enrollment`) chỉ mở đúng hai endpoint dưới đây. Trước
 * khi có màn hình này, vòng đăng ký chỉ đi trọn được nếu có người chạm vào máy
 * chủ.
 *
 * Ba bước, đúng thứ tự server đòi:
 *
 * 1. nhập lại mật khẩu → `POST /auth/totp/enroll` trả `otpauth://` URI;
 * 2. quét mã QR (hoặc nhập tay chuỗi bí mật) vào ứng dụng xác thực;
 * 3. nhập mã đang hiện → `POST /auth/totp/confirm`.
 *
 * Sau bước 3, server **thu hồi** chính phiên hạn chế này: nó được cấp cho một
 * tài khoản chưa qua lớp thứ hai nên không được tự nâng cấp. Người dùng đăng
 * nhập lại và nhập mã — đó cũng là lần thử thật đầu tiên của thiết bị vừa đăng
 * ký, tức là lỗi cấu hình lộ ra ngay bây giờ chứ không phải sáng hôm sau.
 *
 * Mã QR vẽ **cục bộ** bằng `qrcode`: URI chứa bí mật dạng rõ và không được gửi
 * tới bất kỳ dịch vụ sinh ảnh nào, kể cả khi máy có internet.
 */

import type { FormEvent, ReactElement } from 'react'
import { useState } from 'react'
import QRCode from 'qrcode'

import type { Schemas } from '@api-types'

import { Alert, Button, TextField } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

type EnrollResponse = Schemas['TotpEnrollResponse']

/** Chuỗi bí mật để nhập tay khi máy quét không đọc được mã QR. */
function secretFrom(provisioningUri: string): string {
  try {
    return new URL(provisioningUri).searchParams.get('secret') ?? ''
  } catch {
    return ''
  }
}

export function TotpEnrollmentPage(): ReactElement {
  const { t } = useI18n()
  const { client, refresh } = useSession()

  const [password, setPassword] = useState('')
  const [provisioningUri, setProvisioningUri] = useState<string | null>(null)
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function report(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : translateErrorCode(t, 'transport.unreachable'),
    )
  }

  async function beginEnrollment(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const enrolled = await client.post<EnrollResponse>('/api/v1/auth/totp/enroll', {
        password,
      })
      setProvisioningUri(enrolled.provisioning_uri)
      setQrDataUrl(await QRCode.toDataURL(enrolled.provisioning_uri, { margin: 1, width: 220 }))
      // Mật khẩu đã dùng xong: giữ nó trong state suốt màn hình chỉ để nó nằm
      // trong ảnh chụp bộ nhớ và trong công cụ gỡ rối của React.
      setPassword('')
    } catch (caught) {
      report(caught)
    } finally {
      setBusy(false)
    }
  }

  async function confirmDevice(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await client.post<void>('/api/v1/auth/totp/confirm', { code })
      // Phiên hạn chế vừa bị server thu hồi. `refresh` sẽ nhận
      // `auth.not_authenticated` và lớp phiên tự đưa về màn hình đăng nhập —
      // không có nhánh riêng nào ở đây, nên đường "phiên bị thu hồi" chỉ có một
      // cách xử lý trong cả ứng dụng.
      await refresh()
    } catch (caught) {
      // Chỉ nuốt đúng lỗi của bước trên. Mã 2FA sai cũng là `401` nhưng mang mã
      // `auth.totp_code_invalid`, và nuốt nó nghĩa là người dùng gõ nhầm một
      // chữ số thì màn hình im lặng — còn bí mật vừa sinh thì mất.
      if (caught instanceof ApiError && caught.isSessionLost) {
        return
      }
      report(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <AuthShell title={t('totp.title')}>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-text-muted">{t('totp.intro')}</p>
        {error !== null && <Alert tone="error">{error}</Alert>}

        {provisioningUri === null ? (
          <form
            className="flex flex-col gap-4"
            onSubmit={(event) => {
              void beginEnrollment(event)
            }}
          >
            <TextField
              label={t('totp.password')}
              type="password"
              autoComplete="current-password"
              required
              autoFocus
              value={password}
              onChange={(event) => {
                setPassword(event.target.value)
              }}
            />
            <Button type="submit" disabled={busy}>
              {t('totp.begin')}
            </Button>
          </form>
        ) : (
          <form
            className="flex flex-col gap-4"
            onSubmit={(event) => {
              void confirmDevice(event)
            }}
          >
            <p className="text-sm text-text-default">{t('totp.scan')}</p>
            {qrDataUrl !== null && (
              <img
                src={qrDataUrl}
                alt={t('totp.qrAlt')}
                className="self-center rounded border border-border-default"
              />
            )}
            <details className="text-sm text-text-muted">
              <summary className="cursor-pointer">{t('totp.cantScan')}</summary>
              <code className="mt-2 block break-all rounded bg-surface p-2 text-xs">
                {secretFrom(provisioningUri)}
              </code>
            </details>
            <TextField
              label={t('totp.code')}
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              value={code}
              onChange={(event) => {
                setCode(event.target.value)
              }}
            />
            <Button type="submit" disabled={busy}>
              {t('totp.confirm')}
            </Button>
          </form>
        )}
      </div>
    </AuthShell>
  )
}
