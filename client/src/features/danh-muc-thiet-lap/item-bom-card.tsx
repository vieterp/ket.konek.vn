/**
 * Thẻ định mức nguyên vật liệu của một mã hàng (FR-SYS-044, bảng con
 * `item_bom_lines`, lát 8C-2). Mỗi dòng: linh kiện · số lượng theo đơn vị
 * CHÍNH của linh kiện cho MỘT đơn vị chính thành phẩm · tỷ lệ phân bổ giá trị
 * khi tháo dỡ. Mã linh kiện tra server (`search=`) như dòng bảng giá; tên của
 * dòng đã lưu tra bù theo `ids=`. Vòng / tự trỏ / hàng không qua kho do server
 * từ chối — lỗi hiện qua `editor.fail`.
 */

import type { ReactElement } from 'react'
import { useState } from 'react'

import type { LookupOption } from '@/design-system/components'
import { LookupInput, TextField } from '@/design-system/components'
import { useI18n } from '@/lib/i18n'
import { useSession } from '@/lib/session'

import { SubTableCard, SubTableFormActions, SubTableRowActions } from './sub-table-card'
import { useItemBomLines, useItemBomMutations, type ItemBomLine } from './use-item-pricing'
import { useMasterSearchLookup } from './use-master-search-lookup'
import { useSubTableEditor } from './use-sub-table-editor'

interface Draft {
  readonly component: LookupOption | null
  readonly quantity: string
  readonly allocationRatio: string
}

const EMPTY_DRAFT: Draft = { component: null, quantity: '', allocationRatio: '1' }

export function ItemBomCard({ itemId }: { readonly itemId: number }): ReactElement {
  const { t } = useI18n()
  const { readOnly } = useSession()
  const rows = useItemBomLines(itemId)
  const { create, update, remove } = useItemBomMutations(itemId)
  const editor = useSubTableEditor<ItemBomLine>()
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)

  const list = rows.data ?? []
  const itemLookup = useMasterSearchLookup('items', list.map((row) => row.component_item_id))

  const componentLabel = (componentId: number): string => {
    const option = itemLookup.byId.get(componentId)
    return option === undefined ? `#${String(componentId)}` : `${option.code} — ${option.label}`
  }

  function open(row: ItemBomLine | 'new'): void {
    if (row === 'new') {
      setDraft(EMPTY_DRAFT)
      editor.openNew()
      return
    }
    setDraft({
      component: itemLookup.byId.get(row.component_item_id) ?? {
        id: row.component_item_id,
        code: `#${String(row.component_item_id)}`,
        label: '',
      },
      quantity: row.quantity,
      allocationRatio: row.allocation_ratio,
    })
    editor.openEdit(row)
  }

  function submit(): void {
    if (draft.component === null || draft.quantity.trim() === '') {
      editor.setError(t('item.bom.required'))
      return
    }
    editor.setError(null)
    const body = {
      component_item_id: draft.component.id,
      quantity: draft.quantity.trim(),
      allocation_ratio: draft.allocationRatio.trim() === '' ? '1' : draft.allocationRatio.trim(),
    }
    if (editor.editing === 'new') {
      create.mutate({ body, idempotencyKey: editor.idempotencyKey }, { onSuccess: editor.close, onError: editor.fail })
    } else if (editor.editing !== null) {
      update.mutate(
        { rowId: editor.editing.id, body: { ...body, row_version: editor.editing.row_version } },
        { onSuccess: editor.close, onError: editor.fail },
      )
    }
  }

  const set = (patch: Partial<Draft>): void => {
    setDraft((current) => ({ ...current, ...patch }))
  }

  return (
    <SubTableCard
      title={t('item.bom.title')}
      addLabel={t('item.bom.add')}
      canAdd={!readOnly && editor.editing === null}
      onAdd={() => {
        open('new')
      }}
      error={editor.error}
      empty={list.length === 0 && !rows.isPending ? t('item.bom.empty') : null}
      form={
        editor.editing !== null && (
          <div className="mt-3 flex flex-col gap-3 rounded border border-border-default p-3">
            <LookupInput
              label={`${t('item.bom.component')} *`}
              value={draft.component}
              onChange={(option) => {
                set({ component: option })
              }}
              options={itemLookup.options}
              onQueryChange={itemLookup.searchFor}
              clearLabel={t('catalog.lookup.clear')}
              emptyLabel={t('catalog.lookup.empty')}
            />
            <TextField
              label={`${t('item.bom.quantity')} *`}
              inputMode="decimal"
              value={draft.quantity}
              onChange={(event) => {
                set({ quantity: event.target.value })
              }}
              hint={t('item.bom.quantityHint')}
            />
            <TextField
              label={t('item.bom.allocationRatio')}
              inputMode="decimal"
              value={draft.allocationRatio}
              onChange={(event) => {
                set({ allocationRatio: event.target.value })
              }}
              hint={t('item.bom.allocationRatioHint')}
            />
            <SubTableFormActions onSave={submit} onCancel={editor.close} busy={create.isPending || update.isPending} />
          </div>
        )
      }
    >
      {list.length > 0 && (
        <table className="w-full text-left text-sm">
          <caption className="sr-only">{t('item.bom.title')}</caption>
          <thead>
            <tr className="text-xs text-text-muted">
              <th className="py-1 pr-2">{t('item.bom.component')}</th>
              <th className="py-1 pr-2 text-right">{t('item.bom.quantity')}</th>
              <th className="py-1 pr-2 text-right">{t('item.bom.allocationRatio')}</th>
              <th className="py-1" />
            </tr>
          </thead>
          <tbody>
            {list.map((row) => (
              <tr key={row.id} className="border-t border-border-default">
                <td className="py-1.5 pr-2">{componentLabel(row.component_item_id)}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.quantity}</td>
                <td className="py-1.5 pr-2 text-right tabular-nums">{row.allocation_ratio}</td>
                {readOnly ? (
                  <td />
                ) : (
                  <SubTableRowActions
                    onEdit={() => {
                      open(row)
                    }}
                    onRemove={() => {
                      if (editor.confirmRemove(row.id)) {
                        remove.mutate(row.id, { onError: editor.fail })
                      }
                    }}
                    confirming={editor.confirmingId === row.id}
                  />
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </SubTableCard>
  )
}
