/**
 * Không tới được app server.
 *
 * Màn hình riêng chứ không phải một dòng lỗi trên trang đăng nhập: ở bản cài
 * LAN, đây là sự cố **hay gặp nhất** (máy host tắt, đổi IP, tường lửa Windows
 * chặn cổng sau một bản vá), và người gặp nó cần đúng hai thứ — địa chỉ đang
 * gọi và một nút thử lại — chứ không cần thấy ô nhập mật khẩu.
 */

import type { ReactElement } from 'react'

import { Alert, Button } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

export function ServerUnreachablePage(): ReactElement {
  const { t } = useI18n()
  const { serverUrl } = useSession()

  return (
    <AuthShell title={t('error.transport.unreachable')}>
      <div className="flex flex-col gap-4">
        <Alert tone="error">{t('handshake.failed', { url: serverUrl })}</Alert>
        <Button
          onClick={() => {
            // Tải lại cả trang chứ không thử lại riêng lượt bắt tay: khi máy chủ
            // vừa khởi động lại, mọi thứ dựng lúc khởi động (phiên bản, phiên
            // đăng nhập) đều phải dựng lại từ đầu.
            window.location.reload()
          }}
        >
          {t('common.retry')}
        </Button>
      </div>
    </AuthShell>
  )
}
