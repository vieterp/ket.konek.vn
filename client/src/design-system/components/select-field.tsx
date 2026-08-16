/**
 * Ô chọn từ danh sách đóng.
 *
 * `<select>` gốc chứ không dropdown tự vẽ: nó đã đúng sẵn về bàn phím, về IME
 * tiếng Việt và về trình đọc màn hình — ba thứ mà một bản tự vẽ phải làm lại
 * từ đầu và thường làm thiếu. Lưới nhập liệu (spike S3, lát 2C-3) là chỗ duy
 * nhất được phép cân nhắc khác.
 */

import type { ReactElement, SelectHTMLAttributes } from 'react'
import { useId } from 'react'

export interface SelectOption {
  readonly value: string
  readonly label: string
}

export interface SelectFieldProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, 'id'> {
  readonly label: string
  readonly id?: string
  readonly options: readonly SelectOption[]
  /** Ẩn nhãn khỏi màn hình nhưng giữ cho trình đọc màn hình (thanh công cụ chật). */
  readonly labelHidden?: boolean
}

export function SelectField({
  label,
  id,
  options,
  labelHidden = false,
  className = '',
  ...rest
}: SelectFieldProps): ReactElement {
  const generated = useId()
  const selectId = id ?? generated

  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={selectId}
        className={
          labelHidden ? 'sr-only' : 'text-sm font-medium text-text-default'
        }
      >
        {label}
      </label>
      <select
        id={selectId}
        className={`rounded border border-border-default bg-surface px-3 py-2 text-sm text-text-default outline-none focus:border-ocean-500 focus:ring-2 focus:ring-ocean-200 ${className}`}
        {...rest}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </div>
  )
}
