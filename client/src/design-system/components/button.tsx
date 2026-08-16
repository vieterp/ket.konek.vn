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

/**
 * `primary` giữ nền navy đặc ở CẢ hai chế độ: chữ trắng trên navy-700 đạt ~11:1
 * dù nền màn hình sáng hay tối, còn nếu đổi theo token `primary` thì ở chế độ
 * tối nút thành ocean-400 và chữ trắng trên đó chỉ còn ~2,5:1 — không đọc được.
 * Hai biến thể kia dùng `primary` làm **màu chữ/viền** nên phải đổi theo chế độ,
 * nếu không thì ở nền tối chúng là navy đậm trên nền đậm.
 */
const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'border border-navy-700 bg-navy-700 text-white hover:bg-navy-800 disabled:border-navy-300 disabled:bg-navy-300',
  secondary:
    'border border-primary text-primary hover:bg-primary/10 disabled:border-navy-200 disabled:text-navy-300',
  // `.w2-b.q` của design — nút "im", VẪN CÓ viền nhưng viền nhạt và chữ thường.
  // Không phải nút chỉ-có-chữ: trong một thanh công cụ dày đặc, một nút không
  // viền không đọc ra được là nút.
  ghost:
    'border border-border-default font-normal text-text-default hover:bg-primary/10 disabled:text-navy-300',
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
      className={`inline-flex items-center justify-center gap-[7px] rounded px-[13px] py-2 text-control font-semibold transition-colors disabled:cursor-not-allowed ${VARIANTS[variant]} ${className}`}
      {...rest}
    />
  )
}
