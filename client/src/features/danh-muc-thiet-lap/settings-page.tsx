/**
 * Màn hình Thiết lập — hai nhóm theo **hệ quả** (U14): "đổi được bất kỳ lúc
 * nào" và "chốt một lần", đọc từ BFF `/setup/settings-groups`.
 *
 * Luật phân nhóm nằm ở server (một bản, trong BFF) — trang này chỉ vẽ đúng thứ
 * tự nhận được. Sửa một tùy chọn đi qua `/system/settings/{key}` (BFF không
 * ghi, RT-21); `SetupItem` không mang `row_version` nên form sửa tra thêm danh
 * sách tùy chọn gốc theo `key`. Mục thuộc niên độ (`source = fiscal_year`)
 * chỉ đọc ở lát này — đường sửa của chúng ra đời cùng màn hình niên độ.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'
import { Lock, Pencil } from 'lucide-react'

import { Alert, Button, Drawer, SelectField, TextField } from '@/design-system/components'
import { translateErrorCode, useI18n } from '@/lib/i18n'
import { ApiError, useSession } from '@/lib/session'

import { FeatureNav } from './feature-nav'
import { ReportLogoCard } from './report-logo-card'
import { SettingsGroupLockedBanner } from './settings-group-locked-banner'
import type { Setting, SetupGroup, SetupItem } from './use-settings-groups'
import { useSettings, useSettingsGroups, useUpdateSetting } from './use-settings-groups'

const DECIDED_ONCE_GROUP_KEY = 'decided_once'

interface EditState {
  readonly item: SetupItem
  readonly setting: Setting
  readonly value: string
}

export function SettingsPage(): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const groups = useSettingsGroups()
  const settings = useSettings()
  const updateSetting = useUpdateSetting()

  const [edit, setEdit] = useState<EditState | null>(null)
  const [error, setError] = useState<string | null>(null)

  const groupsError =
    groups.error instanceof ApiError
      ? translateErrorCode(t, groups.error.errorCode)
      : groups.isError
        ? t('error.transport.unreachable')
        : null

  function openEdit(item: SetupItem): void {
    const setting = settings.data?.find((candidate) => candidate.key === item.key)
    if (setting === undefined) {
      return
    }
    setError(null)
    setEdit({ item, setting, value: item.value })
  }

  function save(): void {
    if (edit === null) {
      return
    }
    // BR-SYS-06: màn hình Thiết lập sửa ở cấp hệ thống; tùy chọn cá nhân sẽ có
    // chỗ riêng. Trên dây, "chưa có dòng nào" là `row_version: null` — số 0 chỉ
    // là hằng nội bộ của settings_service, gửi 0 bị 422 ngay ở lần lưu đầu tiên
    // (review 3D H-2).
    updateSetting.mutate(
      {
        key: edit.item.key,
        value: edit.value,
        scope: 'system',
        rowVersion: edit.setting.system_row_version ?? null,
      },
      {
        onSuccess: () => {
          setEdit(null)
        },
        onError: (caught) => {
          setError(
            caught instanceof ApiError
              ? translateErrorCode(t, caught.errorCode)
              : t('error.transport.unreachable'),
          )
        },
      },
    )
  }

  const editValueType = edit?.setting.value_type ?? 'string'

  return (
    <div className="flex h-full gap-4">
      <FeatureNav />
      <section className="flex min-w-0 flex-1 flex-col gap-4">
        <h1 className="text-lg font-semibold text-primary">{t('setup.title')}</h1>

        {groups.isPending && <p className="text-sm text-text-muted">{t('common.loading')}</p>}
        {groupsError !== null && <Alert tone="error">{groupsError}</Alert>}

        {(groups.data ?? []).map((group) => (
          <GroupCard
            key={group.key}
            group={group}
            // Nút sửa chỉ hiện khi danh sách tùy chọn gốc đã về: form sửa cần
            // `row_version` từ đó, và một nút bấm mà lặng lẽ không làm gì (vì
            // dữ liệu chưa về) tệ hơn một nút xuất hiện trễ nửa giây.
            readOnly={readOnly || settings.data === undefined}
            onEdit={openEdit}
          />
        ))}

        {/* Logo là tệp, không phải giá trị gõ được — thẻ riêng với nút một
            bước thay vì một dòng trong GroupCard (lát 5E). */}
        <ReportLogoCard readOnly={readOnly} />

        <Drawer
          open={edit !== null}
          title={edit?.item.title ?? ''}
          onClose={() => {
            setEdit(null)
          }}
          closeLabel={t('common.close')}
          footer={
            <div className="flex w-full justify-end gap-2">
              <Button
                variant="secondary"
                onClick={() => {
                  setEdit(null)
                }}
              >
                {t('common.cancel')}
              </Button>
              <Button onClick={save} disabled={updateSetting.isPending}>
                {t('catalog.drawer.save')}
              </Button>
            </div>
          }
        >
          <div className="flex flex-col gap-4">
            {error !== null && <Alert tone="error">{error}</Alert>}
            {edit !== null && editValueType === 'boolean' ? (
              <SelectField
                label={edit.item.title}
                value={edit.value}
                onChange={(event) => {
                  setEdit({ ...edit, value: event.target.value })
                }}
                options={[
                  { value: 'true', label: t('setup.value.on') },
                  { value: 'false', label: t('setup.value.off') },
                ]}
              />
            ) : edit !== null ? (
              <TextField
                label={edit.item.title}
                value={edit.value}
                hint={t('setup.editHint')}
                onChange={(event) => {
                  setEdit({ ...edit, value: event.target.value })
                }}
              />
            ) : null}
          </div>
        </Drawer>
      </section>
    </div>
  )
}

function GroupCard({
  group,
  readOnly,
  onEdit,
}: {
  readonly group: SetupGroup
  readonly readOnly: boolean
  readonly onEdit: (item: SetupItem) => void
}): ReactElement {
  const { t } = useI18n()
  const decidedOnce = group.key === DECIDED_ONCE_GROUP_KEY

  return (
    <section
      aria-label={group.title}
      className="rounded border border-border-default bg-background p-4"
    >
      <h2 className="text-sm font-semibold text-primary">{group.title}</h2>
      <p className="mb-2 text-xs text-text-muted">{group.description}</p>
      {decidedOnce && (
        <div className="mb-3">
          <SettingsGroupLockedBanner />
        </div>
      )}
      <ul className="divide-y divide-border-default">
        {group.items.map((item) => (
          <li
            key={`${item.key}-${item.fiscal_year_code ?? ''}`}
            className="flex items-center justify-between gap-3 py-2"
          >
            <div className="min-w-0">
              <p className="text-sm text-text-default">
                {item.title}
                {item.fiscal_year_code !== null && (
                  <span className="ml-2 rounded bg-surface px-1.5 py-0.5 text-xs text-text-muted">
                    {t('setup.fiscalYear', { year: item.fiscal_year_code })}
                  </span>
                )}
              </p>
              {item.locked_reason !== null && (
                <p className="text-xs text-text-muted">{item.locked_reason}</p>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="text-sm font-medium text-text-default">{item.value}</span>
              {item.is_editable && item.source === 'setting' && !readOnly ? (
                <Button
                  variant="ghost"
                  aria-label={t('setup.editAction', { title: item.title })}
                  onClick={() => {
                    onEdit(item)
                  }}
                >
                  <Pencil size={14} aria-hidden />
                </Button>
              ) : (
                !item.is_editable && <Lock size={14} aria-hidden className="text-text-muted" />
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}
