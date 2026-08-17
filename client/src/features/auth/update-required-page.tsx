/**
 * Màn hình "cần cập nhật" (FR-NFR-054, LD-05, bước 19 + 18).
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
 * Từ lát 2C-4, nút "cập nhật ngay" gọi thẳng updater của shell, và gói lấy từ
 * **chính app server đang dùng** — bản cài LAN không có internet vẫn cập nhật
 * được (LD-01). Chạy trong trình duyệt thì không có shell nào để gọi, và màn
 * hình nói thẳng điều đó thay vì hiện một cái nút không làm gì.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { APP_VERSION } from '@/lib/app-version'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'
import { checkAndInstallUpdate, restartApp } from '@/lib/tauri/updater'

type Progress =
  | { readonly kind: 'idle' }
  | { readonly kind: 'installing' }
  | { readonly kind: 'installed' }
  | { readonly kind: 'up-to-date' }
  | { readonly kind: 'unsupported' }
  | { readonly kind: 'failed'; readonly reason: string }

export function UpdateRequiredPage(): ReactElement {
  const { t } = useI18n()
  const { handshake, continueReadOnly, serverUrl } = useSession()
  const [progress, setProgress] = useState<Progress>({ kind: 'idle' })

  async function install(): Promise<void> {
    setProgress({ kind: 'installing' })
    try {
      // `serverUrl` là địa chỉ người dùng đã khai ở máy trạm này (hoặc mặc định
      // lúc dựng), tức là **cùng** địa chỉ mà mọi lời gọi API đang dùng. Không
      // có hai chỗ cấu hình để trôi lệch khỏi nhau.
      const outcome = await checkAndInstallUpdate(serverUrl)
      setProgress({ kind: outcome === 'installed' ? 'installed' : outcome })
    } catch (error) {
      // Hiện nguyên văn lý do thay vì một câu chung: người dùng ở màn hình này
      // đang bị chặn ghi, và họ phải đọc lại được cho người quản trị nghe.
      setProgress({ kind: 'failed', reason: error instanceof Error ? error.message : String(error) })
    }
  }

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

        {progress.kind === 'up-to-date' && <Alert tone="warning">{t('update.upToDate')}</Alert>}
        {progress.kind === 'unsupported' && <Alert tone="info">{t('update.unsupported')}</Alert>}
        {progress.kind === 'failed' && (
          <Alert tone="error">{t('update.failed', { reason: progress.reason })}</Alert>
        )}
        {progress.kind === 'installed' && <Alert tone="info">{t('update.installed')}</Alert>}

        <div className="flex flex-col gap-2">
          {progress.kind === 'installed' ? (
            <Button
              onClick={() => {
                void restartApp()
              }}
            >
              {t('update.restart')}
            </Button>
          ) : (
            <Button
              disabled={progress.kind === 'installing'}
              onClick={() => {
                void install()
              }}
            >
              {progress.kind === 'installing' ? t('update.installing') : t('update.installNow')}
            </Button>
          )}
          <Button variant="secondary" onClick={continueReadOnly}>
            {t('update.continueReadOnly')}
          </Button>
        </div>
      </div>
    </AuthShell>
  )
}
