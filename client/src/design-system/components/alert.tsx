/**
 * Băng thông báo: lỗi, cảnh báo, thông tin.
 *
 * `role="alert"` cho biến thể lỗi để trình đọc màn hình đọc ngay khi nó xuất
 * hiện — thông báo "sai mật khẩu" mà chỉ nhìn thấy được thì không phải là
 * thông báo với mọi người dùng.
 *
 * Không có nút đóng: mọi băng ở lát này đều biến mất khi nguyên nhân biến mất
 * (thử lại, sửa dữ liệu). Một băng đóng được là một băng người dùng đóng đi rồi
 * quên mất vấn đề.
 */

import type { ReactElement, ReactNode } from 'react'

export type AlertTone = 'error' | 'warning' | 'info'

const TONES: Record<AlertTone, string> = {
  error: 'border-red-300 bg-red-50 text-red-800',
  warning: 'border-amber-300 bg-amber-50 text-amber-900',
  info: 'border-ocean-200 bg-ocean-50 text-navy-800',
}

export function Alert({
  tone = 'info',
  children,
}: {
  readonly tone?: AlertTone
  readonly children: ReactNode
}): ReactElement {
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={`rounded border px-3 py-2 text-sm ${TONES[tone]}`}
    >
      {children}
    </div>
  )
}
