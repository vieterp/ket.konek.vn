/**
 * Không tới được app server — và chỗ khai lại địa chỉ của nó.
 *
 * Màn hình riêng chứ không phải một dòng lỗi trên trang đăng nhập: ở bản cài
 * LAN, đây là sự cố **hay gặp nhất** (máy host tắt, đổi IP, tường lửa Windows
 * chặn cổng sau một bản vá), và người gặp nó cần đúng ba thứ — địa chỉ đang
 * gọi, chỗ sửa nó, và một nút thử lại — chứ không cần thấy ô nhập mật khẩu.
 *
 * Từ lát 2C-4, ô khai địa chỉ là phần chính chứ không phải phụ. Trước đó địa
 * chỉ máy chủ chỉ đến từ `VITE_KET_SERVER_URL` đọc lúc `vite build`, nên mỗi
 * khách hàng cần một bản đóng gói riêng, và mỗi lần máy host đổi địa chỉ là một
 * lần dựng lại rồi cài lại trên từng máy trạm. Giá trị khai ở đây được lưu tại
 * máy trạm và **thắng** giá trị ghim lúc dựng.
 *
 * Cùng địa chỉ đó dùng cho cả lời gọi API lẫn đường tự cập nhật, nên khai một
 * lần là xong cả hai — không có hai chỗ cấu hình để trôi lệch khỏi nhau.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button, TextField } from '@/design-system/components'
import { AuthShell } from '@/features/auth/auth-shell'
import { useI18n } from '@/lib/i18n'
import { isUsableServerUrl, normalizeServerUrl, storeServerUrl } from '@/lib/server-url'
import { useSession } from '@/lib/session'

export function ServerUnreachablePage(): ReactElement {
  const { t } = useI18n()
  const { serverUrl } = useSession()
  const [draft, setDraft] = useState(serverUrl)
  const [error, setError] = useState<string | null>(null)

  function save(): void {
    const candidate = normalizeServerUrl(draft)
    if (!isUsableServerUrl(candidate)) {
      setError(t('server.addressInvalid'))
      return
    }
    storeServerUrl(candidate)
    // Tải lại cả trang chứ không dựng lại riêng client: đổi máy chủ nghĩa là
    // mọi thứ dựng lúc khởi động — phiên bản, phiên đăng nhập, dữ liệu kế toán
    // đang mở — đều thuộc về máy chủ cũ và phải dựng lại từ đầu.
    window.location.reload()
  }

  return (
    <AuthShell title={t('error.transport.unreachable')}>
      <div className="flex flex-col gap-4">
        <Alert tone="error">
          {serverUrl === ''
            ? // Bản đóng gói chưa cấu hình gì: không có địa chỉ nào để hiện, và
              // nói "không tới được ''" thì vô nghĩa.
              t('server.addressMissing')
            : t('handshake.failed', { url: serverUrl })}
        </Alert>

        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault()
            save()
          }}
        >
          <TextField
            label={t('server.addressLabel')}
            value={draft}
            hint={t('server.addressHint')}
            error={error}
            inputMode="url"
            placeholder="https://may-chu.noi-bo:5443"
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
          />
          <Button type="submit">{t('server.addressSave')}</Button>
        </form>

        <Button
          variant="secondary"
          onClick={() => {
            window.location.reload()
          }}
        >
          {t('common.retry')}
        </Button>
      </div>
    </AuthShell>
  )
}
