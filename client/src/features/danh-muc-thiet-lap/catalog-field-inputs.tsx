/**
 * Ô nhập cho **trường riêng** của từng danh mục, vẽ theo mô tả trong registry.
 *
 * Mỗi loại trường một nhánh — text/số/checkbox/select tại chỗ, còn `lookup` là
 * component con riêng vì nó phải tự nạp danh sách tham chiếu (hook không gọi
 * được trong vòng lặp trường). Tham chiếu sang danh mục phẳng dùng
 * `LookupInput`; riêng kho (phân cấp) dùng `TreePicker` — server chỉ có đường
 * đọc theo cây, không có danh sách phẳng (xem `catalog-registry.ts`).
 */

import type { ReactElement } from 'react'

import type { LookupOption } from '@/design-system/components'
import { LookupInput, SelectField, TextField, TreePicker } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'

import type { ExtraField } from './catalog-types'
import { useLoadChildren } from './use-load-children'
import { useLookupOptions } from './use-lookup-options'

/** Giá trị một trường trên form — chuỗi cho ô gõ, boolean cho checkbox, id cho lookup. */
export type FieldValue = string | boolean | number | null

/** Danh mục phân cấp — tham chiếu tới nó phải chọn qua cây, không qua danh sách phẳng. */
const TREE_LOOKUP_SLUGS: readonly string[] = ['warehouses']

interface LookupFieldProps {
  readonly field: ExtraField
  readonly value: FieldValue
  readonly onChange: (value: FieldValue) => void
  readonly disabled: boolean
  readonly disabledHint?: string | undefined
}

function FlatLookupField({ field, value, onChange, disabled, disabledHint }: LookupFieldProps): ReactElement {
  const { t } = useI18n()
  const slug = field.lookupSlug ?? ''
  // Duyệt đủ cả bản ghi nằm trong nhóm (H-3), không chỉ trang gốc.
  const lookup = useLookupOptions(slug)

  const options: readonly LookupOption[] = lookup.data ?? []
  const selected = options.find((option) => option.id === value) ?? null

  return (
    <LookupInput
      label={t(field.labelKey)}
      value={selected}
      onChange={(option) => {
        onChange(option?.id ?? null)
      }}
      options={options}
      clearLabel={t('catalog.lookup.clear')}
      emptyLabel={t('catalog.lookup.empty')}
      disabled={disabled}
      hint={disabled ? disabledHint : undefined}
    />
  )
}

function TreeLookupField({ field, value, onChange, disabled, disabledHint }: LookupFieldProps): ReactElement {
  const { t } = useI18n()
  const slug = field.lookupSlug ?? ''
  const loadChildren = useLoadChildren(slug)

  if (disabled) {
    // Cây bị khóa không có ích gì — hiện giá trị dạng chữ kèm lời giải thích.
    return (
      <TextField
        label={t(field.labelKey)}
        value={value === null ? '' : String(value)}
        disabled
        hint={disabledHint}
        readOnly
      />
    )
  }

  return (
    <TreePicker
      label={t(field.labelKey)}
      selectedId={typeof value === 'number' ? value : null}
      onSelect={(node) => {
        onChange(node?.id ?? null)
      }}
      loadChildren={loadChildren}
      rootLabel={t('catalog.lookup.none')}
      loadingLabel={t('common.loading')}
      emptyLabel={t('catalog.lookup.empty')}
      errorLabel={t('error.transport.unreachable')}
    />
  )
}

export interface ExtraFieldInputProps {
  readonly field: ExtraField
  readonly value: FieldValue
  readonly onChange: (value: FieldValue) => void
  /** Trường chỉ-khai-lúc-tạo (H69) bị khóa ở chế độ sửa. */
  readonly locked: boolean
}

export function ExtraFieldInput({ field, value, onChange, locked }: ExtraFieldInputProps): ReactElement {
  const { t } = useI18n()
  const lockedHint = locked ? t('catalog.field.createOnlyHint') : undefined

  if (field.type === 'checkbox') {
    return (
      <label className="flex items-center gap-2 text-sm text-text-default">
        <input
          type="checkbox"
          checked={value === true}
          disabled={locked}
          onChange={(event) => {
            onChange(event.target.checked)
          }}
        />
        {t(field.labelKey)}
      </label>
    )
  }

  if (field.type === 'select') {
    const options = field.options ?? []
    return (
      <SelectField
        label={t(field.labelKey)}
        value={typeof value === 'string' ? value : ''}
        disabled={locked}
        onChange={(event) => {
          onChange(event.target.value === '' ? null : event.target.value)
        }}
        options={[
          { value: '', label: t('catalog.lookup.none') },
          ...options.map((option) => ({ value: option.value, label: t(option.labelKey) })),
        ]}
      />
    )
  }

  if (field.type === 'lookup') {
    const Component = TREE_LOOKUP_SLUGS.includes(field.lookupSlug ?? '')
      ? TreeLookupField
      : FlatLookupField
    return (
      <Component
        field={field}
        value={value}
        onChange={onChange}
        disabled={locked}
        disabledHint={lockedHint}
      />
    )
  }

  // text / integer / decimal — cùng một ô gõ, khác `inputMode` để bàn phím số
  // hiện đúng; kiểm tra kiểu thật vẫn ở server (client không tính tiền, LD-03).
  return (
    <TextField
      label={t(field.labelKey)}
      value={value === null || typeof value === 'boolean' ? '' : String(value)}
      disabled={locked}
      hint={lockedHint}
      inputMode={field.type === 'text' ? undefined : 'decimal'}
      onChange={(event) => {
        onChange(event.target.value === '' ? null : event.target.value)
      }}
    />
  )
}
