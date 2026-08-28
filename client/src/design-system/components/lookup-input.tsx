/**
 * Ô chọn một bản ghi tham chiếu từ danh sách đã nạp sẵn — combobox lọc tại chỗ.
 *
 * Dành cho tham chiếu sang danh mục **nhỏ, phẳng** (điều khoản thanh toán,
 * ngân hàng, đơn vị tính): chỗ gọi nạp trọn danh sách một lần rồi đưa vào
 * `options`, việc gõ-để-lọc xảy ra hoàn toàn trong bộ nhớ. Danh mục phân cấp
 * hoặc lớn (kho, vật tư) dùng `TreePicker` — server chỉ có đường đọc theo cây.
 *
 * Lọc theo **cả mã lẫn tên**, không phân biệt hoa thường: kế toán viên nhớ mã
 * ("VCB") thường xuyên hơn nhớ đúng chính tả tên đầy đủ.
 *
 * Là component design system: không `useI18n`, không gọi API — mọi chữ và dữ
 * liệu đi vào bằng prop (xem `components/index.ts`).
 */

import type { KeyboardEvent, ReactElement } from 'react'
import { useId, useRef, useState } from 'react'
import { X } from 'lucide-react'

export interface LookupOption {
  readonly id: number
  readonly code: string
  readonly label: string
}

export interface LookupInputProps {
  readonly label: string
  readonly value: LookupOption | null
  readonly onChange: (option: LookupOption | null) => void
  readonly options: readonly LookupOption[]
  readonly placeholder?: string | undefined
  readonly clearLabel: string
  readonly emptyLabel: string
  readonly disabled?: boolean | undefined
  readonly error?: string | null | undefined
  /** Câu giải thích dưới ô — ví dụ vì sao ô bị khóa (trường chỉ khai lúc tạo). */
  readonly hint?: string | undefined
  /**
   * Báo chuỗi đang gõ cho chỗ gọi — dành cho danh mục LỚN mà `options` không
   * nạp trọn được: chỗ gọi tra server (`search=`) rồi đưa kết quả trở lại qua
   * `options`; ô vẫn tự lọc tại chỗ trên danh sách đã gộp. Không truyền thì
   * hành vi giữ nguyên như combobox nạp sẵn.
   */
  readonly onQueryChange?: ((query: string) => void) | undefined
}

const MAX_SUGGESTIONS = 20

export function LookupInput({
  label,
  value,
  onChange,
  options,
  placeholder,
  clearLabel,
  emptyLabel,
  disabled = false,
  error = null,
  hint,
  onQueryChange,
}: LookupInputProps): ReactElement {
  const inputId = useId()
  const listId = useId()
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  const needle = query.trim().toLowerCase()
  const matches =
    needle === ''
      ? options.slice(0, MAX_SUGGESTIONS)
      : options
          .filter(
            (option) =>
              option.code.toLowerCase().includes(needle) ||
              option.label.toLowerCase().includes(needle),
          )
          .slice(0, MAX_SUGGESTIONS)

  function choose(option: LookupOption): void {
    onChange(option)
    setQuery('')
    setOpen(false)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>): void {
    if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
      setOpen(true)
      event.preventDefault()
      return
    }
    if (event.key === 'ArrowDown') {
      setActiveIndex((index) => Math.min(index + 1, matches.length - 1))
    } else if (event.key === 'ArrowUp') {
      setActiveIndex((index) => Math.max(index - 1, 0))
    } else if (event.key === 'Enter') {
      const match = matches[activeIndex]
      if (open && match !== undefined) {
        choose(match)
      } else {
        return
      }
    } else if (event.key === 'Escape') {
      if (!open) {
        return
      }
      setOpen(false)
      // Đừng để Esc lọt ra ngoài: trong Drawer nó sẽ đóng luôn cả panel —
      // người dùng chỉ muốn đóng danh sách gợi ý.
      event.stopPropagation()
    } else {
      return
    }
    event.preventDefault()
  }

  const errorId = error === null ? undefined : `${inputId}-error`
  const hintId = hint === undefined ? undefined : `${inputId}-hint`

  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={inputId} className="text-sm font-medium text-text-default">
        {label}
      </label>
      {value !== null ? (
        <div className="flex items-center justify-between gap-2 rounded border border-border-default bg-surface px-3 py-2 text-sm">
          <span className="truncate text-text-default">
            {value.code} — {value.label}
          </span>
          {!disabled && (
            <button
              type="button"
              aria-label={clearLabel}
              className="shrink-0 text-text-muted hover:text-red-600"
              onClick={() => {
                onChange(null)
                // Trả focus về ô gõ ở lượt vẽ sau, khi nó đã có mặt trong DOM.
                setTimeout(() => inputRef.current?.focus(), 0)
              }}
            >
              <X size={14} aria-hidden />
            </button>
          )}
        </div>
      ) : (
        <div className="relative">
          <input
            ref={inputRef}
            id={inputId}
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={
              open && matches.length > 0 ? `${listId}-opt-${String(activeIndex)}` : undefined
            }
            aria-invalid={error !== null}
            aria-describedby={[hintId, errorId].filter(Boolean).join(' ') || undefined}
            disabled={disabled}
            placeholder={placeholder}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value)
              setOpen(true)
              setActiveIndex(0)
              onQueryChange?.(event.target.value)
            }}
            onFocus={() => {
              setOpen(true)
            }}
            onBlur={() => {
              // Trễ một nhịp để cú bấm vào gợi ý kịp chạy trước khi danh sách đóng.
              setTimeout(() => {
                setOpen(false)
              }, 120)
            }}
            onKeyDown={handleKeyDown}
            className={`w-full rounded border px-3 py-2 text-sm text-text-default outline-none focus:border-ocean-500 focus:ring-2 focus:ring-ocean-200 ${
              error === null ? 'border-border-default' : 'border-red-500'
            }`}
          />
          {open && (
            <ul
              id={listId}
              role="listbox"
              aria-label={label}
              className="absolute z-10 mt-1 max-h-56 w-full overflow-y-auto rounded border border-border-default bg-background py-1 text-sm shadow-lg"
            >
              {matches.length === 0 && (
                <li className="px-3 py-1.5 text-text-muted">{emptyLabel}</li>
              )}
              {matches.map((option, index) => (
                <li
                  key={option.id}
                  id={`${listId}-opt-${String(index)}`}
                  role="option"
                  aria-selected={index === activeIndex}
                  className={`cursor-pointer px-3 py-1.5 ${
                    index === activeIndex
                      ? 'bg-navy-50 text-primary'
                      : 'text-text-default hover:bg-primary/10'
                  }`}
                  onMouseDown={(event) => {
                    // `mousedown` chứ không `click`: `blur` của ô gõ chạy trước
                    // `click` và sẽ đóng danh sách trước khi cú bấm kịp tới.
                    event.preventDefault()
                    choose(option)
                  }}
                  onMouseEnter={() => {
                    setActiveIndex(index)
                  }}
                >
                  {option.code} — {option.label}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {hint !== undefined && (
        <p id={hintId} className="text-xs text-text-muted">
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
