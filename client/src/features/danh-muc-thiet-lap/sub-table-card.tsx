/**
 * Vỏ chung của ba thẻ bảng con lát 7H-2b: tiêu đề + nút Thêm, băng lỗi, bảng,
 * và khung form dưới bảng với hai nút Lưu/Hủy. Ruột bảng và ô form do từng
 * thẻ vẽ — vỏ chỉ giữ cho ba thẻ cùng một hình dạng.
 */

import type { ReactElement, ReactNode } from 'react'

import { Alert, Button } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

export function SubTableCard({
  title,
  addLabel,
  canAdd,
  onAdd,
  error,
  empty,
  children,
  form,
}: {
  readonly title: string
  readonly addLabel: string
  readonly canAdd: boolean
  readonly onAdd: () => void
  readonly error: string | null
  /** Câu hiện khi bảng trống; `null` = đang tải hoặc có dòng. */
  readonly empty: string | null
  readonly children: ReactNode
  readonly form: ReactNode
}): ReactElement {
  return (
    <section aria-label={title} className="rounded border border-border-default bg-background p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-primary">{title}</h2>
        {canAdd && (
          <Button variant="secondary" onClick={onAdd}>
            {addLabel}
          </Button>
        )}
      </div>
      {error !== null && <Alert tone="error">{error}</Alert>}
      {empty !== null && <p className="text-sm text-text-muted">{empty}</p>}
      {children}
      {form}
    </section>
  )
}

/** Hai nút cuối form thêm/sửa của thẻ bảng con. */
export function SubTableFormActions({
  onSave,
  onCancel,
  busy,
}: {
  readonly onSave: () => void
  readonly onCancel: () => void
  readonly busy: boolean
}): ReactElement {
  const { t } = useI18n()
  return (
    <div className="flex gap-2">
      <Button onClick={onSave} disabled={busy}>
        {t('catalog.drawer.save')}
      </Button>
      <Button variant="secondary" onClick={onCancel}>
        {t('common.cancel')}
      </Button>
    </div>
  )
}

/** Cặp nút Sửa / Xóa (hai bước) ở cuối một dòng bảng con. */
export function SubTableRowActions({
  onEdit,
  onRemove,
  confirming,
}: {
  readonly onEdit: () => void
  readonly onRemove: () => void
  readonly confirming: boolean
}): ReactElement {
  const { t } = useI18n()
  return (
    <td className="py-1.5 text-right whitespace-nowrap">
      <button type="button" className="mr-3 text-xs text-secondary hover:underline" onClick={onEdit}>
        {t('subTable.edit')}
      </button>
      <button type="button" className="text-xs text-red-600 hover:underline" onClick={onRemove}>
        {confirming ? t('subTable.removeConfirm') : t('subTable.remove')}
      </button>
    </td>
  )
}
