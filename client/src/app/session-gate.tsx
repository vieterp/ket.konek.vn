/**
 * Quyết định màn hình đầu tiên người dùng thấy, theo trạng thái phiên.
 *
 * Một chỗ duy nhất trong cả ứng dụng làm việc này. Trước đây (và ở nhiều dự án)
 * mỗi màn hình tự kiểm "đã đăng nhập chưa" rồi tự chuyển hướng — và cái giá là
 * một màn hình quên kiểm sẽ hiện ra rỗng, hoặc tệ hơn, hiện ra với dữ liệu của
 * phiên trước.
 *
 * Bảng dưới đây là toàn bộ luật:
 *
 * | Trạng thái | Màn hình |
 * | --- | --- |
 * | `starting` | đang tải |
 * | `unreachable` | không tới được máy chủ |
 * | `update-required` | cần cập nhật (FR-NFR-054) |
 * | `anonymous` | đăng nhập |
 * | `password-change` | đổi mật khẩu tạm |
 * | `totp-enrollment` | đăng ký thiết bị 2FA |
 * | `ready`, chưa chọn bộ sổ | chọn dữ liệu kế toán |
 * | `ready`, đã chọn | ứng dụng (`<Outlet />`) |
 */

import type { ReactElement } from 'react'
import { Outlet } from 'react-router-dom'

import { AppLayout } from '@/app/app-layout'
import { Alert, Button } from '@/design-system/components'
import { ChangePasswordPage } from '@/features/auth/change-password-page'
import { LoginPage } from '@/features/auth/login-page'
import { ServerUnreachablePage } from '@/features/auth/server-unreachable-page'
import { TotpEnrollmentPage } from '@/features/auth/totp-enrollment-page'
import { UpdateRequiredPage } from '@/features/auth/update-required-page'
import { DatasetPickerPage } from '@/features/dataset/dataset-picker-page'
import { APP_VERSION, checkVersion } from '@/lib/app-version'
import { useAccess } from '@/lib/access'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

export function SessionGate(): ReactElement {
  const { t } = useI18n()
  const { stage, datasetCode, handshake, clearDataset, logout } = useSession()
  // Gọi vô điều kiện (hook không được nằm sau `return`); truy vấn tự nghỉ khi
  // chưa chọn dữ liệu kế toán — xem `enabled` trong `useAccess`.
  const access = useAccess()

  if (stage === 'starting') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-screen text-sm text-text-muted">
        {t('common.loading')}
      </div>
    )
  }
  if (stage === 'unreachable') {
    return <ServerUnreachablePage />
  }
  if (stage === 'update-required') {
    return <UpdateRequiredPage />
  }
  if (stage === 'anonymous') {
    return <LoginPage />
  }
  if (stage === 'password-change') {
    return <ChangePasswordPage />
  }
  if (stage === 'totp-enrollment') {
    return <TotpEnrollmentPage />
  }
  if (datasetCode === null) {
    return <DatasetPickerPage />
  }

  // Chọn được một bộ sổ **không** có nghĩa là có quyền trong đó: danh sách dữ
  // liệu kế toán không lọc theo vai trò (xem `/system/datasets`), nên câu trả
  // lời thật đến từ `/system/access` và nó có thể là `403`. Đo được lúc thử
  // trên bản cài thật: không có nhánh này, người dùng rơi vào một vỏ ứng dụng
  // rỗng, không thông báo, không lối ra.
  if (access.isError) {
    const denied = access.error instanceof ApiError ? access.error.errorCode : null
    return (
      <div className="flex min-h-screen items-center justify-center bg-screen p-6">
        <section className="w-full max-w-md rounded border-2 border-navy-700 bg-white p-6">
          <Alert tone="error">
            {denied === null ? t('error.transport.unreachable') : translateErrorCode(t, denied)}
          </Alert>
          {/* Hai lối ra, không phải một. Chỉ có nút đổi bộ sổ thì người dùng ở
              bản cài một dữ liệu kế toán không đi đâu được: đổi xong lại quay
              về đúng màn hình này. Đăng xuất là lối ra luôn tồn tại. */}
          <div className="mt-4 flex gap-2">
            <Button variant="secondary" onClick={clearDataset}>
              {t('dataset.switch')}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                void logout()
              }}
            >
              {t('common.signOut')}
            </Button>
          </div>
        </section>
      </div>
    )
  }

  const serverBehind =
    handshake !== null && checkVersion(APP_VERSION, handshake) === 'server-behind'

  return (
    <AppLayout>
      {serverBehind && (
        <div className="mb-4">
          <Alert tone="warning">
            {t('handshake.serverBehind', {
              client: APP_VERSION,
              server: handshake.server_version,
            })}
          </Alert>
        </div>
      )}
      <Outlet />
    </AppLayout>
  )
}
