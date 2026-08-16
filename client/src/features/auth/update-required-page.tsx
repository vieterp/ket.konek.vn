/**
 * Màn hình "cần cập nhật" (FR-NFR-054, LD-05, bước 19).
 *
 * Hiện khi bắt tay cho thấy bản đang chạy cũ hơn `min_client_version` — tức là
 * server sẽ từ chối **mọi** lệnh ghi của nó (`426`). Chặn ở đây, ngay màn hình
 * đầu, thay vì để người dùng nhập xong một chứng từ rồi mới nhận lỗi lúc lưu.
 *
 * Nút "tiếp tục ở chế độ chỉ đọc" là phần cố ý giữ: một văn phòng đang chờ
 * người quản trị cập nhật vẫn phải tra cứu được sổ sách. Cờ `readOnly` đi theo
 * suốt phiên nên màn hình nghiệp vụ ẩn nút lưu thay vì mời người dùng bấm vào
 * thứ chắc chắn hỏng.
 *
 * Lát 2C-4 nối nút "cập nhật ngay" vào Tauri updater (gói cập nhật do chính app
 * server phục vụ, LAN không internet vẫn cập nhật được).
 */

import type { ReactElement } from 'react'

import { Alert, Button } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { APP_VERSION } from '@/lib/app-version'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

export function UpdateRequiredPage(): ReactElement {
  const { t } = useI18n()
  const { handshake, continueReadOnly } = useSession()

  return (
    <AuthShell title={t('update.title')}>
      <div className="flex flex-col gap-4">
        <Alert tone="warning">{t('update.body')}</Alert>
        <dl className="flex flex-col gap-1 text-sm text-text-default">
          <div>{t('update.currentVersion', { version: APP_VERSION })}</div>
          <div>
            {t('update.requiredVersion', {
              version: handshake?.min_client_version ?? '—',
            })}
          </div>
        </dl>
        <p className="text-sm text-text-muted">{t('update.howTo')}</p>
        <Button variant="secondary" onClick={continueReadOnly}>
          {t('update.continueReadOnly')}
        </Button>
      </div>
    </AuthShell>
  )
}
