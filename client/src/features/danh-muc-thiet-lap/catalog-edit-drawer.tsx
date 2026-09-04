/**
 * Drawer tạo/sửa một bản ghi danh mục — dùng chung cho cả 20 danh mục.
 *
 * Bố cục theo U2: mã + tên + trường thiết yếu của danh mục hiện sẵn, phần còn
 * lại nằm trong khối "Mở rộng" thu gọn. Trường chỉ-khai-lúc-tạo (H69: tính
 * chất, đơn vị chính của VTHH) hiện ở chế độ sửa nhưng bị khóa kèm lời giải
 * thích — giấu hẳn đi thì người dùng tưởng dữ liệu mất.
 *
 * Chế độ sửa gửi **đủ mọi trường sửa được** với giá trị đang có trên form (form
 * đã nạp từ bản ghi): hợp đồng `PUT` của server là thay-toàn-bộ, một trường bỏ
 * trống nghĩa là "xóa giá trị", không phải "giữ nguyên".
 *
 * Khóa chống trùng sinh **một lần mỗi lần mở drawer** (RT-12): bấm Lưu hai lần
 * hay mạng chớp giữa chừng đều không tạo bản ghi thứ hai.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import { Alert, Button, Drawer, TextField, TreePicker } from '@/design-system/components'
import { newIdempotencyKey } from '@/lib/api-client'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'
import { useAccess } from '@/lib/access'

import type { FieldValue } from './catalog-field-inputs'
import { ExtraFieldInput } from './catalog-field-inputs'
import { useLoadChildren } from './use-load-children'
import type { CatalogDef, CatalogRow } from './catalog-types'
import { extraValue } from './catalog-types'
import {
  useCreateCatalogRecord,
  useDeleteCatalogRecord,
  useUpdateCatalogRecord,
} from './use-catalog'

export interface CatalogEditDrawerProps {
  readonly catalog: CatalogDef
  /** `null` = tạo mới; bản ghi = sửa. */
  readonly record: CatalogRow | null
  readonly open: boolean
  /** Nút cha gợi ý khi tạo từ một nhánh đang chọn trên cây. */
  readonly initialParentId?: number | null
  readonly onClose: () => void
  /** Gọi sau khi lưu/xóa thành công — chỗ gọi đóng drawer và làm mới danh sách. */
  readonly onSaved: () => void
}

interface CommonState {
  readonly code: string
  readonly name: string
  readonly nameEn: string
  readonly isGroup: boolean
  readonly isActive: boolean
  readonly parentId: number | null
  readonly branchScoped: boolean
}

function initialCommon(record: CatalogRow | null, initialParentId: number | null): CommonState {
  if (record === null) {
    return {
      code: '',
      name: '',
      nameEn: '',
      isGroup: false,
      isActive: true,
      parentId: initialParentId,
      branchScoped: false,
    }
  }
  return {
    code: record.code,
    name: record.name,
    nameEn: record.name_en ?? '',
    isGroup: record.is_group,
    isActive: record.is_active,
    parentId: record.parent_id,
    branchScoped: record.branch_id !== null,
  }
}

function initialExtras(catalog: CatalogDef, record: CatalogRow | null): Record<string, FieldValue> {
  const values: Record<string, FieldValue> = {}
  for (const field of catalog.extraFields) {
    if (record !== null) {
      values[field.key] = extraValue(record, field.key)
      continue
    }
    // Checkbox khởi tạo bằng boolean thật, không bao giờ `null`: cột bool phía
    // server không nhận null, và "chưa tick" nghĩa là `false` chứ không phải
    // "chưa trả lời" (review 3D C-1 — tạo đối tác chỉ tick một vai bị 422).
    values[field.key] =
      field.type === 'checkbox' ? (field.defaultValue ?? false) : (field.defaultValue ?? null)
  }
  return values
}

/** Ép giá trị form về kiểu thân request; trả `undefined` khi số nguyên gõ sai. */
function coerce(field: { readonly type: string }, value: FieldValue): unknown {
  if (field.type !== 'integer' || value === null || typeof value !== 'string') {
    return value
  }
  const parsed = Number.parseInt(value, 10)
  return Number.isNaN(parsed) || String(parsed) !== value.trim() ? undefined : parsed
}

