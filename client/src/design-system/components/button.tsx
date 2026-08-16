/**
 * Nút — component cơ sở của design system (docs/design-guidelines.md §5).
 *
 * Ba biến thể, không hơn: mỗi biến thể thêm vào là một quyết định thị giác mà
 * màn hình sau sẽ phải chọn, và design Konek Screens 2a chỉ dùng ba.
 *
 * `type="button"` là mặc định có chủ đích. Mặc định của HTML là `submit`, nên
 * một nút phụ đặt trong form (Hủy, Mở rộng) sẽ **gửi form** — lỗi kinh điển,
 * và ở phần mềm kế toán thì hậu quả của nó là một chứng từ được lưu ngoài ý
 * muốn. Nút gửi form phải khai `type="submit"` tường minh.
 */

import type { ButtonHTMLAttributes, ReactElement } from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost'

const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-navy-700 text-white hover:bg-navy-800 disabled:bg-navy-300',
  secondary:
    'border-2 border-navy-700 text-navy-700 hover:bg-navy-50 disabled:border-navy-200 disabled:text-navy-300',
  ghost: 'text-navy-700 hover:bg-navy-50 disabled:text-navy-300',
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  readonly variant?: ButtonVariant
}

export function Button({
  variant = 'primary',
  type = 'button',
  className = '',
  ...rest
}: ButtonProps): ReactElement {
  return (
    <button
      type={type}
      className={`inline-flex items-center justify-center gap-2 rounded px-4 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed ${VARIANTS[variant]} ${className}`}
      {...rest}
    />
  )
}
