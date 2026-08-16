/**
 * Ô nhập một dòng, kèm nhãn và chỗ báo lỗi.
 *
 * Nhãn **luôn** là `<label htmlFor>` thật, không phải placeholder: người nhập
 * liệu cả ngày dùng bàn phím và trình đọc màn hình, và một ô chỉ có placeholder
 * sẽ mất nhãn ngay khi bắt đầu gõ — đúng lúc cần nó nhất để soát lại.
 *
 * `id` tự sinh bằng `useId` để chỗ gọi không phải nghĩ ra tên duy nhất; truyền
 * `id` tường minh vẫn được (form nhiều bước cần neo tới ô cụ thể).
 */

import type { InputHTMLAttributes, ReactElement, ReactNode } from 'react'
import { useId } from 'react'

export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'id'> {
  readonly label: string
  readonly id?: string
  /** Câu giải thích dưới ô — hướng dẫn, không phải lỗi. */
  readonly hint?: ReactNode
  readonly error?: string | null
}

export function TextField({
  label,
  id,
  hint,
  error = null,
  className = '',
  ...rest
}: TextFieldProps): ReactElement {
  const generated = useId()
  const inputId = id ?? generated
  const describedBy = hint === undefined ? undefined : `${inputId}-hint`
  const errorId = error === null ? undefined : `${inputId}-error`

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={inputId} className="text-sm font-medium text-text-default">
        {label}
      </label>
      <input
        id={inputId}
        aria-invalid={error !== null}
        aria-describedby={[describedBy, errorId].filter(Boolean).join(' ') || undefined}
        className={`rounded border px-3 py-2 text-sm text-text-default outline-none focus:border-ocean-500 focus:ring-2 focus:ring-ocean-200 ${
          error === null ? 'border-border-default' : 'border-red-500'
        } ${className}`}
        {...rest}
      />
      {hint !== undefined && (
        <p id={describedBy} className="text-xs text-text-muted">
          {hint}
        </p>
      )}
      {error !== null && (
        <p id={errorId} className="text-xs text-red-600">
          {error}
        </p>
      )}
    </div>
  )
}