export function CatalogEditDrawer({
  open,
  record,
  ...rest
}: CatalogEditDrawerProps): ReactElement | null {
  // Dựng lại toàn bộ ruột mỗi lần mở (và mỗi bản ghi khác nhau): state của form
  // sống đúng bằng một lượt mở, không cần effect "nạp lại" — cùng khuôn với
  // `ImportWizard`. Khóa theo `record.id` để chuyển từ sửa bản ghi này sang bản
  // ghi khác cũng là một lượt mới.
  if (!open) {
    return null
  }
  return <DrawerBody key={record?.id ?? 'new'} record={record} {...rest} />
}

function DrawerBody({
  catalog,
  record,
  initialParentId = null,
  onClose,
  onSaved,
}: Omit<CatalogEditDrawerProps, 'open'>): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const access = useAccess()
  const loadChildren = useLoadChildren(catalog.slug)

  const [common, setCommon] = useState<CommonState>(() => initialCommon(record, initialParentId))
  const [extras, setExtras] = useState<Record<string, FieldValue>>(() =>
    initialExtras(catalog, record),
  )
  const [expanded, setExpanded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)

  // Khóa chống trùng sinh một lần mỗi lượt mở (RT-12): cùng khóa cho mọi lần thử lưu.
  const [idempotencyKey] = useState(newIdempotencyKey)

  const create = useCreateCatalogRecord(catalog.slug)
  const update = useUpdateCatalogRecord(catalog.slug)
  const remove = useDeleteCatalogRecord(catalog.slug)
  const busy = create.isPending || update.isPending || remove.isPending

  const isEdit = record !== null
  const actingBranchId = access.data?.acting_branch_id ?? null

  const essential = catalog.extraFields.filter((field) => field.essential === true)
  const secondary = catalog.extraFields.filter((field) => field.essential !== true)

  function fail(caught: unknown): void {
    setError(
      caught instanceof ApiError
        ? translateErrorCode(t, caught.errorCode)
        : t('error.transport.unreachable'),
    )
  }

  function buildExtras(editable: boolean): Record<string, unknown> | null {
    const body: Record<string, unknown> = {}
    for (const field of catalog.extraFields) {
      if (editable && field.createOnly === true) {
        continue
      }
      if (field.type === 'checkbox') {
        // Bool luôn gửi tường minh — cột phía server không nhận null (C-1).
        body[field.key] = extras[field.key] === true
        continue
      }
      const value = coerce(field, extras[field.key] ?? null)
      if (value === undefined) {
        setError(t('catalog.error.integerInvalid', { field: t(field.labelKey) }))
        return null
      }
      if (value === null) {
        // Ô bỏ trống thì **bỏ khóa khỏi thân request** thay vì gửi null: trường
        // nullable nhận cùng kết quả (Pydantic default None), còn trường
        // non-nullable có default (`due_days` = 0…) nhận default thay vì 422
        // (review 3D H-1). "Bỏ trống" vì thế nghĩa là "về mặc định".
        continue
      }
      body[field.key] = value
    }
    return body
  }

  function save(): void {
    setError(null)
    if (common.code.trim() === '' || common.name.trim() === '') {
      setError(t('catalog.error.codeNameRequired'))
      return
    }

    if (isEdit) {
      const extrasBody = buildExtras(true)
      if (extrasBody === null) {
        return
      }
      update.mutate(
        {
          id: record.id,
          body: {
            row_version: record.row_version,
            code: common.code.trim(),
            name: common.name.trim(),
            name_en: common.nameEn.trim() === '' ? null : common.nameEn.trim(),
            is_active: common.isActive,
            ...extrasBody,
          },
        },
        { onSuccess: onSaved, onError: fail },
      )
      return
    }

    const extrasBody = buildExtras(false)
    if (extrasBody === null) {
      return
    }
    create.mutate(
      {
        idempotencyKey,
        body: {
          code: common.code.trim(),
          name: common.name.trim(),
          name_en: common.nameEn.trim() === '' ? null : common.nameEn.trim(),
          is_group: common.isGroup,
          parent_id: common.parentId,
          branch_id: common.branchScoped ? actingBranchId : null,
          ...extrasBody,
        },
      },
      { onSuccess: onSaved, onError: fail },
    )
  }

  function destroy(): void {
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    if (record !== null) {
      remove.mutate(record.id, { onSuccess: onSaved, onError: fail })
    }
  }

  const title = isEdit
    ? t('catalog.drawer.editTitle', { catalog: t(catalog.titleKey) })
    : t('catalog.drawer.createTitle', { catalog: t(catalog.titleKey) })

  return (
    <Drawer
      open
      title={title}
      onClose={onClose}
      closeLabel={t('common.close')}
      footer={
        <div className="flex w-full items-center justify-between gap-2">
          <span>
            {isEdit && !readOnly && (
              <Button variant="ghost" disabled={busy} onClick={destroy}>
                {confirmDelete ? t('catalog.drawer.deleteConfirm') : t('catalog.drawer.delete')}
              </Button>
            )}
          </span>
          <span className="flex gap-2">
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              {t('common.cancel')}
            </Button>
            {!readOnly && (
              <Button onClick={save} disabled={busy}>
                {busy ? t('catalog.drawer.saving') : t('catalog.drawer.save')}
              </Button>
            )}
          </span>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        {error !== null && <Alert tone="error">{error}</Alert>}

        <TextField
          label={`${t('catalog.field.code')} *`}
          value={common.code}
          onChange={(event) => {
            setCommon({ ...common, code: event.target.value })
          }}
        />
        <TextField
          label={`${t('catalog.field.name')} *`}
          value={common.name}
          onChange={(event) => {
            setCommon({ ...common, name: event.target.value })
          }}
        />

        {!isEdit && (
          <label className="flex items-center gap-2 text-sm text-text-default">
            <input
              type="checkbox"
              checked={common.isGroup}
              onChange={(event) => {
                setCommon({ ...common, isGroup: event.target.checked })
              }}
            />
            {t('catalog.field.isGroup')}
          </label>
        )}

        {!isEdit && (
          <TreePicker
            label={t('catalog.field.parent')}
            selectedId={common.parentId}
            onSelect={(node) => {
              setCommon({ ...common, parentId: node?.id ?? null })
            }}
            loadChildren={loadChildren}
            selectableGroups
            rootLabel={t('catalog.field.parentNone')}
            loadingLabel={t('common.loading')}
            emptyLabel={t('catalog.lookup.empty')}
            errorLabel={t('error.transport.unreachable')}
          />
        )}

        {!isEdit && actingBranchId !== null && catalog.sharedOnly !== true && (
          <label className="flex items-center gap-2 text-sm text-text-default">
            <input
              type="checkbox"
              checked={common.branchScoped}
              onChange={(event) => {
                setCommon({ ...common, branchScoped: event.target.checked })
              }}
            />
            {t('catalog.field.branchScoped')}
          </label>
        )}

        {essential.map((field) => (
          <ExtraFieldInput
            key={field.key}
            field={field}
            value={extras[field.key] ?? null}
            onChange={(value) => {
              setExtras({ ...extras, [field.key]: value })
            }}
            locked={isEdit && field.createOnly === true}
          />
        ))}

        {isEdit && (
          <label className="flex items-center gap-2 text-sm text-text-default">
            <input
              type="checkbox"
              checked={!common.isActive}
              onChange={(event) => {
                setCommon({ ...common, isActive: !event.target.checked })
              }}
            />
            {t('catalog.field.deactivated')}
          </label>
        )}

        {/* Khối "Mở rộng" luôn có mặt: tối thiểu nó chứa tên tiếng Anh (U2). */}
        {(
          <div>
            <button
              type="button"
              className="text-sm font-medium text-secondary hover:underline"
              aria-expanded={expanded}
              onClick={() => {
                setExpanded((current) => !current)
              }}
            >
              {expanded ? t('catalog.drawer.collapse') : t('catalog.drawer.expand')}
            </button>
            {expanded && (
              <div className="mt-3 flex flex-col gap-4">
                <TextField
                  label={t('catalog.field.nameEn')}
                  value={common.nameEn}
                  onChange={(event) => {
                    setCommon({ ...common, nameEn: event.target.value })
                  }}
                />
                {secondary.map((field) => (
                  <ExtraFieldInput
                    key={field.key}
                    field={field}
                    value={extras[field.key] ?? null}
                    onChange={(value) => {
                      setExtras({ ...extras, [field.key]: value })
                    }}
                    locked={isEdit && field.createOnly === true}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </Drawer>
  )
}
